"""Bounded, atomic CSV onboarding of drafts; preview never authorizes a commit."""

import csv
import hashlib
import io
import json
import uuid

from django.core.exceptions import ObjectDoesNotExist, PermissionDenied, ValidationError
from django.db import connection, models, transaction

from catalog.import_batches import CSVImportBatch
from catalog.models import Category, Material, Product, ProductVariant, Purity
from compliance.audit import model_facts, record_model_change
from compliance.models import AuditEvent
from inventory.services import register_unit
from markets.models import Hub

# Technical request bounds, unrelated to commercial stock or trial-box limits.
MAX_CSV_BYTES = 1_000_000
MAX_CSV_ROWS = 500
HEADERS = {
    "VARIANTS": (
        "product_code",
        "product_name",
        "category_code",
        "sku",
        "material_code",
        "purity_code",
        "size_label",
    ),
    "UNITS": ("registration_id", "variant_sku", "hub_code", "source_reference"),
}


def _authorize(actor, kind):
    required = ["catalog.import_catalogue"]
    if kind == "VARIANTS":
        required += [
            "catalog.add_product",
            "catalog.add_productvariant",
            "catalog.view_category",
            "catalog.view_material",
            "catalog.view_purity",
        ]
    elif kind == "UNITS":
        required += [
            "inventory.add_inventoryunit",
            "catalog.view_productvariant",
            "markets.view_hub",
        ]
    else:
        raise ValidationError("Select a supported CSV format.")
    if (
        not actor.is_authenticated
        or not actor.is_active
        or not actor.is_staff
        or not actor.has_perms(required)
    ):
        raise PermissionDenied("This staff account cannot perform this import.")


def parse_csv(content: bytes, kind: str) -> list[dict]:
    if kind not in HEADERS:
        raise ValidationError("Select a supported CSV format.")
    if not isinstance(content, bytes) or len(content) > MAX_CSV_BYTES:
        raise ValidationError("CSV must be at most 1,000,000 bytes.")
    try:
        text = content.decode("utf-8-sig")
        reader = csv.DictReader(io.StringIO(text, newline=""), strict=True)
        if reader.fieldnames != list(HEADERS[kind]):
            raise ValidationError(f"Use these CSV columns in order: {', '.join(HEADERS[kind])}.")
        rows = []
        seen = set()
        for line, row in enumerate(reader, 2):
            if len(rows) >= MAX_CSV_ROWS:
                raise ValidationError("CSV supports at most 500 rows per import.")
            if None in row or any(value is None or "\x00" in value for value in row.values()):
                raise ValidationError(f"Row {line}: the column count or text is invalid.")
            if any(value != value.strip() for value in row.values()):
                raise ValidationError(f"Row {line}: remove surrounding whitespace from values.")
            key = row["sku" if kind == "VARIANTS" else "registration_id"]
            if kind == "UNITS":
                try:
                    key = str(uuid.UUID(key))
                except ValueError as error:
                    raise ValidationError(f"Row {line}: use a UUID registration ID.") from error
                row["registration_id"] = key
            if not key or key in seen:
                raise ValidationError(
                    f"Row {line}: each row needs a distinct SKU or registration ID."
                )
            seen.add(key)
            rows.append(row)
    except (UnicodeDecodeError, csv.Error) as error:
        raise ValidationError("Upload a well-formed UTF-8 CSV.") from error
    if not rows:
        raise ValidationError("The CSV contains no onboarding rows.")
    return rows


def _variant_inputs(row, *, lock=False):
    category = Category.objects.get(code=row["category_code"])
    material = Material.objects.get(code=row["material_code"])
    purity = (
        Purity.objects.get(material=material, code=row["purity_code"])
        if row["purity_code"]
        else None
    )
    variants = ProductVariant.objects.select_for_update() if lock else ProductVariant.objects
    variant = variants.filter(sku=row["sku"]).first()
    products = Product.objects.select_for_update() if lock else Product.objects
    product = products.filter(code=row["product_code"]).first()
    expected_product = {"name": row["product_name"], "category_id": category.pk}
    if product is not None:
        if product.status != "DRAFT" or any(
            getattr(product, key) != value for key, value in expected_product.items()
        ):
            raise ValidationError(
                "Existing product must be a matching draft; imports do not edit records."
            )
    else:
        product = Product(code=row["product_code"], **expected_product)
        product.full_clean()
    if variant is not None:
        expected = (
            product.pk,
            material.pk,
            purity.pk if purity else None,
            row["size_label"],
            {},
            "DRAFT",
        )
        actual = (
            variant.product_id,
            variant.material_id,
            variant.purity_id,
            variant.size_label,
            variant.specification,
            variant.status,
        )
        if actual != expected:
            raise ValidationError(
                "Existing SKU must be a matching draft; imports do not redefine variants."
            )
    else:
        variant = ProductVariant(
            product=product,
            material=material,
            purity=purity,
            sku=row["sku"],
            size_label=row["size_label"],
        )
        variant.full_clean(exclude=["product"] if product.pk is None else [])
    return product, variant


def _unit_inputs(row):
    try:
        registration_id = uuid.UUID(row["registration_id"])
    except (ValueError, AttributeError) as error:
        raise ValidationError("Use a UUID registration ID for each physical piece.") from error
    if not row["source_reference"] or len(row["source_reference"]) > 500:
        raise ValidationError("Record a source reference of 1–500 characters.")
    return (
        ProductVariant.objects.get(sku=row["variant_sku"]),
        Hub.objects.get(code=row["hub_code"]),
        registration_id,
    )


def _validate_rows(rows, kind):
    errors = []
    product_definitions = {}
    for line, row in enumerate(rows, 2):
        try:
            if kind == "VARIANTS":
                definition = (row["product_name"], row["category_code"])
                previous = product_definitions.setdefault(row["product_code"], definition)
                if definition != previous:
                    raise ValidationError(
                        "Repeated product codes must use the same name and category."
                    )
                _variant_inputs(row)
            else:
                _unit_inputs(row)
        except ObjectDoesNotExist:
            errors.append(
                f"Row {line}: a referenced category, material, purity, SKU or hub does not exist."
            )
        except ValidationError as error:
            errors.append(f"Row {line}: {'; '.join(error.messages)}")
    return errors


def preview_import(*, actor, kind, content):
    _authorize(actor, kind)
    rows = parse_csv(content, kind)
    return {
        "kind": kind,
        "rows": rows,
        "row_count": len(rows),
        "errors": _validate_rows(rows, kind),
    }


def _lock_import_references(rows, kind):
    # Acquire all request keys before all variants, then all products. Holding a
    # product from an earlier row while waiting for another variant would invert
    # the registration service's variant -> product order.
    if kind == "UNITS":
        keys = sorted({uuid.UUID(row["registration_id"]).int & ((1 << 63) - 1) for row in rows})
        with connection.cursor() as cursor:
            for key in keys:
                cursor.execute("SELECT pg_advisory_xact_lock(%s)", [key])
    skus = [row["sku" if kind == "VARIANTS" else "variant_sku"] for row in rows]
    variants = list(ProductVariant.objects.select_for_update().filter(sku__in=skus).order_by("pk"))
    product_filter = models.Q(pk__in=[variant.product_id for variant in variants])
    if kind == "VARIANTS":
        product_filter |= models.Q(code__in=[row["product_code"] for row in rows])
    list(Product.objects.select_for_update().filter(product_filter).order_by("pk"))


def apply_import(*, actor, kind, content, idempotency_key, reason):
    """Revalidate the whole file at commit; preserve exact outcomes on retries.

    A draft CSV never receives stock or approves eligibility/prices.
    Refs: BUSINESS_RULES.md R-11, R-15, R-28, R-30; DECISIONS.md BD-09, BD-13.
    """
    _authorize(actor, kind)
    rows = parse_csv(content, kind)
    if not isinstance(reason, str) or not reason.strip() or reason != reason.strip():
        raise ValidationError("Provide a nonblank import reason without outer whitespace.")
    try:
        key = uuid.UUID(str(idempotency_key))
    except (ValueError, AttributeError) as error:
        raise ValidationError("Supply a valid import identifier.") from error
    digest = hashlib.sha256(
        json.dumps(
            {"actor": str(actor.public_id), "kind": kind, "rows": rows, "reason": reason},
            sort_keys=True,
        ).encode()
    ).hexdigest()
    with transaction.atomic():
        from common.serialization import lock_integrity_writes

        lock_integrity_writes("default")
        # Serialize bounded catalogue imports; Admin/model constraints still guard other writers.
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_xact_lock(%s)", [674805010304])
        previous = CSVImportBatch.objects.filter(idempotency_key=key).first()
        if previous:
            if previous.payload_sha256 != digest:
                raise ValidationError("This import identifier belongs to a different request.")
            return previous
        errors = _validate_rows(rows, kind)
        if errors:
            raise ValidationError(errors)
        _lock_import_references(rows, kind)
        results = []
        for row in rows:
            if kind == "VARIANTS":
                product, variant = _variant_inputs(row, lock=True)
                if product.pk is None:
                    product.save()
                    record_model_change(
                        actor=actor,
                        instance=product,
                        before={},
                        after=model_facts(product, ("code", "name", "category_id", "status")),
                        reason=reason,
                    )
                variant.product = product
                created = variant.pk is None
                if created:
                    variant.save()
                    record_model_change(
                        actor=actor,
                        instance=variant,
                        before={},
                        after=model_facts(
                            variant,
                            (
                                "sku",
                                "product_id",
                                "material_id",
                                "purity_id",
                                "size_label",
                                "status",
                            ),
                        ),
                        reason=reason,
                    )
                results.append(
                    {"sku": variant.sku, "id": str(variant.public_id), "created": created}
                )
            else:
                variant, hub, registration_id = _unit_inputs(row)
                unit = register_unit(
                    variant=variant,
                    hub=hub,
                    actor=actor,
                    idempotency_key=registration_id,
                    reason=reason,
                    source_reference=row["source_reference"],
                )
                results.append(
                    {
                        "registration_id": str(registration_id),
                        "id": str(unit.public_id),
                    }
                )
        batch = CSVImportBatch.objects.create(
            idempotency_key=key,
            actor=actor,
            kind=kind,
            payload_sha256=digest,
            reason=reason,
            row_count=len(rows),
            result=results,
        )
        AuditEvent.objects.create(
            actor=actor,
            action="catalog.import",
            resource_type="catalog.csvimportbatch",
            resource_identifier=str(batch.public_id),
            reason=reason,
            changes={"kind": kind, "row_count": len(rows), "result": results},
            correlation_id=key,
        )
        return batch
