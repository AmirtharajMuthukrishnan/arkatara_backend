"""Explicit, authorized hub observations; no inferred receiving or QC policy."""

import hashlib
import json
from datetime import datetime
from uuid import UUID

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import connection, transaction
from django.utils import timezone

from compliance.configuration import get_approved_configuration
from compliance.models import AuditEvent
from inventory.models import InventoryMovement, InventoryUnit, _recording_operation


class InventoryOperationConflict(ValidationError):
    """A retry changed its request or an observed state no longer permits the action."""


def _authorize(actor, permission: str):
    if not (
        actor
        and actor.is_authenticated
        and actor.is_active
        and actor.is_staff
        and actor.has_perm(f"inventory.{permission}")
    ):
        raise PermissionDenied("This staff account cannot perform the inventory action.")


def _request(*, actor, action, idempotency_key, reason, source_reference, occurred_at, **facts):
    try:
        key = UUID(str(idempotency_key))
    except (ValueError, TypeError, AttributeError) as error:
        raise ValidationError("Supply a valid operation identifier.") from error
    for label, value in (("reason", reason), ("source_reference", source_reference)):
        if not isinstance(value, str) or not value.strip() or value != value.strip():
            raise ValidationError({label: "Supply a nonblank value without outer whitespace."})
    if len(source_reference) > 500:
        raise ValidationError({"source_reference": "Source reference is too long."})
    if occurred_at is not None and (
        not isinstance(occurred_at, datetime) or timezone.is_naive(occurred_at)
    ):
        raise ValidationError({"occurred_at": "Use a timezone-aware observation time."})
    if occurred_at is not None and occurred_at > timezone.now():
        raise ValidationError({"occurred_at": "An observation cannot occur in the future."})
    payload = {
        "actor": str(actor.public_id),
        "action": action,
        "reason": reason,
        "source_reference": source_reference,
        "occurred_at": occurred_at.isoformat() if occurred_at is not None else None,
        **facts,
    }
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
    return key, digest


def _lock_request(key: UUID, fingerprint: str):
    # A registration has no unit row yet. Serialize repeated request keys before
    # creating identity/evidence; independent keys still use normal unit row locks.
    # Refs: BUSINESS_RULES.md R-15, R-30;
    # STATE_MACHINES.md#separate-facts-common-transition-discipline.
    lock_key = key.int & ((1 << 63) - 1)
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock(%s)", [lock_key])
    previous = InventoryMovement.objects.filter(idempotency_key=key).select_related("unit").first()
    if previous is not None and previous.request_fingerprint != fingerprint:
        raise InventoryOperationConflict(
            "This operation identifier belongs to a different request."
        )
    return previous


def _procedure_snapshot(revision):
    return {
        "id": str(revision.public_id),
        "namespace": revision.namespace,
        "key": revision.key,
        "version": revision.version,
        "scope": revision.scope,
        "value": revision.value,
        "approval_reference": revision.approval_reference,
        "approved_at": revision.approved_at.isoformat(),
        "approval_recorded_by": str(revision.approval_recorded_by.public_id),
        "effective_from": revision.effective_from.isoformat(),
        "effective_to": revision.effective_to.isoformat() if revision.effective_to else None,
    }


def _record(
    *,
    unit,
    action,
    actor,
    key,
    fingerprint,
    before,
    reason,
    source_reference,
    occurred_at,
    procedure=None,
):
    with _recording_operation():
        movement = InventoryMovement.objects.create(
            unit=unit,
            action=action,
            from_status=before["status"],
            to_status=unit.status,
            from_hub_id=before["hub_id"],
            to_hub=unit.hub,
            from_custody=before["custody"],
            to_custody=unit.custody,
            actor=actor,
            occurred_at=occurred_at,
            reason=reason,
            source_reference=source_reference,
            idempotency_key=key,
            request_fingerprint=fingerprint,
            procedure_revision=procedure,
            procedure_snapshot=_procedure_snapshot(procedure) if procedure else {},
        )
    AuditEvent.objects.create(
        actor=actor,
        action=f"inventory.{action.lower()}",
        resource_type="inventory.inventoryunit",
        resource_identifier=str(unit.public_id),
        reason=reason,
        changes={
            "status": {"before": before["status"], "after": unit.status},
            "custody": {"before": before["custody"], "after": unit.custody},
            "hub": {
                "before": str(unit.hub.public_id) if before["hub_id"] else None,
                "after": str(unit.hub.public_id),
            },
        },
        metadata={"movement_id": str(movement.public_id), "source_reference": source_reference},
        correlation_id=key,
    )
    return movement


def register_unit(
    *, variant, hub, actor, idempotency_key, reason, source_reference, occurred_at=None
) -> InventoryUnit:
    """Register an unverified identity; neither ownership nor physical receipt is implied.

    Refs: BUSINESS_RULES.md R-11, R-15, R-30; DECISIONS.md BD-09, BD-11.
    """
    _authorize(actor, "add_inventoryunit")
    key, fingerprint = _request(
        actor=actor,
        action=InventoryMovement.Action.REGISTER,
        idempotency_key=idempotency_key,
        reason=reason,
        source_reference=source_reference,
        occurred_at=occurred_at,
        variant=str(variant.public_id),
        hub=str(hub.public_id),
    )
    with transaction.atomic():
        previous = _lock_request(key, fingerprint)
        if previous:
            return previous.unit
        unit = InventoryUnit.objects.create(variant=variant, hub=hub)
        _record(
            unit=unit,
            action=InventoryMovement.Action.REGISTER,
            actor=actor,
            key=key,
            fingerprint=fingerprint,
            before={"status": "", "hub_id": None, "custody": "UNKNOWN"},
            reason=reason,
            source_reference=source_reference,
            occurred_at=occurred_at or timezone.now(),
        )
        return unit


def _transition(
    *,
    unit,
    actor,
    action,
    permission,
    policy_key,
    target_status,
    idempotency_key,
    reason,
    source_reference,
    occurred_at,
):
    _authorize(actor, permission)
    key, fingerprint = _request(
        actor=actor,
        action=action,
        idempotency_key=idempotency_key,
        reason=reason,
        source_reference=source_reference,
        occurred_at=occurred_at,
        unit=str(unit.public_id),
    )
    with transaction.atomic():
        previous = _lock_request(key, fingerprint)
        if previous:
            return previous
        locked = InventoryUnit.objects.select_for_update().get(public_id=unit.public_id)
        instant = occurred_at or timezone.now()
        latest = locked.movements.order_by("-occurred_at", "-pk").first()
        if latest and instant < latest.occurred_at:
            raise InventoryOperationConflict(
                "An observation cannot precede the recorded unit history."
            )
        if action == InventoryMovement.Action.RECEIVE:
            if (
                locked.status != InventoryUnit.Status.DRAFT
                or locked.custody != InventoryUnit.Custody.UNKNOWN
            ):
                raise InventoryOperationConflict("Only an unverified draft may be received.")
        else:
            if (
                locked.status != InventoryUnit.Status.QC_PENDING
                or locked.custody != InventoryUnit.Custody.HUB
            ):
                raise InventoryOperationConflict(
                    "Inspection requires stock received at the hub and awaiting QC."
                )
            if not locked.movements.filter(
                action=InventoryMovement.Action.RECEIVE,
                to_hub=locked.hub,
                to_custody=InventoryUnit.Custody.HUB,
            ).exists():
                raise InventoryOperationConflict(
                    "Recorded physical hub receipt is required before inspection."
                )
        procedure = get_approved_configuration(
            "inventory", policy_key, scope={"hub": str(locked.hub.public_id)}, at=instant
        )
        before = {"status": locked.status, "hub_id": locked.hub_id, "custody": locked.custody}
        locked.status = target_status
        locked.custody = InventoryUnit.Custody.HUB
        with _recording_operation():
            locked.save(update_fields=["status", "custody", "updated_at"])
        return _record(
            unit=locked,
            action=action,
            actor=actor,
            key=key,
            fingerprint=fingerprint,
            before=before,
            reason=reason,
            source_reference=source_reference,
            occurred_at=instant,
            procedure=procedure,
        )


def receive_unit(
    *, unit, actor, idempotency_key, reason, source_reference, occurred_at=None
) -> InventoryMovement:
    """Record actual receipt using the hub's explicitly approved receiving procedure.

    No return-from-customer or inter-hub transfer route is enabled by this action.
    Refs: BUSINESS_RULES.md R-08, R-15, R-30; DECISIONS.md BD-08, BD-09.
    """
    return _transition(
        unit=unit,
        actor=actor,
        action=InventoryMovement.Action.RECEIVE,
        permission="receive_inventoryunit",
        policy_key="receiving-procedure-reference",
        target_status=InventoryUnit.Status.QC_PENDING,
        idempotency_key=idempotency_key,
        reason=reason,
        source_reference=source_reference,
        occurred_at=occurred_at,
    )


def record_qc(
    *, unit, passed: bool, actor, idempotency_key, reason, source_reference, occurred_at=None
) -> InventoryMovement:
    """Only recorded hub receipt and reviewed passing QC can establish availability.

    Failed inspection quarantines stock without choosing liability or disposition.
    Refs: BUSINESS_RULES.md R-15, R-17, R-30; DECISIONS.md BD-09.
    """
    if type(passed) is not bool:
        raise ValidationError({"passed": "Record an explicit inspection outcome."})
    return _transition(
        unit=unit,
        actor=actor,
        action=InventoryMovement.Action.QC_PASS if passed else InventoryMovement.Action.QC_FAIL,
        permission="inspect_inventoryunit",
        policy_key="qc-procedure-reference",
        target_status=InventoryUnit.Status.AVAILABLE
        if passed
        else InventoryUnit.Status.QUARANTINED,
        idempotency_key=idempotency_key,
        reason=reason,
        source_reference=source_reference,
        occurred_at=occurred_at,
    )
