"""Public catalogue observations use fictional content and reviewed test procedures."""

import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from unittest.mock import patch

import pytest
from django.contrib.admin import AdminSite
from django.contrib.auth.models import Permission
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import DatabaseError, close_old_connections, connection
from django.db.migrations.executor import MigrationExecutor
from django.test import Client, RequestFactory
from django.urls import reverse
from django.utils import timezone

from accounts.models import StaffUser
from catalog.models import Category, Material, PriceRevision, Product, ProductVariant, Purity
from catalog.storefront import change_publication
from catalog.storefront_admin import StorefrontPageAdmin
from catalog.storefront_models import StorefrontPage
from compliance.models import AuditEvent, BusinessConfiguration
from inventory.services import receive_unit, record_qc, register_unit
from markets.models import Hub, Market

pytestmark = pytest.mark.django_db


@pytest.fixture
def graph():
    actor = StaffUser.objects.create_superuser(username="fictional-content-owner", password="test")
    market = Market.objects.create(
        code="CATALOGUE-A", name="Fictional first city", country_code="IN", status="ACTIVE"
    )
    other_market = Market.objects.create(
        code="CATALOGUE-B", name="Fictional second city", country_code="IN", status="ACTIVE"
    )
    hub = Hub.objects.create(
        code="CATALOGUE-HUB", name="Fictional hub", market=market, status="ACTIVE"
    )
    material = Material.objects.create(
        code="CATALOGUE-METAL", name="Fictional metal", status="ACTIVE"
    )
    purity = Purity.objects.create(
        code="CATALOGUE-PURITY", name="Fictional grade", material=material, status="ACTIVE"
    )
    category = Category.objects.create(
        code="CATALOGUE-KIND", name="Fictional category", status="ACTIVE"
    )
    product = Product.objects.create(
        code="CATALOGUE-DESIGN",
        name="Fictional design",
        category=category,
        description="Verified fictional description",
        status="ACTIVE",
    )
    variant = ProductVariant.objects.create(
        product=product,
        material=material,
        purity=purity,
        sku="fictional-opaque-sku",
        size_label="Fictional size",
        specification={"finish": "Fictional finish"},
        status="ACTIVE",
    )
    return {
        "actor": actor,
        "market": market,
        "other_market": other_market,
        "hub": hub,
        "material": material,
        "purity": purity,
        "category": category,
        "product": product,
        "variant": variant,
    }


def page_for(graph, *, kind="PRODUCT", slug="fictional-design", published=False, **values):
    page = StorefrontPage.objects.create(
        kind=kind,
        slug=slug,
        title=f"Reviewed {slug}",
        description="Reviewed fictional page copy",
        **{kind.lower(): graph[kind.lower()]},
        **values,
    )
    if published:
        return change_publication(
            actor=graph["actor"], page=page, publish=True, reason="Fictional editorial review"
        )
    return page


def detail_path(page):
    return reverse("api_v1:storefront-page", kwargs={"slug": page.slug})


def query(graph, **changes):
    return {"market": graph["market"].code, "material": graph["material"].code, **changes}


def inspect_stock(graph, *, passed=True, hub=None):
    hub = hub or graph["hub"]
    instant = timezone.now() - timedelta(days=1)
    for key in ("receiving-procedure-reference", "qc-procedure-reference"):
        BusinessConfiguration.objects.get_or_create(
            namespace="inventory",
            key=key,
            scope={"hub": str(hub.public_id)},
            version=1,
            defaults={
                "value": "fictional-procedure:catalogue-test",
                "status": "ACTIVE",
                "effective_from": instant,
                "approval_reference": "fictional-approval:catalogue-test",
                "approval_recorded_by": graph["actor"],
                "approved_at": instant,
            },
        )
    values = {
        "actor": graph["actor"],
        "reason": "Fictional stock observation",
        "source_reference": "fictional-evidence:catalogue-test",
    }
    unit = register_unit(variant=graph["variant"], hub=hub, idempotency_key=uuid.uuid4(), **values)
    receive_unit(unit=unit, idempotency_key=uuid.uuid4(), **values)
    record_qc(unit=unit, passed=passed, idempotency_key=uuid.uuid4(), **values)
    return unit


def staff(*permissions):
    actor = StaffUser.objects.create_user(
        username=f"fictional-editor-{uuid.uuid4()}", is_staff=True
    )
    actor.user_permissions.add(
        *Permission.objects.filter(content_type__app_label="catalog", codename__in=permissions)
    )
    return actor


def admin_request(actor):
    request = RequestFactory().post("/")
    request.user = actor
    return request


def test_drafts_withdrawn_and_inactive_references_never_leak(graph, client):
    page = page_for(graph)
    path = detail_path(page)
    assert client.get(path, query(graph)).status_code == 404
    assert client.get(reverse("api_v1:catalogue"), query(graph)).json()["products"] == []
    page = change_publication(
        actor=graph["actor"], page=page, publish=True, reason="Fictional approval"
    )
    assert client.get(path, query(graph)).status_code == 200
    graph["category"].status = "INACTIVE"
    graph["category"].save()
    assert client.get(path, query(graph)).status_code == 404
    graph["category"].status = "ACTIVE"
    graph["category"].save()
    change_publication(
        actor=graph["actor"], page=page, publish=False, reason="Fictional withdrawal"
    )
    assert client.get(path, query(graph)).status_code == 404
    assert client.get(reverse("api_v1:catalogue"), query(graph)).json()["products"] == []


@pytest.mark.parametrize("field", ["product", "category"])
def test_publication_requires_active_product_and_category(graph, field):
    page = page_for(graph)
    graph[field].status = "COMING_SOON"
    graph[field].save()
    with pytest.raises(ValidationError, match="references are active"):
        change_publication(actor=graph["actor"], page=page, publish=True, reason="Fictional review")
    page.refresh_from_db()
    assert page.status == "DRAFT"
    assert not AuditEvent.objects.filter(action="storefront.publish").exists()


def test_publication_permission_reason_and_audit_are_atomic(graph):
    page = page_for(graph)
    actor = staff("change_storefrontpage")
    with pytest.raises(PermissionDenied):
        change_publication(actor=actor, page=page, publish=True, reason="Fictional review")
    with pytest.raises(ValidationError, match="reason"):
        change_publication(actor=graph["actor"], page=page, publish=True, reason=" ")
    with (
        patch(
            "catalog.storefront.AuditEvent.objects.create",
            side_effect=RuntimeError("audit unavailable"),
        ),
        pytest.raises(RuntimeError),
    ):
        change_publication(actor=graph["actor"], page=page, publish=True, reason="Fictional review")
    page.refresh_from_db()
    assert page.status == "DRAFT"
    actor = staff("publish_storefrontpage")
    published = change_publication(actor=actor, page=page, publish=True, reason="Fictional review")
    change_publication(actor=actor, page=page, publish=True, reason="Fictional retry")
    event = AuditEvent.objects.get(action="storefront.publish")
    assert event.actor == actor
    assert event.resource_identifier == str(page.public_id)
    assert event.reason == "Fictional review"
    assert event.changes == {"status": {"before": "DRAFT", "after": "PUBLISHED"}}
    with pytest.raises(PermissionDenied):
        change_publication(actor=actor, page=published, publish=False, reason="Fictional review")


def test_content_identity_and_workflow_cannot_be_bypassed(graph):
    page = page_for(graph)
    page.status = "PUBLISHED"
    with pytest.raises(ValidationError, match="publish or withdraw"):
        page.save()
    page.refresh_from_db()
    page.slug = "replacement-url"
    with pytest.raises(ValidationError, match="cannot be reassigned"):
        page.save()
    page.refresh_from_db()
    with pytest.raises(TypeError):
        StorefrontPage.objects.filter(pk=page.pk).update(status="PUBLISHED")
    with pytest.raises(TypeError):
        page.delete()
    with pytest.raises(TypeError):
        StorefrontPage.objects.filter(pk=page.pk).delete()
    published = change_publication(
        actor=graph["actor"], page=page, publish=True, reason="Fictional review"
    )
    published.title = "Unreviewed replacement"
    with pytest.raises(ValidationError, match="Withdraw"):
        published.save()
    withdrawn = change_publication(
        actor=graph["actor"], page=page, publish=False, reason="Fictional edit needed"
    )
    withdrawn.title = "Reviewed replacement"
    withdrawn.save()


@pytest.mark.parametrize(
    "slug", ["Uppercase", "two--hyphens", "_underscore", "two words", "trailing-"]
)
def test_slug_has_one_canonical_lowercase_spelling(graph, slug):
    with pytest.raises(ValidationError):
        page_for(graph, slug=slug)


def test_subject_uniqueness_and_collection_hierarchy(graph):
    category = page_for(graph, kind="CATEGORY", slug="fictional-category")
    material = page_for(graph, kind="MATERIAL", slug="fictional-material", parent=category)
    product = page_for(graph, parent=material)
    with pytest.raises(ValidationError):
        page_for(graph, slug="duplicate-subject")
    category.parent = material
    with pytest.raises(ValidationError, match="cycle"):
        category.save()
    material.parent = product
    with pytest.raises(ValidationError, match="collection"):
        material.save()
    category.refresh_from_db()
    category.material = graph["material"]
    with pytest.raises(ValidationError, match="subject"):
        category.full_clean()


def test_names_can_change_without_changing_canonical_routes(graph, client):
    category = page_for(graph, kind="CATEGORY", slug="fictional-category", published=True)
    page = page_for(graph, parent=category, published=True)
    graph["product"].name = "New fictional design name"
    graph["product"].save()
    payload = client.get(detail_path(page), query(graph, market=graph["other_market"].code)).json()
    assert payload["page"]["path"] == "/products/fictional-design"
    assert payload["product"]["name"] == page.title
    assert payload["product"]["description"] == page.description
    assert payload["breadcrumbs"] == [
        {"title": "Home", "path": "/"},
        {"title": category.title, "path": category.path},
        {"title": page.title, "path": page.path},
    ]
    change_publication(
        actor=graph["actor"], page=category, publish=False, reason="Fictional withdrawal"
    )
    payload = client.get(detail_path(page), query(graph)).json()
    assert len(payload["breadcrumbs"]) == 2
    assert category.slug not in str(payload["breadcrumbs"])


def test_stock_requires_qc_and_stays_in_selected_active_market(graph, client):
    page = page_for(graph, published=True)
    assert client.get(detail_path(page), query(graph)).json()["product"]["availability"] == {
        "state": "UNAVAILABLE",
        "bookable": False,
    }
    inspect_stock(graph)
    payload = client.get(detail_path(page), query(graph)).json()
    assert payload["product"]["availability"] == {"state": "AVAILABLE_AT_HUB", "bookable": False}
    assert payload["product"]["variants"][0]["availability"]["state"] == "AVAILABLE_AT_HUB"
    other = client.get(detail_path(page), query(graph, market=graph["other_market"].code)).json()
    assert other["product"]["availability"] == {"state": "UNAVAILABLE", "bookable": False}
    assert payload["page"]["path"] == other["page"]["path"]
    graph["hub"].status = "INACTIVE"
    graph["hub"].save()
    assert (
        client.get(detail_path(page), query(graph)).json()["product"]["availability"]["state"]
        == "UNAVAILABLE"
    )


def test_quarantined_stock_never_appears_available(graph, client):
    page = page_for(graph, published=True)
    inspect_stock(graph, passed=False)
    assert (
        client.get(detail_path(page), query(graph)).json()["product"]["availability"]["state"]
        == "UNAVAILABLE"
    )


def test_future_city_and_material_need_no_special_case_or_booking_permission(graph, client):
    page = page_for(graph, published=True)
    future_market = Market.objects.create(
        code="FUTURE-CITY", name="Arbitrary future city", country_code="IN", status="COMING_SOON"
    )
    future_material = Material.objects.create(
        code="FUTURE-METAL", name="Arbitrary future metal", status="COMING_SOON"
    )
    payload = client.get(
        detail_path(page), query(graph, market=future_market.code, material=future_material.code)
    ).json()
    assert payload["selected_market"]["name"] == "Arbitrary future city"
    assert payload["selected_material"]["name"] == "Arbitrary future metal"
    assert payload["product"]["availability"] == {"state": "COMING_SOON", "bookable": False}
    assert payload["product"]["variants"] == []
    future_market.status = "ACTIVE"
    future_market.save()
    future_material.status = "ACTIVE"
    future_material.save()
    ProductVariant.objects.create(
        product=graph["product"],
        material=future_material,
        sku="future-fictional-sku",
        status="ACTIVE",
    )
    payload = client.get(
        detail_path(page), query(graph, market=future_market.code, material=future_material.code)
    ).json()
    assert payload["product"]["availability"] == {"state": "UNAVAILABLE", "bookable": False}
    assert payload["product"]["variants"][0]["material"]["code"] == future_material.code


@pytest.mark.parametrize("field", ["variant", "material", "purity"])
def test_hidden_variant_references_never_claim_available(graph, client, field):
    page = page_for(graph, published=True)
    inspect_stock(graph)
    graph[field].status = "INACTIVE"
    graph[field].save()
    selection = query(graph)
    if field == "material":
        selection.pop("material")
    payload = client.get(detail_path(page), selection).json()
    assert payload["product"]["variants"] == []
    assert payload["product"]["availability"]["state"] != "AVAILABLE_AT_HUB"


def test_no_active_market_yields_unconfigured_state_not_implicit_fulfilment(graph, client):
    page = page_for(graph, published=True)
    for market in Market.objects.filter(status="ACTIVE"):
        market.status = "INACTIVE"
        market.save()
    payload = client.get(detail_path(page), {"material": graph["material"].code}).json()
    assert payload["selected_market"] is None
    assert payload["product"]["availability"] == {"state": "UNCONFIGURED", "bookable": False}


def test_draft_prices_and_private_identity_are_not_public_projections(graph, client):
    page = page_for(graph, published=True)
    graph["variant"].specification = {"supplier_note": "fictional-private-supplier-note"}
    graph["variant"].save()
    PriceRevision.objects.create(
        variant=graph["variant"],
        revision=1,
        mode="FIXED",
        currency="INR",
        fixed_amount="912345.123456",
    )
    unit = inspect_stock(graph)
    response = client.get(detail_path(page), query(graph))
    payload = response.json()
    assert payload["product"]["price"] == {"state": "UNCONFIGURED"}
    assert payload["product"]["media"] == []
    assert payload["product"]["variants"][0]["specification"] == {}
    assert payload["products"][0]["price"] == {"state": "UNCONFIGURED"}
    assert payload["products"][0]["media"] == []
    text = response.content.decode()
    for secret in (
        "fictional-private-supplier-note",
        "supplier_note",
        "912345.123456",
        str(unit.public_id),
        str(graph["hub"].public_id),
        "object_key",
        "storage_backend",
        "approval_reference",
    ):
        assert secret not in text


def test_collection_filters_use_identity_and_material_page_controls_its_context(graph, client):
    page_for(graph, published=True)
    material_page = page_for(graph, kind="MATERIAL", slug="fictional-metal", published=True)
    category_page = page_for(graph, kind="CATEGORY", slug="fictional-category", published=True)
    other_category = Category.objects.create(
        code="OTHER-CATEGORY", name="Other fictional category", status="ACTIVE"
    )
    other_product = Product.objects.create(
        code="OTHER-PRODUCT",
        name="Other fictional design",
        category=other_category,
        status="ACTIVE",
    )
    ProductVariant.objects.create(
        product=other_product,
        material=graph["material"],
        sku="other-fictional-sku",
        status="ACTIVE",
    )
    page_for({**graph, "product": other_product}, slug="other-fictional-design", published=True)
    payload = client.get(detail_path(category_page), query(graph)).json()
    assert [product["slug"] for product in payload["products"]] == ["fictional-design"]
    assert payload["product"] is None
    payload = client.get(detail_path(material_page), query(graph, material="SILVER")).json()
    assert payload["selected_material"]["code"] == graph["material"].code
    assert len(payload["products"]) == 2


@pytest.mark.parametrize(
    "suffix",
    [
        "page=0",
        "page=-1",
        "page=1.5",
        "page=01",
        "page=%201",
        "page=",
        "page=1&page=2",
        "unknown=value",
        "market=missing",
        "market=CATALOGUE-A&market=CATALOGUE-A",
        "material=missing",
        "page=9999999999",
    ],
)
def test_invalid_query_has_consistent_400_envelope(graph, client, suffix):
    response = client.get(f"{reverse('api_v1:catalogue')}?{suffix}")
    assert response.status_code == 400
    assert response.json()["error"]["status_code"] == 400


def test_draft_selector_codes_are_rejected(graph, client):
    material = Material.objects.create(code="DRAFT-METAL", name="Unpublished metal")
    response = client.get(reverse("api_v1:catalogue"), query(graph, material=material.code))
    assert response.status_code == 400


def test_pagination_is_stable_bounded_and_empty_page_one_is_valid(graph, client):
    endpoint = reverse("api_v1:catalogue")
    empty = client.get(endpoint, query(graph)).json()
    assert empty["pagination"] == {"page": 1, "page_size": 12, "total_items": 0, "total_pages": 1}
    assert client.get(endpoint, query(graph, page=2)).status_code == 404
    for number in range(13):
        product = Product.objects.create(
            code=f"PAGE-{number}",
            name=f"Fictional design {number}",
            category=graph["category"],
            status="ACTIVE",
        )
        ProductVariant.objects.create(
            product=product,
            material=graph["material"],
            sku=f"fictional-page-{number}",
            status="ACTIVE",
        )
        page_for(
            {**graph, "product": product}, slug=f"fictional-design-{number:02}", published=True
        )
    first = client.get(endpoint, query(graph)).json()
    second = client.get(endpoint, query(graph, page=2)).json()
    assert len(first["products"]) == 12
    assert len(second["products"]) == 1
    assert first["pagination"]["total_items"] == 13
    assert first["pagination"]["total_pages"] == 2
    assert not {page["id"] for page in first["products"]} & {
        page["id"] for page in second["products"]
    }
    assert client.get(endpoint, query(graph, page=3)).status_code == 404


def test_database_outage_is_not_misrepresented_as_missing_content(graph):
    page = page_for(graph, published=True)
    client = Client(raise_request_exception=False)
    with patch(
        "catalog.storefront_api.visible_pages", side_effect=DatabaseError("Fictional outage")
    ):
        assert client.get(detail_path(page), query(graph)).status_code == 500


def test_product_detail_is_not_paginated_and_does_not_include_unrelated_products(graph, client):
    page = page_for(graph, published=True)
    other_product = Product.objects.create(
        code="UNRELATED",
        name="Unrelated fictional design",
        category=graph["category"],
        status="ACTIVE",
    )
    ProductVariant.objects.create(
        product=other_product, material=graph["material"], sku="unrelated-sku", status="ACTIVE"
    )
    page_for({**graph, "product": other_product}, slug="unrelated-design", published=True)
    response = client.get(detail_path(page), query(graph, page=999))
    assert response.status_code == 200
    payload = response.json()
    assert payload["pagination"] == {"page": 1, "page_size": 12, "total_items": 1, "total_pages": 1}
    assert [product["slug"] for product in payload["products"]] == [page.slug]


def test_product_default_material_comes_from_its_variants_but_explicit_selection_is_retained(
    graph, client
):
    page = page_for(graph, published=True)
    unrelated = Material.objects.create(
        code="AAA-UNRELATED", name="Unrelated fictional metal", status="ACTIVE"
    )
    implicit = client.get(detail_path(page), {"market": graph["market"].code}).json()
    assert implicit["selected_material"]["code"] == graph["material"].code
    assert len(implicit["product"]["variants"]) == 1
    explicit = client.get(detail_path(page), query(graph, material=unrelated.code)).json()
    assert explicit["selected_material"]["code"] == unrelated.code
    assert explicit["product"]["variants"] == []


def test_draft_admin_edit_is_audited_and_published_content_is_read_only(graph):
    page = page_for(graph)
    model_admin = StorefrontPageAdmin(StorefrontPage, AdminSite())
    request = admin_request(graph["actor"])
    page.title = "Reviewed replacement copy"
    model_admin.save_model(request, page, None, True)
    assert AuditEvent.objects.get(action="admin.change").changes["title"]["after"] == page.title
    page = change_publication(
        actor=graph["actor"], page=page, publish=True, reason="Fictional review"
    )
    assert not model_admin.has_change_permission(request, page)
    assert not model_admin.has_delete_permission(request, page)
    with pytest.raises(PermissionDenied):
        model_admin.save_model(request, page, None, True)


def test_admin_publish_requires_reason_custom_permission_and_csrf(graph):
    page = page_for(graph)
    actor = staff("view_storefrontpage", "publish_storefrontpage")
    client = Client(enforce_csrf_checks=True)
    client.force_login(actor)
    path = reverse("admin:catalog_storefrontpage_changelist")
    values = {
        "action": "publish_content",
        "_selected_action": [page.pk],
        "publication_reason": "Fictional editorial approval",
        "index": "0",
    }
    assert client.post(path, values).status_code == 403
    assert client.get(path).status_code == 200
    token = client.cookies["csrftoken"].value
    assert (
        client.post(path, {**values, "publication_reason": ""}, HTTP_X_CSRFTOKEN=token).status_code
        == 302
    )
    page.refresh_from_db()
    assert page.status == "DRAFT"
    assert client.post(path, values, HTTP_X_CSRFTOKEN=token).status_code == 302
    page.refresh_from_db()
    assert page.status == "PUBLISHED"
    assert AuditEvent.objects.get(action="storefront.publish").actor == actor


@pytest.mark.django_db(transaction=True)
def test_concurrent_publication_creates_one_transition_and_audit(graph):
    page = page_for(graph)
    barrier = Barrier(2)

    def publish():
        close_old_connections()
        try:
            actor = StaffUser.objects.get(pk=graph["actor"].pk)
            with connection.cursor() as cursor:
                cursor.execute("SET lock_timeout = '5s'")
            barrier.wait(timeout=10)
            return change_publication(
                actor=actor, page=page, publish=True, reason="Fictional concurrent review"
            ).status
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as workers:
        results = list(workers.map(lambda _: publish(), range(2)))
    assert results == ["PUBLISHED", "PUBLISHED"]
    assert AuditEvent.objects.filter(action="storefront.publish").count() == 1


@pytest.mark.django_db(transaction=True)
def test_storefront_migration_preserves_existing_catalogue_without_publishing_it():
    leaves = MigrationExecutor(connection).loader.graph.leaf_nodes()
    try:
        executor = MigrationExecutor(connection)
        previous = [("catalog", "0005_import_history_guard")]
        executor.migrate(previous)
        apps = executor.loader.project_state(previous).apps
        category = apps.get_model("catalog", "Category").objects.create(
            code="MIGRATION-PAGE-KIND", name="Fictional migration category", status="ACTIVE"
        )
        product = apps.get_model("catalog", "Product").objects.create(
            code="MIGRATION-PAGE-DESIGN",
            name="Fictional migration design",
            category=category,
            status="ACTIVE",
        )
        identity = product.public_id
        MigrationExecutor(connection).migrate(leaves)
        assert Product.objects.get(pk=product.pk).public_id == identity
        assert not StorefrontPage.objects.exists()
        assert not AuditEvent.objects.filter(action="storefront.publish").exists()
    finally:
        MigrationExecutor(connection).migrate(leaves)
