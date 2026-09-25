"""Scenario reference data exercises identity without approving live offerings."""

import uuid
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from time import monotonic, sleep

import pytest
from django.core.exceptions import ValidationError
from django.db import close_old_connections, connection, transaction
from django.db.models.deletion import ProtectedError

from catalog.models import Category, Material, Product, ProductVariant, Purity
from common.reference import ReferenceStatus
from inventory.models import InventoryUnit
from markets.models import Hub, Market

pytestmark = pytest.mark.django_db


@pytest.fixture
def reference_graph():
    market = Market.objects.create(code="TEST-CITY-A", name="Scenario city A", country_code="IN")
    second_market = Market.objects.create(
        code="TEST-CITY-B", name="Scenario city B", country_code="IN"
    )
    hub = Hub.objects.create(code="TEST-HUB-A", name="Scenario hub A", market=market)
    second_hub = Hub.objects.create(code="TEST-HUB-B", name="Scenario hub B", market=second_market)
    material = Material.objects.create(code="TEST-METAL-A", name="Scenario metal A")
    second_material = Material.objects.create(code="TEST-METAL-B", name="Scenario metal B")
    purity = Purity.objects.create(code="TEST-PURITY", name="Scenario purity", material=material)
    category = Category.objects.create(code="TEST-CATEGORY", name="Scenario category")
    product = Product.objects.create(code="TEST-DESIGN", name="Scenario design", category=category)
    variant = ProductVariant.objects.create(
        product=product,
        material=material,
        purity=purity,
        sku="opaque-scenario-sku",
        size_label="Scenario size",
        specification={"finish": "scenario finish"},
    )
    return {
        "market": market,
        "second_market": second_market,
        "hub": hub,
        "second_hub": second_hub,
        "material": material,
        "second_material": second_material,
        "purity": purity,
        "category": category,
        "product": product,
        "variant": variant,
    }


def test_design_variant_and_physical_pieces_have_separate_identity(reference_graph):
    graph = reference_graph
    first = InventoryUnit.objects.create(variant=graph["variant"], hub=graph["hub"])
    second = InventoryUnit.objects.create(variant=graph["variant"], hub=graph["hub"])

    assert (
        len(
            {
                graph["product"].public_id,
                graph["variant"].public_id,
                first.public_id,
                second.public_id,
            }
        )
        == 4
    )
    assert first.status == second.status == InventoryUnit.Status.DRAFT
    assert first.variant.product == graph["product"]
    assert graph["variant"].inventory_units.count() == 2
    assert graph["category"].status == ReferenceStatus.DRAFT
    assert not hasattr(graph["category"], "trial_eligible")


def test_same_variant_can_have_distinct_units_in_different_market_hubs(reference_graph):
    graph = reference_graph
    first = InventoryUnit.objects.create(variant=graph["variant"], hub=graph["hub"])
    second = InventoryUnit.objects.create(variant=graph["variant"], hub=graph["second_hub"])

    assert list(InventoryUnit.objects.filter(hub__market=graph["market"])) == [first]
    assert list(InventoryUnit.objects.filter(hub__market=graph["second_market"])) == [second]
    assert graph["variant"].inventory_units.count() == 2


def test_purity_must_match_variant_material_on_save(reference_graph):
    graph = reference_graph
    with pytest.raises(ValidationError, match="Purity must belong"):
        ProductVariant.objects.create(
            product=graph["product"],
            material=graph["second_material"],
            purity=graph["purity"],
            sku="scenario-invalid-purity",
        )
    assert not ProductVariant.objects.filter(sku="scenario-invalid-purity").exists()


def test_purity_code_is_unique_within_material_not_across_all_materials(reference_graph):
    graph = reference_graph
    other = Purity.objects.create(
        code=graph["purity"].code,
        name="Different material standard",
        material=graph["second_material"],
    )
    assert other.material_id != graph["purity"].material_id
    with pytest.raises(ValidationError, match="already exists"):
        Purity.objects.create(code=other.code, name="Duplicate", material=other.material)


@pytest.mark.parametrize("value", [[], "description", 1, True])
def test_variant_specification_requires_object(reference_graph, value):
    variant = reference_graph["variant"]
    variant.specification = value
    with pytest.raises(ValidationError, match="JSON object"):
        variant.save()


@pytest.mark.parametrize("field", ["code", "public_id"])
def test_reference_identity_stays_stable(reference_graph, field):
    material = reference_graph["material"]
    setattr(material, field, uuid.uuid4() if field == "public_id" else "NEW-CODE")
    with pytest.raises(ValidationError, match="identity cannot"):
        material.save()


def test_hub_cannot_be_reassigned_to_different_market(reference_graph):
    graph = reference_graph
    graph["hub"].market = graph["second_market"]
    with pytest.raises(ValidationError, match="identity cannot"):
        graph["hub"].save()


def test_purity_cannot_be_reassigned_to_different_material(reference_graph):
    graph = reference_graph
    graph["purity"].material = graph["second_material"]
    with pytest.raises(ValidationError, match="identity cannot"):
        graph["purity"].save()


@pytest.mark.parametrize(
    "field,value", [("size_label", "Changed size"), ("specification", {"finish": "Changed"})]
)
def test_used_variant_cannot_change_physical_definition(reference_graph, field, value):
    graph = reference_graph
    InventoryUnit.objects.create(variant=graph["variant"], hub=graph["hub"])
    setattr(graph["variant"], field, value)
    with pytest.raises(ValidationError, match="recorded units or prices cannot be redefined"):
        graph["variant"].save()


def test_unused_variant_can_correct_specification(reference_graph):
    variant = reference_graph["variant"]
    variant.size_label = "Corrected scenario size"
    variant.save()
    variant.refresh_from_db()
    assert variant.size_label == "Corrected scenario size"


def test_used_design_cannot_change_category(reference_graph):
    graph = reference_graph
    InventoryUnit.objects.create(variant=graph["variant"], hub=graph["hub"])
    graph["product"].category = Category.objects.create(code="TEST-OTHER", name="Other scenario")
    with pytest.raises(ValidationError, match="cannot be recategorized"):
        graph["product"].save()


def test_display_name_and_activation_can_change_without_reassigning_identity(reference_graph):
    material = reference_graph["material"]
    original_id = material.public_id
    material.name = "Scenario renamed material"
    material.status = ReferenceStatus.ACTIVE
    material.save()
    material.refresh_from_db()
    assert material.public_id == original_id
    assert material.status == ReferenceStatus.ACTIVE


def test_units_cannot_be_reassigned_by_generic_save(reference_graph):
    graph = reference_graph
    unit = InventoryUnit.objects.create(variant=graph["variant"], hub=graph["hub"])
    unit.hub = graph["second_hub"]
    with pytest.raises(ValidationError, match="identity cannot"):
        unit.save()


def test_units_are_not_made_available_by_a_status_edit(reference_graph):
    graph = reference_graph
    unit = InventoryUnit.objects.create(variant=graph["variant"], hub=graph["hub"])
    unit.status = "AVAILABLE"
    with pytest.raises(ValidationError):
        unit.save()


def test_referenced_catalogue_and_hubs_cannot_be_deleted(reference_graph):
    graph = reference_graph
    InventoryUnit.objects.create(variant=graph["variant"], hub=graph["hub"])
    for record in (graph["variant"], graph["product"], graph["hub"], graph["material"]):
        with pytest.raises(ProtectedError):
            record.delete()


def test_individual_validation_cannot_be_skipped_with_bulk_mutations(reference_graph):
    graph = reference_graph
    with pytest.raises(TypeError, match="individual saves"):
        ProductVariant.objects.filter(pk=graph["variant"].pk).update(
            material=graph["second_material"]
        )
    with pytest.raises(TypeError, match="Bulk writes"):
        ProductVariant.objects.bulk_update([graph["variant"]], ["material"])
    with pytest.raises(TypeError, match="Bulk writes"):
        InventoryUnit.objects.bulk_create(
            [InventoryUnit(variant=graph["variant"], hub=graph["hub"])]
        )


def test_new_instance_with_existing_pk_cannot_overwrite_identity(reference_graph):
    existing = reference_graph["material"]
    replacement = Material(pk=existing.pk, code="TEST-REPLACEMENT", name="Scenario replacement")
    with pytest.raises(ValidationError, match="already exists"):
        replacement.save()
    existing.refresh_from_db()
    assert existing.code == "TEST-METAL-A"


def test_duplicate_sku_rejected_without_using_sku_for_material_logic(reference_graph):
    graph = reference_graph
    with pytest.raises(ValidationError, match="already exists"):
        ProductVariant.objects.create(
            product=graph["product"], material=graph["second_material"], sku=graph["variant"].sku
        )


@pytest.mark.django_db(transaction=True)
def test_first_unit_creation_serializes_against_concurrent_variant_redefinition(reference_graph):
    graph = reference_graph
    unit_inserted = Event()
    release_insertion = Event()
    editor_started = Event()
    editor_pid = []

    def insert_unit():
        close_old_connections()
        try:
            with transaction.atomic():
                unit = InventoryUnit.objects.create(
                    variant_id=graph["variant"].pk, hub_id=graph["hub"].pk
                )
                unit_inserted.set()
                assert release_insertion.wait(10), "Test did not release the unit insertion."
                return unit.pk
        finally:
            connection.close()

    def redefine_variant():
        close_old_connections()
        try:
            variant = ProductVariant.objects.get(pk=graph["variant"].pk)
            variant.size_label = "Concurrent changed size"
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_backend_pid()")
                editor_pid.append(cursor.fetchone()[0])
            editor_started.set()
            with pytest.raises(
                ValidationError, match="recorded units or prices cannot be redefined"
            ):
                variant.save()
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as workers:
        insertion = workers.submit(insert_unit)
        try:
            assert unit_inserted.wait(10), "Unit insertion did not reach the protected transaction."
            editing = workers.submit(redefine_variant)
            assert editor_started.wait(10), "Variant edit did not start."
            deadline = monotonic() + 5
            while True:
                with connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT wait_event_type FROM pg_stat_activity WHERE pid = %s",
                        [editor_pid[0]],
                    )
                    waiting = cursor.fetchone()
                if waiting and waiting[0] == "Lock":
                    break
                assert monotonic() < deadline, "Variant edit did not wait for the identity lock."
                sleep(0.01)
        finally:
            release_insertion.set()
        assert (
            InventoryUnit.objects.get(pk=insertion.result(timeout=10)).variant_id
            == graph["variant"].pk
        )
        editing.result(timeout=10)

    graph["variant"].refresh_from_db()
    assert graph["variant"].size_label == "Scenario size"
