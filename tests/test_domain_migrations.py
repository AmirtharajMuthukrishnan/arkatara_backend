"""Upgrade from Task 1 and protect new price history against direct SQL edits."""

import pytest
from django.core.exceptions import ValidationError
from django.db import DatabaseError, connection, transaction
from django.db.migrations.executor import MigrationExecutor

from catalog.models import Category, Material, PriceRevision, Product, ProductVariant
from compliance.models import AuditEvent


@pytest.mark.django_db(transaction=True)
def test_upgrade_from_foundation_seeds_only_approved_reference_states():
    executor = MigrationExecutor(connection)
    current = executor.loader.graph.leaf_nodes()
    event = AuditEvent.objects.create(
        actor_label="system:scenario-migration-test",
        action="scenario.before_task2",
        resource_type="fixture",
        resource_identifier="foundation",
    )
    try:
        executor.migrate([("inventory", None), ("catalog", None), ("markets", None)])
        MigrationExecutor(connection).migrate(current)
        apps = MigrationExecutor(connection).loader.project_state(current).apps
        market = apps.get_model("markets", "Market")
        material = apps.get_model("catalog", "Material")
        assert dict(market.objects.values_list("code", "status")) == {
            "BLR": "ACTIVE",
            "HYD": "COMING_SOON",
        }
        assert dict(material.objects.values_list("code", "status")) == {
            "SILVER": "ACTIVE",
            "GOLD": "COMING_SOON",
        }
        assert list(apps.get_model("catalog", "Purity").objects.values_list("code", flat=True)) == [
            "S925"
        ]
        for app, model in (
            ("markets", "Hub"),
            ("markets", "ServiceArea"),
            ("catalog", "Category"),
            ("catalog", "PriceRevision"),
            ("inventory", "InventoryUnit"),
            ("compliance", "BusinessConfiguration"),
        ):
            assert not apps.get_model(app, model).objects.exists()
        assert AuditEvent.objects.get(pk=event.pk).action == "scenario.before_task2"
        initial_ids = dict(market.objects.values_list("code", "public_id"))
        # Seed reversal retains identity; reapplication must not reset future display edits.
        market.objects.filter(code="BLR").update(name="Scenario renamed display")
        MigrationExecutor(connection).migrate(
            [("markets", "0001_initial"), ("catalog", "0001_initial")]
        )
        MigrationExecutor(connection).migrate(current)
        assert dict(market.objects.values_list("code", "public_id")) == initial_ids
        assert market.objects.get(code="BLR").name == "Scenario renamed display"
    finally:
        MigrationExecutor(connection).migrate(current)


@pytest.mark.django_db
def test_price_history_guard_and_referenced_variant_definition():
    category = Category.objects.create(code="PRICE-HISTORY", name="Scenario category")
    material = Material.objects.create(code="PRICE-HISTORY", name="Scenario material")
    product = Product.objects.create(
        code="PRICE-HISTORY", name="Scenario product", category=category
    )
    variant = ProductVariant.objects.create(
        product=product, material=material, sku="price-history-sku"
    )
    price = PriceRevision.objects.create(
        variant=variant, revision=1, mode="FIXED", currency="INR", fixed_amount="123.456789"
    )
    for statement in (
        "UPDATE catalog_pricerevision SET fixed_amount = 1 WHERE id = %s",
        "DELETE FROM catalog_pricerevision WHERE id = %s",
    ):
        with pytest.raises(DatabaseError, match="append-only"), transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute(statement, [price.pk])
    variant.size_label = "Changed after price evidence"
    with pytest.raises(ValidationError, match="cannot be redefined"):
        variant.save()
    product.category = Category.objects.create(code="NEW-PRICE-HISTORY", name="Other category")
    with pytest.raises(ValidationError, match="cannot be recategorized"):
        product.save()
