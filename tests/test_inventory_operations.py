"""Reviewed-procedure scenarios, not production receiving or QC policy defaults."""

import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier

import pytest
from django.contrib.auth.models import Permission
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import DatabaseError, close_old_connections, connection, transaction
from django.db.models.deletion import ProtectedError
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from accounts.models import StaffUser
from catalog.models import Category, Material, Product, ProductVariant
from compliance.configuration import ConfigurationConflict, ConfigurationUnavailable
from compliance.models import AuditEvent, BusinessConfiguration
from inventory.models import InventoryMovement, InventoryUnit
from inventory.services import InventoryOperationConflict, receive_unit, record_qc, register_unit
from markets.models import Hub, Market

pytestmark = pytest.mark.django_db


@pytest.fixture
def operation_graph():
    actor = StaffUser.objects.create_superuser(
        username="scenario-stock-operator", password="test-only"
    )
    market = Market.objects.create(code="STOCK-SCENARIO", name="Scenario city", country_code="IN")
    hub = Hub.objects.create(code="STOCK-SCENARIO-HUB", name="Scenario hub", market=market)
    other_hub = Hub.objects.create(code="STOCK-OTHER-HUB", name="Other scenario hub", market=market)
    material = Material.objects.create(code="STOCK-METAL", name="Scenario material")
    category = Category.objects.create(code="STOCK-CATEGORY", name="Scenario category")
    product = Product.objects.create(code="STOCK-DESIGN", name="Scenario design", category=category)
    variant = ProductVariant.objects.create(
        product=product, material=material, sku="opaque-stock-scenario"
    )
    return {"actor": actor, "hub": hub, "other_hub": other_hub, "variant": variant}


def action_values(graph, **overrides):
    return {
        "actor": graph["actor"],
        "idempotency_key": uuid.uuid4(),
        "reason": "Controlled scenario observation",
        "source_reference": "test-evidence:scenario",
        **overrides,
    }


def policy(graph, key, *, hub=None, **overrides):
    instant = timezone.now() - timedelta(days=1)
    values = {
        "namespace": "inventory",
        "key": key,
        "scope": {"hub": str((hub or graph["hub"]).public_id)},
        "version": 1,
        "value": "test-only-procedure:scenario-v1",
        "status": BusinessConfiguration.Status.ACTIVE,
        "effective_from": instant,
        "approval_reference": "test-only-approval:scenario",
        "approval_recorded_by": graph["actor"],
        "approved_at": instant,
        **overrides,
    }
    return BusinessConfiguration.objects.create(**values)


def draft(graph):
    return register_unit(variant=graph["variant"], hub=graph["hub"], **action_values(graph))


def received(graph):
    unit = draft(graph)
    policy(graph, "receiving-procedure-reference")
    receive_unit(unit=unit, **action_values(graph))
    return unit


def test_registration_creates_identity_evidence_without_claiming_receipt(operation_graph):
    graph = operation_graph
    unit = draft(graph)
    assert (unit.status, unit.custody) == (
        InventoryUnit.Status.DRAFT,
        InventoryUnit.Custody.UNKNOWN,
    )
    movement = unit.movements.get()
    assert movement.action == InventoryMovement.Action.REGISTER
    assert movement.from_hub_id is None
    assert movement.to_hub == graph["hub"]
    assert movement.from_status == ""
    assert movement.procedure_revision is None
    assert movement.procedure_snapshot == {}
    assert movement.actor_label == f"staff:{graph['actor'].public_id}"
    audit = AuditEvent.objects.get(correlation_id=movement.idempotency_key)
    assert audit.metadata["movement_id"] == str(movement.public_id)


def test_receipt_and_passing_qc_require_distinct_reviewed_procedures(operation_graph):
    graph = operation_graph
    unit = draft(graph)
    receiving = policy(graph, "receiving-procedure-reference")
    receipt = receive_unit(unit=unit, **action_values(graph))
    unit.refresh_from_db()
    assert (unit.status, unit.custody) == (
        InventoryUnit.Status.QC_PENDING,
        InventoryUnit.Custody.HUB,
    )
    assert receipt.procedure_revision == receiving
    assert receipt.procedure_snapshot["value"] == receiving.value
    assert receipt.from_custody == InventoryUnit.Custody.UNKNOWN
    assert receipt.to_custody == InventoryUnit.Custody.HUB
    with pytest.raises(ConfigurationUnavailable):
        record_qc(unit=unit, passed=True, **action_values(graph))
    qc = policy(graph, "qc-procedure-reference")
    inspection = record_qc(unit=unit, passed=True, **action_values(graph))
    unit.refresh_from_db()
    assert unit.status == InventoryUnit.Status.AVAILABLE
    assert inspection.procedure_revision == qc
    assert unit.movements.count() == 3
    assert AuditEvent.objects.filter(resource_identifier=str(unit.public_id)).count() == 3


def test_failed_qc_quarantines_without_allowing_unreviewed_reinspection(operation_graph):
    graph = operation_graph
    unit = received(graph)
    policy(graph, "qc-procedure-reference")
    record_qc(unit=unit, passed=False, **action_values(graph))
    unit.refresh_from_db()
    assert unit.status == InventoryUnit.Status.QUARANTINED
    with pytest.raises(InventoryOperationConflict):
        record_qc(unit=unit, passed=True, **action_values(graph))
    assert unit.movements.count() == 3


def test_inspection_cannot_skip_physical_receipt(operation_graph):
    graph = operation_graph
    unit = draft(graph)
    policy(graph, "qc-procedure-reference")
    with pytest.raises(InventoryOperationConflict):
        record_qc(unit=unit, passed=True, **action_values(graph))
    unit.refresh_from_db()
    assert unit.status == InventoryUnit.Status.DRAFT


def test_missing_or_other_hub_procedure_cannot_authorize_receipt(operation_graph):
    graph = operation_graph
    unit = draft(graph)
    policy(graph, "receiving-procedure-reference", hub=graph["other_hub"])
    with pytest.raises(ConfigurationUnavailable):
        receive_unit(unit=unit, **action_values(graph))
    unit.refresh_from_db()
    assert (unit.status, unit.custody) == (
        InventoryUnit.Status.DRAFT,
        InventoryUnit.Custody.UNKNOWN,
    )
    assert unit.movements.count() == 1


def test_conflicting_procedures_do_not_select_latest_revision(operation_graph):
    graph = operation_graph
    unit = draft(graph)
    policy(graph, "receiving-procedure-reference")
    policy(graph, "receiving-procedure-reference", version=2)
    with pytest.raises(ConfigurationConflict):
        receive_unit(unit=unit, **action_values(graph))


def test_existing_legacy_draft_can_receive_without_invented_registration_history(operation_graph):
    graph = operation_graph
    unit = InventoryUnit.objects.create(variant=graph["variant"], hub=graph["hub"])
    policy(graph, "receiving-procedure-reference")
    receive_unit(unit=unit, **action_values(graph))
    assert list(unit.movements.values_list("action", flat=True)) == [
        InventoryMovement.Action.RECEIVE
    ]


def test_same_request_retries_once_even_after_unit_progresses(operation_graph):
    graph = operation_graph
    request = action_values(graph)
    unit = register_unit(variant=graph["variant"], hub=graph["hub"], **request)
    policy(graph, "receiving-procedure-reference")
    receipt_request = action_values(graph)
    first = receive_unit(unit=unit, **receipt_request)
    assert register_unit(variant=graph["variant"], hub=graph["hub"], **request).pk == unit.pk
    assert receive_unit(unit=unit, **receipt_request).pk == first.pk
    assert unit.movements.count() == 2
    assert AuditEvent.objects.count() == 2


def test_retry_reuses_evidence_without_reevaluating_changed_policy(operation_graph, monkeypatch):
    graph = operation_graph
    unit = received(graph)
    policy(graph, "qc-procedure-reference")
    request = action_values(graph)
    first = record_qc(unit=unit, passed=True, **request)

    def unavailable(*args, **kwargs):
        raise ConfigurationUnavailable("Scenario policy is no longer available.")

    monkeypatch.setattr("inventory.services.get_approved_configuration", unavailable)
    assert record_qc(unit=unit, passed=True, **request).pk == first.pk
    assert unit.movements.count() == 3


def test_successful_retry_still_requires_current_staff_authorization(operation_graph):
    graph = operation_graph
    request = action_values(graph)
    register_unit(variant=graph["variant"], hub=graph["hub"], **request)
    graph["actor"].is_active = False
    with pytest.raises(PermissionDenied):
        register_unit(variant=graph["variant"], hub=graph["hub"], **request)
    assert InventoryMovement.objects.count() == 1


def test_movement_snapshot_remains_original_when_new_procedure_revision_is_recorded(
    operation_graph,
):
    graph = operation_graph
    unit = draft(graph)
    original = policy(graph, "receiving-procedure-reference")
    movement = receive_unit(unit=unit, **action_values(graph))
    policy(
        graph, "receiving-procedure-reference", version=2, value="test-only-procedure:changed-v2"
    )
    movement.refresh_from_db()
    assert movement.procedure_revision_id == original.pk
    assert movement.procedure_snapshot["id"] == str(original.public_id)
    assert movement.procedure_snapshot["value"] == original.value


@pytest.mark.parametrize(
    "field,value", [("reason", "Different reason"), ("source_reference", "changed-evidence")]
)
def test_idempotency_key_cannot_be_reused_for_changed_evidence(operation_graph, field, value):
    graph = operation_graph
    request = action_values(graph)
    register_unit(variant=graph["variant"], hub=graph["hub"], **request)
    request[field] = value
    with pytest.raises(InventoryOperationConflict):
        register_unit(variant=graph["variant"], hub=graph["hub"], **request)
    assert InventoryUnit.objects.count() == 1


def test_idempotency_key_binds_inspection_result_and_unit(operation_graph):
    graph = operation_graph
    unit = received(graph)
    policy(graph, "qc-procedure-reference")
    request = action_values(graph)
    record_qc(unit=unit, passed=False, **request)
    with pytest.raises(InventoryOperationConflict):
        record_qc(unit=unit, passed=True, **request)
    other = draft(graph)
    with pytest.raises(InventoryOperationConflict):
        record_qc(unit=other, passed=False, **request)


def test_service_checks_action_permissions_not_just_staff_flag(operation_graph):
    graph = operation_graph
    staff = StaffUser.objects.create_user(username="limited-operator", is_staff=True)
    with pytest.raises(PermissionDenied):
        register_unit(
            variant=graph["variant"], hub=graph["hub"], **action_values(graph, actor=staff)
        )
    staff.user_permissions.add(Permission.objects.get(codename="add_inventoryunit"))
    staff = StaffUser.objects.get(pk=staff.pk)
    unit = register_unit(
        variant=graph["variant"], hub=graph["hub"], **action_values(graph, actor=staff)
    )
    with pytest.raises(PermissionDenied):
        receive_unit(unit=unit, **action_values(graph, actor=staff))
    with pytest.raises(PermissionDenied):
        record_qc(unit=unit, passed=True, **action_values(graph, actor=staff))


@pytest.mark.parametrize("field", ["is_active", "is_staff"])
def test_disabled_or_nonstaff_superuser_cannot_operate(operation_graph, field):
    graph = operation_graph
    setattr(graph["actor"], field, False)
    with pytest.raises(PermissionDenied):
        draft(graph)


def test_atomic_failure_leaves_state_and_history_unchanged(operation_graph, monkeypatch):
    graph = operation_graph
    unit = draft(graph)
    policy(graph, "receiving-procedure-reference")

    def fail_audit(*args, **kwargs):
        raise RuntimeError("Scenario audit failure")

    monkeypatch.setattr(AuditEvent.objects, "create", fail_audit)
    with pytest.raises(RuntimeError, match="audit failure"):
        receive_unit(unit=unit, **action_values(graph))
    unit.refresh_from_db()
    assert (unit.status, unit.custody) == (
        InventoryUnit.Status.DRAFT,
        InventoryUnit.Custody.UNKNOWN,
    )
    assert unit.movements.count() == 1
    unit.status = InventoryUnit.Status.AVAILABLE
    with pytest.raises(ValidationError):
        unit.save()


def test_registration_participates_in_caller_batch_transaction(operation_graph):
    with pytest.raises(RuntimeError), transaction.atomic():
        draft(operation_graph)
        raise RuntimeError("Scenario batch failure")
    assert InventoryUnit.objects.count() == 0
    assert InventoryMovement.objects.count() == 0
    assert AuditEvent.objects.count() == 0


@pytest.mark.parametrize(
    "field,value", [("status", "AVAILABLE"), ("status", "RETIRED"), ("custody", "HUB")]
)
def test_operational_state_cannot_be_changed_by_generic_save(operation_graph, field, value):
    unit = draft(operation_graph)
    setattr(unit, field, value)
    with pytest.raises(ValidationError):
        unit.save()
    with pytest.raises(TypeError):
        InventoryUnit.objects.filter(pk=unit.pk).update(**{field: value})
    with pytest.raises(TypeError):
        InventoryUnit.objects.bulk_update([unit], [field])


def test_active_unit_cannot_be_created_or_identity_deleted(operation_graph):
    graph = operation_graph
    with pytest.raises(ValidationError):
        InventoryUnit.objects.create(
            variant=graph["variant"], hub=graph["hub"], status="AVAILABLE", custody="HUB"
        )
    unit = draft(graph)
    with pytest.raises(TypeError):
        unit.delete()
    with pytest.raises(TypeError):
        InventoryUnit.objects.filter(pk=unit.pk).delete()
    with pytest.raises(ProtectedError):
        graph["actor"].delete()


def test_movement_orm_evidence_is_append_only(operation_graph):
    movement = draft(operation_graph).movements.get()
    movement.reason = "Rewrite"
    with pytest.raises(TypeError):
        movement.save()
    with pytest.raises(TypeError):
        movement.delete()
    with pytest.raises(TypeError):
        InventoryMovement.objects.filter(pk=movement.pk).update(reason="Rewrite")
    with pytest.raises(TypeError):
        InventoryMovement.objects.filter(pk=movement.pk).delete()
    with pytest.raises(ValidationError):
        InventoryMovement.objects.create(unit=movement.unit)


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE inventory_inventorymovement SET reason = 'rewrite' WHERE id = %s",
        "DELETE FROM inventory_inventorymovement WHERE id = %s",
    ],
)
def test_postgresql_preserves_movement_evidence(operation_graph, sql):
    movement = draft(operation_graph).movements.get()
    with (
        pytest.raises(DatabaseError, match="append-only"),
        transaction.atomic(),
        connection.cursor() as cursor,
    ):
        cursor.execute(sql, [movement.pk])
    movement.refresh_from_db()
    assert movement.reason == "Controlled scenario observation"


def test_future_or_out_of_order_observation_is_rejected(operation_graph):
    graph = operation_graph
    with pytest.raises(ValidationError, match="future"):
        register_unit(
            variant=graph["variant"],
            hub=graph["hub"],
            **action_values(graph, occurred_at=timezone.now() + timedelta(days=1)),
        )
    unit = draft(graph)
    policy(graph, "receiving-procedure-reference")
    with pytest.raises(InventoryOperationConflict, match="precede"):
        receive_unit(
            unit=unit, **action_values(graph, occurred_at=timezone.now() - timedelta(hours=1))
        )


def test_qc_needs_actual_receipt_evidence_even_if_state_is_tampered(operation_graph):
    graph = operation_graph
    unit = draft(graph)
    policy(graph, "qc-procedure-reference")
    with connection.cursor() as cursor:
        cursor.execute(
            "UPDATE inventory_inventoryunit SET status = 'QC_PENDING', custody = 'HUB' "
            "WHERE id = %s",
            [unit.pk],
        )
    with pytest.raises(InventoryOperationConflict, match="physical hub receipt"):
        record_qc(unit=unit, passed=True, **action_values(graph))


def test_admin_only_exposes_reviewed_forms_and_blocks_plain_state_posts(operation_graph, client):
    graph = operation_graph
    unit = draft(graph)
    client.force_login(graph["actor"])
    page = client.get(reverse("admin:inventory_inventoryunit_change", args=[unit.pk]))
    assert page.status_code == 200
    assert b"Record hub receipt" in page.content
    assert b'name="status"' not in page.content
    assert (
        client.post(
            reverse("admin:inventory_inventoryunit_change", args=[unit.pk]), {"status": "AVAILABLE"}
        ).status_code
        == 403
    )
    unit.refresh_from_db()
    assert unit.status == InventoryUnit.Status.DRAFT


def test_admin_receive_requires_csrf_and_reports_missing_policy(operation_graph, client):
    graph = operation_graph
    unit = draft(graph)
    path = reverse("admin:inventory_inventoryunit_receive", args=[unit.public_id])
    csrf_client = Client(enforce_csrf_checks=True)
    csrf_client.force_login(graph["actor"])
    payload = {
        "idempotency_key": str(uuid.uuid4()),
        "reason": "Scenario receipt",
        "source_reference": "test-evidence:receipt",
        "occurred_at": timezone.now().isoformat(),
    }
    assert csrf_client.post(path, payload).status_code == 403
    client.force_login(graph["actor"])
    response = client.post(path, payload)
    assert response.status_code == 200
    assert b"No approved policy" in response.content
    unit.refresh_from_db()
    assert unit.status == InventoryUnit.Status.DRAFT


def test_admin_registration_and_qc_run_service_history(operation_graph, client):
    graph = operation_graph
    client.force_login(graph["actor"])
    payload = {
        "idempotency_key": str(uuid.uuid4()),
        "reason": "Scenario onboarding",
        "source_reference": "test-evidence:admin",
        "occurred_at": timezone.now().isoformat(),
        "variant": graph["variant"].pk,
        "hub": graph["hub"].pk,
    }
    assert (
        client.post(reverse("admin:inventory_inventoryunit_register"), payload).status_code == 302
    )
    unit = InventoryUnit.objects.get()
    policy(graph, "receiving-procedure-reference")
    policy(graph, "qc-procedure-reference")
    for route, extra in (("receive", {}), ("inspect", {"outcome": "PASS"})):
        action_payload = {
            "idempotency_key": str(uuid.uuid4()),
            "reason": "Scenario observation",
            "source_reference": "test-evidence:admin",
            "occurred_at": timezone.now().isoformat(),
            **extra,
        }
        assert (
            client.post(
                reverse(f"admin:inventory_inventoryunit_{route}", args=[unit.public_id]),
                action_payload,
            ).status_code
            == 302
        )
    unit.refresh_from_db()
    assert unit.status == InventoryUnit.Status.AVAILABLE
    assert unit.movements.count() == 3


@pytest.mark.django_db(transaction=True)
def test_concurrent_identical_registration_records_one_identity(operation_graph):
    graph = operation_graph
    request = action_values(graph)
    barrier = Barrier(2)

    def execute():
        close_old_connections()
        try:
            actor = StaffUser.objects.get(pk=graph["actor"].pk)
            barrier.wait(timeout=10)
            return register_unit(
                variant=graph["variant"], hub=graph["hub"], **{**request, "actor": actor}
            ).pk
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as workers:
        futures = [workers.submit(execute) for _ in range(2)]
        results = [future.result(timeout=20) for future in futures]
    assert results[0] == results[1]
    assert (
        InventoryMovement.objects.count()
        == AuditEvent.objects.count()
        == InventoryUnit.objects.count()
        == 1
    )


@pytest.mark.django_db(transaction=True)
def test_concurrent_receipts_cannot_create_two_physical_transitions(operation_graph):
    graph = operation_graph
    unit = draft(graph)
    policy(graph, "receiving-procedure-reference")
    barrier = Barrier(2)

    def execute():
        close_old_connections()
        try:
            actor = StaffUser.objects.get(pk=graph["actor"].pk)
            barrier.wait(timeout=10)
            try:
                receive_unit(unit=unit, **action_values(graph, actor=actor))
                return "recorded"
            except InventoryOperationConflict:
                return "conflict"
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as workers:
        futures = [workers.submit(execute) for _ in range(2)]
        results = [future.result(timeout=20) for future in futures]
    assert sorted(results) == ["conflict", "recorded"]
    assert unit.movements.filter(action=InventoryMovement.Action.RECEIVE).count() == 1
    assert AuditEvent.objects.count() == 2


@pytest.mark.django_db(transaction=True)
def test_concurrent_inspection_outcomes_cannot_overwrite_each_other(operation_graph):
    graph = operation_graph
    unit = received(graph)
    policy(graph, "qc-procedure-reference")
    barrier = Barrier(2)

    def execute(passed):
        close_old_connections()
        try:
            actor = StaffUser.objects.get(pk=graph["actor"].pk)
            barrier.wait(timeout=10)
            try:
                movement = record_qc(unit=unit, passed=passed, **action_values(graph, actor=actor))
                return movement.to_status
            except InventoryOperationConflict:
                return "conflict"
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as workers:
        futures = [workers.submit(execute, passed) for passed in (True, False)]
        results = [future.result(timeout=20) for future in futures]
    assert results.count("conflict") == 1
    unit.refresh_from_db()
    assert unit.status in (InventoryUnit.Status.AVAILABLE, InventoryUnit.Status.QUARANTINED)
    assert unit.status in results
    assert unit.movements.count() == AuditEvent.objects.count() == 3
