"""Fictional draft onboarding, without receiving or commercial policy approval."""

import csv
import io
import uuid
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest.mock import patch

import pytest
from django.contrib.admin.sites import AdminSite
from django.contrib.auth.models import Permission
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import DatabaseError, close_old_connections, connection, transaction
from django.db.models.deletion import ProtectedError
from django.test import Client, RequestFactory
from django.urls import reverse

from accounts.models import StaffUser
from catalog.admin import MaterialAdmin
from catalog.imports import HEADERS, CSVImportBatch, apply_import, parse_csv, preview_import
from catalog.models import Category, Material, Product, ProductVariant, Purity
from compliance.models import AuditEvent
from inventory.models import InventoryMovement, InventoryUnit
from inventory.services import register_unit
from markets.models import Hub, Market

pytestmark = pytest.mark.django_db

VARIANT_PERMISSIONS = (
    "catalog.import_catalogue",
    "catalog.add_product",
    "catalog.add_productvariant",
    "catalog.view_category",
    "catalog.view_material",
    "catalog.view_purity",
)


@pytest.fixture
def catalogue():
    actor = StaffUser.objects.create_superuser(username="fictional-onboarding-admin")
    market = Market.objects.create(code="IMPORT-CITY", name="Fictional city", country_code="IN")
    hub = Hub.objects.create(code="IMPORT-HUB", name="Fictional hub", market=market)
    category = Category.objects.create(code="IMPORT-CATEGORY", name="Fictional category")
    material = Material.objects.create(code="IMPORT-MATERIAL", name="Fictional material")
    purity = Purity.objects.create(material=material, code="IMPORT-PURITY", name="Fictional purity")
    return {
        "actor": actor,
        "hub": hub,
        "category": category,
        "material": material,
        "purity": purity,
    }


def variant_row(**changes):
    return {
        "product_code": "IMPORT-DESIGN",
        "product_name": "Fictional design",
        "category_code": "IMPORT-CATEGORY",
        "sku": "opaque-import-sku",
        "material_code": "IMPORT-MATERIAL",
        "purity_code": "IMPORT-PURITY",
        "size_label": "Fictional size",
        **changes,
    }


def content(kind, rows):
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=HEADERS[kind])
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue().encode("utf-8")


def unit_rows(graph, count=1):
    product = Product.objects.create(
        code="UNIT-IMPORT-DESIGN", name="Fictional unit design", category=graph["category"]
    )
    variant = ProductVariant.objects.create(
        product=product, material=graph["material"], sku="opaque-unit-import-sku"
    )
    return [
        {
            "registration_id": str(uuid.uuid4()),
            "variant_sku": variant.sku,
            "hub_code": graph["hub"].code,
            "source_reference": "fictional-evidence:stock-intake",
        }
        for _ in range(count)
    ]


def request_values(graph, **changes):
    return {
        "actor": graph["actor"],
        "kind": "VARIANTS",
        "content": content("VARIANTS", [variant_row()]),
        "idempotency_key": uuid.uuid4(),
        "reason": "Fictional catalogue onboarding",
        **changes,
    }


def record_counts():
    return tuple(
        model.objects.count()
        for model in (
            Product,
            ProductVariant,
            InventoryUnit,
            InventoryMovement,
            CSVImportBatch,
            AuditEvent,
        )
    )


def importer():
    actor = StaffUser.objects.create_user(username="fictional-import-only-staff", is_staff=True)
    for permission in VARIANT_PERMISSIONS:
        app_label, codename = permission.split(".")
        actor.user_permissions.add(
            Permission.objects.get(content_type__app_label=app_label, codename=codename)
        )
    return actor


@pytest.mark.parametrize("kind", ["VARIANTS", "UNITS"])
def test_preview_validates_without_writing_identity_or_evidence(catalogue, kind):
    rows = [variant_row(), variant_row(sku="second-opaque-sku")]
    if kind == "UNITS":
        rows = unit_rows(catalogue, count=2)
    before = record_counts()
    preview = preview_import(actor=catalogue["actor"], kind=kind, content=content(kind, rows))
    assert preview["errors"] == []
    assert preview["row_count"] == 2
    assert record_counts() == before


@pytest.mark.parametrize(
    "malformed",
    [
        pytest.param(b"\xff", id="invalid-utf8"),
        pytest.param(b"sku,product_code\nA,B\n", id="wrong-columns"),
        pytest.param(content("VARIANTS", []), id="empty-import"),
        pytest.param(content("VARIANTS", [variant_row(sku=" padded")]), id="whitespace"),
        pytest.param(content("VARIANTS", [variant_row(sku="bad\x00sku")]), id="nul"),
        pytest.param(content("VARIANTS", [variant_row(), variant_row()]), id="duplicate-sku"),
        pytest.param(b"x" * 1_000_001, id="request-size"),
    ],
)
def test_malformed_csv_cannot_write_partial_results(catalogue, malformed):
    before = record_counts()
    with pytest.raises(ValidationError):
        apply_import(**request_values(catalogue, content=malformed))
    assert record_counts() == before


@pytest.mark.parametrize("alias", ["hex", "uppercase", "braced"])
def test_equivalent_registration_uuid_spellings_cannot_count_as_two_pieces(catalogue, alias):
    rows = unit_rows(catalogue)
    identity = uuid.UUID("abcdefab-1234-4234-8234-abcdefabcdef")
    spellings = {
        "hex": identity.hex,
        "uppercase": str(identity).upper(),
        "braced": f"{{{identity}}}",
    }
    rows[0]["registration_id"] = str(identity)
    rows.append({**rows[0], "registration_id": spellings[alias]})
    payload = content("UNITS", rows)
    before = record_counts()
    with pytest.raises(ValidationError, match="distinct"):
        parse_csv(payload, "UNITS")
    with pytest.raises(ValidationError, match="distinct"):
        apply_import(**request_values(catalogue, kind="UNITS", content=payload))
    assert record_counts() == before


@pytest.mark.parametrize("kind", ["VARIANTS", "UNITS"])
def test_invalid_later_row_prevents_the_entire_import(catalogue, kind):
    if kind == "VARIANTS":
        rows = [variant_row(), variant_row(sku="second-sku", category_code="MISSING-CATEGORY")]
    else:
        rows = unit_rows(catalogue, count=2)
        rows[1]["hub_code"] = "MISSING-HUB"
    values = request_values(catalogue, kind=kind, content=content(kind, rows))
    before = record_counts()
    preview = preview_import(actor=values["actor"], kind=kind, content=values["content"])
    assert len(preview["errors"]) == 1
    assert "Row 3" in preview["errors"][0]
    with pytest.raises(ValidationError, match="Row 3"):
        apply_import(**values)
    assert record_counts() == before


def test_later_registration_failure_rolls_back_earlier_unit_movement_and_audit(catalogue):
    rows = unit_rows(catalogue, count=2)
    before = record_counts()
    calls = 0

    def fail_second(**values):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise ValidationError("Fictional late registration conflict")
        return register_unit(**values)

    with (
        patch("catalog.imports.register_unit", side_effect=fail_second),
        pytest.raises(ValidationError, match="late registration conflict"),
    ):
        apply_import(**request_values(catalogue, kind="UNITS", content=content("UNITS", rows)))
    assert calls == 2
    assert record_counts() == before


@pytest.mark.parametrize("kind", ["VARIANTS", "UNITS"])
def test_exact_import_retry_returns_original_outcome_without_duplicate_writes(catalogue, kind):
    rows = [variant_row()] if kind == "VARIANTS" else unit_rows(catalogue)
    values = request_values(catalogue, kind=kind, content=content(kind, rows))
    first = apply_import(**values)
    after_first = record_counts()
    retry = apply_import(**values)
    assert retry.pk == first.pk
    assert retry.result == first.result
    assert record_counts() == after_first
    if kind == "UNITS":
        unit = InventoryUnit.objects.get(public_id=first.result[0]["id"])
        assert (unit.status, unit.custody) == ("DRAFT", "UNKNOWN")
        assert unit.movements.get().action == InventoryMovement.Action.REGISTER


@pytest.mark.parametrize("changed", ["content", "reason", "actor"])
def test_retry_identifier_cannot_be_reused_for_a_changed_request_or_actor(catalogue, changed):
    values = request_values(catalogue)
    apply_import(**values)
    before = record_counts()
    replacement = {
        "content": content("VARIANTS", [variant_row(sku="different-sku")]),
        "reason": "A different fictional explanation",
        "actor": StaffUser.objects.create_superuser(username="another-fictional-importer"),
    }
    with pytest.raises(ValidationError, match="different request"):
        apply_import(**{**values, changed: replacement[changed]})
    assert record_counts() == before


def test_matching_existing_drafts_are_reused_without_redefinition(catalogue):
    first = apply_import(**request_values(catalogue))
    second = apply_import(**request_values(catalogue))
    assert first.pk != second.pk
    assert first.result[0]["id"] == second.result[0]["id"]
    assert Product.objects.count() == ProductVariant.objects.count() == 1
    assert AuditEvent.objects.filter(action="admin.create").count() == 2


def test_commit_revalidates_draft_status_after_a_successful_preview(catalogue):
    values = request_values(catalogue)
    apply_import(**values)
    assert (
        preview_import(actor=values["actor"], kind=values["kind"], content=values["content"])[
            "errors"
        ]
        == []
    )
    product = Product.objects.get(code="IMPORT-DESIGN")
    product.status = "ACTIVE"
    product.save()
    before = record_counts()
    with pytest.raises(ValidationError, match="matching draft"):
        apply_import(**{**values, "idempotency_key": uuid.uuid4()})
    assert record_counts() == before


def test_permission_is_rechecked_before_returning_a_previous_import(catalogue):
    actor = importer()
    values = request_values(catalogue, actor=actor)
    apply_import(**values)
    before = record_counts()
    actor.user_permissions.clear()
    actor = StaffUser.objects.get(pk=actor.pk)
    with pytest.raises(PermissionDenied):
        apply_import(**{**values, "actor": actor})
    assert record_counts() == before


@pytest.mark.parametrize("is_staff,is_active", [(False, True), (True, False)])
def test_staff_and_active_flags_are_required_even_with_import_permissions(
    catalogue, is_staff, is_active
):
    actor = importer()
    actor.is_staff, actor.is_active = is_staff, is_active
    actor.save()
    with pytest.raises(PermissionDenied):
        preview_import(actor=actor, kind="VARIANTS", content=content("VARIANTS", [variant_row()]))
    assert not CSVImportBatch.objects.exists()


@pytest.mark.parametrize("kind", ["VARIANTS", "UNITS"])
def test_final_import_audit_failure_rolls_back_all_domain_and_history_writes(catalogue, kind):
    rows = [variant_row()] if kind == "VARIANTS" else unit_rows(catalogue)
    before = record_counts()
    create_audit = AuditEvent.objects.create

    def fail_import_audit(**values):
        if values.get("action") == "catalog.import":
            raise RuntimeError("Fictional final audit failure")
        return create_audit(**values)

    with (
        patch("catalog.imports.AuditEvent.objects.create", side_effect=fail_import_audit),
        pytest.raises(RuntimeError, match="final audit failure"),
    ):
        apply_import(**request_values(catalogue, kind=kind, content=content(kind, rows)))
    assert record_counts() == before


def test_import_outcomes_are_append_only_through_orm_and_protect_the_actor(catalogue):
    batch = apply_import(**request_values(catalogue))
    original_reason = batch.reason
    batch.reason = "Rewrite"
    with pytest.raises(TypeError):
        batch.save()
    with pytest.raises(TypeError):
        batch.delete()
    with pytest.raises(TypeError):
        CSVImportBatch.objects.filter(pk=batch.pk).update(reason="Rewrite")
    with pytest.raises(TypeError):
        CSVImportBatch.objects.filter(pk=batch.pk).delete()
    with pytest.raises(TypeError):
        CSVImportBatch.objects.bulk_update([batch], ["reason"])
    with pytest.raises(ProtectedError):
        catalogue["actor"].delete()
    batch.refresh_from_db()
    assert batch.reason == original_reason


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE catalog_csvimportbatch SET reason = 'rewrite' WHERE id = %s",
        "DELETE FROM catalog_csvimportbatch WHERE id = %s",
    ],
)
def test_postgresql_rejects_import_history_rewrites(catalogue, sql):
    batch = apply_import(**request_values(catalogue))
    with (
        pytest.raises(DatabaseError, match="append-only"),
        transaction.atomic(),
        connection.cursor() as cursor,
    ):
        cursor.execute(sql, [batch.pk])
    batch.refresh_from_db()
    assert batch.reason == "Fictional catalogue onboarding"


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("kind", ["VARIANTS", "UNITS"])
def test_concurrent_same_batch_records_only_one_outcome(catalogue, kind):
    rows = [variant_row()] if kind == "VARIANTS" else unit_rows(catalogue)
    values = request_values(catalogue, kind=kind, content=content(kind, rows))
    barrier = Barrier(2)

    def execute():
        close_old_connections()
        try:
            actor = StaffUser.objects.get(pk=catalogue["actor"].pk)
            barrier.wait(timeout=10)
            batch = apply_import(**{**values, "actor": actor})
            return batch.pk, batch.result
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as workers:
        futures = [workers.submit(execute) for _ in range(2)]
        results = [future.result(timeout=25) for future in futures]
    assert results[0] == results[1]
    assert CSVImportBatch.objects.count() == 1
    assert AuditEvent.objects.filter(action="catalog.import").count() == 1
    if kind == "UNITS":
        assert InventoryUnit.objects.count() == InventoryMovement.objects.count() == 1
    else:
        assert Product.objects.count() == ProductVariant.objects.count() == 1
        assert AuditEvent.objects.filter(action="admin.create").count() == 2


def admin_request(actor):
    request = RequestFactory().post("/admin/")
    request.user = actor
    return request


def test_reference_admin_records_draft_maintenance_with_original_history(catalogue):
    model_admin = MaterialAdmin(Material, AdminSite())
    request = admin_request(catalogue["actor"])
    material = Material(code="NEW-FICTIONAL-MATERIAL", name="Original fictional name")
    model_admin.save_model(request, material, None, False)
    created = AuditEvent.objects.get(resource_identifier=str(material.public_id))
    assert material.status == "DRAFT"
    assert created.actor_id == request.user.pk
    material.name = "Changed fictional name"
    model_admin.save_model(request, material, None, True)
    changed = AuditEvent.objects.get(
        resource_identifier=str(material.public_id), action="admin.change"
    )
    assert changed.changes == {
        "name": {"before": "Original fictional name", "after": "Changed fictional name"}
    }
    created.refresh_from_db()
    assert created.changes["name"]["after"] == "Original fictional name"


def test_reference_admin_denies_activation_deletion_and_unprivileged_maintenance(catalogue):
    model_admin = MaterialAdmin(Material, AdminSite())
    request = admin_request(catalogue["actor"])
    material = catalogue["material"]
    material.status = "ACTIVE"
    with pytest.raises(PermissionDenied):
        model_admin.save_model(request, material, None, True)
    with pytest.raises(PermissionDenied):
        model_admin.save_model(
            request, Material(code="UNAPPROVED", name="Fictional", status="ACTIVE"), None, False
        )
    assert not model_admin.has_delete_permission(request, material)
    with pytest.raises(PermissionDenied):
        model_admin.delete_model(request, material)
    with pytest.raises(PermissionDenied):
        model_admin.delete_queryset(request, Material.objects.filter(pk=material.pk))
    material.refresh_from_db()
    assert material.status == "DRAFT"
    actor = StaffUser.objects.create_user(username="fictional-unprivileged-editor", is_staff=True)
    material.name = "Unauthorized name"
    with pytest.raises(PermissionDenied):
        model_admin.save_model(admin_request(actor), material, None, True)
    material.refresh_from_db()
    assert material.name == "Fictional material"
    assert not AuditEvent.objects.exists()


def test_reference_admin_audit_failure_rolls_back_the_edit(catalogue):
    material = catalogue["material"]
    material.name = "Unrecorded edit"
    model_admin = MaterialAdmin(Material, AdminSite())
    with (
        patch("common.admin.record_model_change", side_effect=RuntimeError("Fictional failure")),
        pytest.raises(RuntimeError, match="Fictional failure"),
    ):
        model_admin.save_model(admin_request(catalogue["actor"]), material, None, True)
    material.refresh_from_db()
    assert material.name == "Fictional material"
    assert not AuditEvent.objects.exists()


def test_reference_admin_post_cannot_forge_status_or_existing_identity(catalogue, client):
    client.force_login(catalogue["actor"])
    material = catalogue["material"]
    response = client.post(
        reverse("admin:catalog_material_change", args=[material.pk]),
        {
            "code": "REPLACEMENT-CODE",
            "name": "Audited name",
            "status": "ACTIVE",
            "revision": material.updated_at.isoformat(),
            "_save": "Save",
        },
    )
    assert response.status_code == 302
    material.refresh_from_db()
    assert (material.code, material.name, material.status) == (
        "IMPORT-MATERIAL",
        "Audited name",
        "DRAFT",
    )
    assert AuditEvent.objects.get(resource_identifier=str(material.public_id)).changes == {
        "name": {"before": "Fictional material", "after": "Audited name"}
    }


def upload_form_values(**changes):
    return {
        "kind": "VARIANTS",
        "file": SimpleUploadedFile(
            "fictional-catalogue.csv", content("VARIANTS", [variant_row()]), content_type="text/csv"
        ),
        "idempotency_key": str(uuid.uuid4()),
        "reason": "Fictional Admin import",
        "operation": "apply",
        **changes,
    }


def test_reference_admin_rejects_stale_form_without_overwriting_newer_values(catalogue, client):
    client.force_login(catalogue["actor"])
    material = catalogue["material"]
    path = reverse("admin:catalog_material_change", args=[material.pk])
    response = client.get(path)
    revision = response.context["adminform"].form.initial["revision"].isoformat()
    material.name = "Newer administrator edit"
    material.save()
    response = client.post(path, {"name": "Stale edit", "revision": revision, "_save": "Save"})
    assert response.status_code == 200
    assert "This record changed" in str(response.context["adminform"].form.errors)
    material.refresh_from_db()
    assert material.name == "Newer administrator edit"
    assert not AuditEvent.objects.exists()


def test_least_privilege_csv_import_commits_and_redirects_to_an_authorized_page(catalogue, client):
    actor = importer()
    assert not actor.has_perm("catalog.view_csvimportbatch")
    assert not actor.has_perm("catalog.change_csvimportbatch")
    client.force_login(actor)
    response = client.post(
        reverse("admin:catalog_csvimportbatch_add"), upload_form_values(), follow=True
    )
    assert response.status_code == 200
    assert len(response.redirect_chain) == 1
    assert CSVImportBatch.objects.get().actor_id == actor.pk
    assert ProductVariant.objects.count() == 1


def test_csv_admin_preview_has_no_side_effects_and_unprivileged_posts_are_denied(catalogue, client):
    actor = importer()
    client.force_login(actor)
    before = record_counts()
    path = reverse("admin:catalog_csvimportbatch_add")
    response = client.post(path, upload_form_values(operation="preview"))
    assert response.status_code == 200
    assert response.context["preview"]["errors"] == []
    assert record_counts() == before
    actor.user_permissions.clear()
    response = client.post(path, upload_form_values())
    assert response.status_code == 403
    assert record_counts() == before


def test_csv_admin_requires_csrf_before_committing(catalogue):
    actor = importer()
    client = Client(enforce_csrf_checks=True)
    client.force_login(actor)
    path = reverse("admin:catalog_csvimportbatch_add")
    before = record_counts()
    assert client.post(path, upload_form_values()).status_code == 403
    assert record_counts() == before
    assert client.get(path).status_code == 200
    token = client.cookies["csrftoken"].value
    response = client.post(path, upload_form_values(), HTTP_X_CSRFTOKEN=token)
    assert response.status_code == 302
    assert CSVImportBatch.objects.count() == 1


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("kind", ["VARIANTS", "UNITS"])
def test_multirow_import_can_race_single_registration_without_duplicate_stock(catalogue, kind):
    rows = [variant_row(), variant_row(sku="second-import-sku")]
    apply_import(**request_values(catalogue, content=content("VARIANTS", rows)))
    variant = ProductVariant.objects.get(sku="second-import-sku")
    registration_id = uuid.uuid4()
    source = "fictional-evidence:parallel-intake"
    reason = "Fictional concurrent import"
    if kind == "UNITS":
        rows = [
            {
                "registration_id": str(uuid.uuid4()),
                "variant_sku": "opaque-import-sku",
                "hub_code": catalogue["hub"].code,
                "source_reference": source,
            },
            {
                "registration_id": str(registration_id),
                "variant_sku": variant.sku,
                "hub_code": catalogue["hub"].code,
                "source_reference": source,
            },
        ]
    values = request_values(catalogue, kind=kind, content=content(kind, rows), reason=reason)
    barrier = Barrier(2)

    def execute(is_import):
        close_old_connections()
        try:
            actor = StaffUser.objects.get(pk=catalogue["actor"].pk)
            with connection.cursor() as cursor:
                cursor.execute("SET lock_timeout = '5s'")
            barrier.wait(timeout=10)
            if is_import:
                return apply_import(**{**values, "actor": actor}).result
            return str(
                register_unit(
                    actor=actor,
                    variant=variant,
                    hub=catalogue["hub"],
                    idempotency_key=registration_id,
                    reason=reason,
                    source_reference=source,
                ).public_id
            )
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as workers:
        imported = workers.submit(execute, True)
        registered = workers.submit(execute, False)
        results, unit_id = imported.result(timeout=25), registered.result(timeout=25)
    assert ProductVariant.objects.count() == 2
    assert InventoryUnit.objects.count() == (2 if kind == "UNITS" else 1)
    assert InventoryMovement.objects.count() == InventoryUnit.objects.count()
    if kind == "UNITS":
        assert results[1]["id"] == unit_id
