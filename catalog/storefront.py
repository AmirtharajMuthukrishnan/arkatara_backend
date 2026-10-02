"""Audited publication and public catalogue projections, without booking commitments."""

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Exists, OuterRef, Prefetch, Q

from catalog.models import ProductVariant
from catalog.storefront_models import StorefrontPage, lock_page_tree, publication_write
from compliance.models import AuditEvent
from inventory.models import InventoryUnit


def visible_pages():
    return (
        StorefrontPage.objects.filter(status=StorefrontPage.Status.PUBLISHED)
        .filter(
            Q(kind="MATERIAL", material__status="ACTIVE")
            | Q(kind="CATEGORY", category__status="ACTIVE")
            | Q(kind="PRODUCT", product__status="ACTIVE", product__category__status="ACTIVE")
        )
        .select_related("material", "category", "product", "product__category")
    )


def change_publication(*, actor, page, publish: bool, reason):
    """Record the content decision; reference activation is a separate prerequisite.

    Refs: BUSINESS_RULES.md R-07, R-09, R-30; DECISIONS.md D-20.
    """
    action = "publish" if publish else "withdraw"
    if type(publish) is not bool:
        raise ValidationError("Select a publication action.")
    if not (
        actor
        and actor.is_authenticated
        and actor.is_active
        and actor.is_staff
        and actor.has_perm(f"catalog.{action}_storefrontpage")
    ):
        raise PermissionDenied("Publication requires the relevant staff permission.")
    if not isinstance(reason, str) or not reason.strip():
        raise ValidationError("Record the reason for this content decision.")
    with transaction.atomic():
        lock_page_tree()
        locked = StorefrontPage.objects.select_for_update().get(pk=page.pk)
        target = StorefrontPage.Status.PUBLISHED if publish else StorefrontPage.Status.WITHDRAWN
        if locked.status == target:
            return locked
        if not publish and locked.status != StorefrontPage.Status.PUBLISHED:
            raise ValidationError("Only a published page may be withdrawn.")
        if publish:
            linked = getattr(locked, locked.kind.lower())
            if linked.status != "ACTIVE" or (
                locked.kind == StorefrontPage.Kind.PRODUCT and linked.category.status != "ACTIVE"
            ):
                raise ValidationError("Publish only content whose catalogue references are active.")
        previous = locked.status
        locked.status = target
        with publication_write():
            locked.save(update_fields=["status", "updated_at"])
        AuditEvent.objects.create(
            actor=actor,
            action=f"storefront.{action}",
            resource_type="catalog.storefrontpage",
            resource_identifier=str(locked.public_id),
            reason=reason.strip(),
            changes={"status": {"before": previous, "after": target}},
        )
        return locked


def reference(row):
    return {"id": str(row.public_id), "code": row.code, "name": row.name, "status": row.status}


def summary(page):
    return {
        "id": str(page.public_id),
        "kind": page.kind,
        "slug": page.slug,
        "path": page.path,
        "title": page.title,
        "description": page.description,
        "seo_title": page.seo_title,
        "seo_description": page.seo_description,
    }


def public_variants(market, material):
    variants = ProductVariant.objects.filter(
        status="ACTIVE",
        material__status="ACTIVE",
        product__status="ACTIVE",
        product__category__status="ACTIVE",
    ).filter(Q(purity__isnull=True) | Q(purity__status="ACTIVE"))
    if material is not None:
        variants = variants.filter(material=material)
    available = InventoryUnit.objects.filter(
        variant_id=OuterRef("pk"),
        status="AVAILABLE",
        custody="HUB",
        hub__status="ACTIVE",
        hub__market_id=market.pk if market else None,
        hub__market__status="ACTIVE",
    )
    return variants.select_related("material", "purity").annotate(at_hub=Exists(available))


def availability(variants, market, material):
    # An inspected piece at a hub is not a serviceable/eligible booking promise.
    # Refs: BUSINESS_RULES.md R-07, R-09; DECISIONS.md D-20, BD-08, BD-13.
    if market is None:
        state = "UNCONFIGURED"
    elif market.status == "COMING_SOON" or (material and material.status == "COMING_SOON"):
        state = "COMING_SOON"
    elif market.status != "ACTIVE":
        state = "UNAVAILABLE"
    else:
        state = "AVAILABLE_AT_HUB" if any(variant.at_hub for variant in variants) else "UNAVAILABLE"
    return {"state": state, "bookable": False}


def product_pages(market, material, collection=None):
    pages = visible_pages().filter(kind=StorefrontPage.Kind.PRODUCT)
    variants = public_variants(market, material)
    if material is not None:
        pages = pages.filter(product_id__in=variants.values("product_id"))
    if collection and collection.kind == StorefrontPage.Kind.CATEGORY:
        pages = pages.filter(product__category_id=collection.category_id)
    elif collection and collection.kind == StorefrontPage.Kind.PRODUCT:
        pages = pages.filter(pk=collection.pk)
    return pages.prefetch_related(
        Prefetch("product__variants", queryset=variants, to_attr="public_variants")
    ).order_by("-is_featured", "sort_order", "slug")


def product_summary(page, market, material):
    variants = page.product.public_variants
    return {
        **summary(page),
        "product_id": str(page.product.public_id),
        "material_names": sorted({variant.material.name for variant in variants}),
        "availability": availability(variants, market, material),
        "price": {"state": "UNCONFIGURED"},
        "media": [],
    }


def product_detail(page, market, material):
    product = page.product
    variants = list(public_variants(market, material).filter(product=product))
    return {
        "id": str(product.public_id),
        "name": page.title,
        "description": page.description,
        "variants": [
            {
                "id": str(variant.public_id),
                "sku": variant.sku,
                "material": reference(variant.material),
                "purity": reference(variant.purity) if variant.purity_id else None,
                "size_label": variant.size_label,
                # Arbitrary internal JSON has no reviewed public-field contract.
                # Refs: BUSINESS_RULES.md R-11; DECISIONS.md D-20.
                "specification": {},
                "availability": availability([variant], market, material),
            }
            for variant in variants
        ],
        "availability": availability(variants, market, material),
        # Private draft assets and prices are deliberately absent from the projection.
        # Refs: BUSINESS_RULES.md R-07, R-11; DECISIONS.md D-20, BD-04, BD-15.
        "price": {"state": "UNCONFIGURED"},
        "media": [],
    }


def breadcrumbs(page):
    ancestors = []
    current = page.parent_id
    seen = {page.pk}
    while current is not None and current not in seen:
        seen.add(current)
        public = visible_pages().filter(pk=current).first()
        ancestor = public or StorefrontPage.objects.filter(pk=current).only("parent_id").first()
        if ancestor is None:
            break
        if public is not None:
            ancestors.append({"title": public.title, "path": public.path})
        current = ancestor.parent_id
    return [
        {"title": "Home", "path": "/"},
        *reversed(ancestors),
        {"title": page.title, "path": page.path},
    ]
