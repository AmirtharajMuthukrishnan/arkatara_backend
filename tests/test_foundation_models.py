import pytest
from django.core.exceptions import ValidationError

from compliance.models import AuditEvent, BusinessConfiguration


def test_business_configuration_requires_object_scope() -> None:
    configuration = BusinessConfiguration(
        namespace="trials",
        key="example",
        scope=[],
        value={"enabled": True},
    )

    with pytest.raises(ValidationError, match="Scope must be a JSON object"):
        configuration.clean()


@pytest.mark.django_db
def test_audit_event_cannot_be_changed_or_deleted() -> None:
    event = AuditEvent.objects.create(
        action="foundation.test",
        resource_type="test",
        resource_identifier="example",
    )

    event.reason = "changed"
    with pytest.raises(TypeError, match="append-only"):
        event.save()

    with pytest.raises(TypeError, match="append-only"):
        AuditEvent.objects.filter(pk=event.pk).delete()
