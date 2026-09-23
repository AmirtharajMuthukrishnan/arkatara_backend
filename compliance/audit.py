"""Attributable audit writes used inside the same transaction as privileged changes."""

from datetime import date, datetime
from uuid import UUID

from compliance.models import AuditEvent


def model_facts(instance, field_names: tuple[str, ...]) -> dict:
    facts = {}
    for name in field_names:
        value = getattr(instance, name)
        if isinstance(value, date | datetime):
            value = value.isoformat()
        elif isinstance(value, UUID):
            value = str(value)
        facts[name] = value
    return facts


def record_model_change(
    *, actor, instance, before: dict, after: dict, reason: str = ""
) -> AuditEvent:
    changes = {
        name: {"before": before.get(name), "after": value}
        for name, value in after.items()
        if name not in before or before[name] != value
    }
    return AuditEvent.objects.using(instance._state.db).create(
        actor=actor,
        action="admin.change" if before else "admin.create",
        resource_type=instance._meta.label_lower,
        resource_identifier=str(instance.public_id),
        changes=changes,
        reason=reason,
    )
