"""Upgrade custody conservatively and retain SQL evidence guards on unsafe downgrade."""

import uuid

import pytest
from django.core.exceptions import FieldDoesNotExist
from django.db import DatabaseError, IntegrityError, connection, transaction
from django.db.migrations.exceptions import IrreversibleError
from django.db.migrations.executor import MigrationExecutor
from django.db.migrations.recorder import MigrationRecorder
from django.utils import timezone

pytestmark = pytest.mark.django_db(transaction=True)

TASK2_INVENTORY = ("inventory", "0001_initial")
TASK3_SCHEMA = ("inventory", "0002_inventorymovement_alter_inventoryunit_options_and_more")
DOWNGRADE_GUARD = ("inventory", "0004_operational_downgrade_guard")


def migrate(targets):
    executor = MigrationExecutor(connection)
    executor.migrate(targets)
    return executor.loader.project_state([node for node in targets if node[1] is not None]).apps


def reference_graph(apps):
    market = apps.get_model("markets", "Market").objects.create(
        code="MIGRATION-CITY", name="Migration scenario city", country_code="IN"
    )
    hub = apps.get_model("markets", "Hub").objects.create(
        code="MIGRATION-HUB", name="Migration scenario hub", market=market
    )
    material = apps.get_model("catalog", "Material").objects.create(
        code="MIGRATION-METAL", name="Migration scenario material"
    )
    category = apps.get_model("catalog", "Category").objects.create(
        code="MIGRATION-CATEGORY", name="Migration scenario category"
    )
    product = apps.get_model("catalog", "Product").objects.create(
        code="MIGRATION-DESIGN", name="Migration scenario design", category=category
    )
    variant = apps.get_model("catalog", "ProductVariant").objects.create(
        product=product, material=material, sku="migration-scenario-sku"
    )
    return hub, variant


def unit_identity(unit):
    return (
        unit.pk,
        unit.public_id,
        unit.variant_id,
        unit.hub_id,
        unit.status,
        unit.created_at,
        unit.updated_at,
    )


def assert_movement_guard(movement_id):
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT EXISTS (SELECT 1 FROM pg_trigger "
            "WHERE tgrelid = 'inventory_inventorymovement'::regclass "
            "AND tgname = 'inventory_movement_append_only' AND tgenabled <> 'D')"
        )
        assert cursor.fetchone()[0] is True
    for sql in (
        "UPDATE inventory_inventorymovement SET reason = 'rewritten' WHERE id = %s",
        "DELETE FROM inventory_inventorymovement WHERE id = %s",
    ):
        with pytest.raises(DatabaseError, match="append-only"), transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute(sql, [movement_id])


def test_task2_upgrade_preserves_draft_identity_without_invented_custody_or_history():
    restore = MigrationExecutor(connection).loader.graph.leaf_nodes()
    # Transaction apps depend on later catalogue migrations, so an older catalogue
    # target must also remove those dependants rather than request their latest state.
    task2 = [
        (app, None)
        if app in {"trials", "delivery", "payments", "notifications", "billing"}
        else ("catalog", "0003_price_history_guard")
        if app == "catalog"
        else TASK2_INVENTORY
        if app == "inventory"
        else (app, migration)
        for app, migration in restore
    ]
    try:
        old_apps = migrate(task2)
        hub, variant = reference_graph(old_apps)
        old_unit = old_apps.get_model("inventory", "InventoryUnit")
        legacy = old_unit.objects.create(hub=hub, variant=variant)
        identity = unit_identity(legacy)
        # Task 2 admitted only DRAFT. Do not pretend a RETIRED legacy row existed.
        with pytest.raises(IntegrityError), transaction.atomic():
            old_unit.objects.create(hub=hub, variant=variant, status="RETIRED")

        new_apps = migrate(restore)
        upgraded = new_apps.get_model("inventory", "InventoryUnit").objects.get(pk=legacy.pk)
        assert unit_identity(upgraded) == identity
        assert upgraded.custody == "UNKNOWN"
        assert new_apps.get_model("inventory", "InventoryMovement").objects.count() == 0

        # With no operations recorded, a development schema rollback remains possible.
        reversed_apps = migrate(task2)
        reversed_unit = reversed_apps.get_model("inventory", "InventoryUnit")
        assert unit_identity(reversed_unit.objects.get(pk=legacy.pk)) == identity
        with pytest.raises(FieldDoesNotExist):
            reversed_unit._meta.get_field("custody")
        restored_apps = migrate(restore)
        restored = restored_apps.get_model("inventory", "InventoryUnit").objects.get(pk=legacy.pk)
        assert unit_identity(restored) == identity
        assert restored.custody == "UNKNOWN"
        assert restored_apps.get_model("inventory", "InventoryMovement").objects.count() == 0
    finally:
        migrate(restore)


def test_guard_install_preserves_prior_draft_movement_and_failed_downgrade_keeps_guard():
    restore = MigrationExecutor(connection).loader.graph.leaf_nodes()
    try:
        schema_apps = migrate([TASK3_SCHEMA])
        hub, variant = reference_graph(schema_apps)
        unit = schema_apps.get_model("inventory", "InventoryUnit").objects.create(
            hub=hub, variant=variant
        )
        actor = schema_apps.get_model("accounts", "StaffUser").objects.create(
            username="migration-scenario-operator"
        )
        # Historical models intentionally represent evidence predating guard installation.
        movement = schema_apps.get_model("inventory", "InventoryMovement").objects.create(
            unit=unit,
            action="REGISTER",
            from_status="",
            to_status="DRAFT",
            to_hub=hub,
            from_custody="UNKNOWN",
            to_custody="UNKNOWN",
            actor=actor,
            actor_label=f"staff:{actor.public_id}",
            occurred_at=timezone.now(),
            reason="Original migration scenario evidence",
            source_reference="test-evidence:migration",
            idempotency_key=uuid.uuid4(),
            request_fingerprint="a" * 64,
            procedure_snapshot={},
        )
        original = (
            movement.public_id,
            movement.occurred_at,
            movement.recorded_at,
            movement.reason,
            movement.actor_label,
            movement.idempotency_key,
        )
        guarded_apps = migrate(restore)
        retained_model = guarded_apps.get_model("inventory", "InventoryMovement")
        retained = retained_model.objects.get(pk=movement.pk)
        assert (
            retained.public_id,
            retained.occurred_at,
            retained.recorded_at,
            retained.reason,
            retained.actor_label,
            retained.idempotency_key,
        ) == original
        assert retained.procedure_revision_id is None
        assert retained.procedure_snapshot == {}
        assert retained_model.objects.count() == 1
        assert_movement_guard(movement.pk)

        with pytest.raises(IrreversibleError, match="forward recovery"):
            migrate([TASK2_INVENTORY])
        assert DOWNGRADE_GUARD in MigrationRecorder(connection).applied_migrations()
        assert retained_model.objects.get(pk=movement.pk).reason == original[3]
        assert_movement_guard(movement.pk)
    finally:
        migrate(restore)


def test_retired_task3_state_survives_guard_upgrade_and_blocks_unsafe_downgrade():
    restore = MigrationExecutor(connection).loader.graph.leaf_nodes()
    try:
        schema_apps = migrate([TASK3_SCHEMA])
        hub, variant = reference_graph(schema_apps)
        # RETIRED can be represented in Task 3's schema, although no live retirement
        # workflow is enabled. This fixture does not approve a disposition policy.
        unit = schema_apps.get_model("inventory", "InventoryUnit").objects.create(
            hub=hub, variant=variant, status="RETIRED", custody="UNKNOWN"
        )
        identity = unit_identity(unit)
        guarded_apps = migrate(restore)
        current_unit = guarded_apps.get_model("inventory", "InventoryUnit")
        retained = current_unit.objects.get(pk=unit.pk)
        assert unit_identity(retained) == identity
        assert retained.custody == "UNKNOWN"
        assert guarded_apps.get_model("inventory", "InventoryMovement").objects.count() == 0

        with pytest.raises(IrreversibleError, match="forward recovery"):
            migrate([TASK2_INVENTORY])
        assert DOWNGRADE_GUARD in MigrationRecorder(connection).applied_migrations()
        assert unit_identity(current_unit.objects.get(pk=unit.pk)) == identity
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT count(*) FROM pg_trigger "
                "WHERE tgrelid = 'inventory_inventorymovement'::regclass "
                "AND tgname = 'inventory_movement_append_only' AND tgenabled <> 'D'"
            )
            assert cursor.fetchone()[0] == 1
    finally:
        migrate(restore)
