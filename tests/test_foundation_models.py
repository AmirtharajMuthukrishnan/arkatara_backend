"""Fictional configuration scenarios; these values do not approve production policy."""

from datetime import timedelta

import pytest
from django.core.exceptions import ValidationError
from django.db import DatabaseError, connection, transaction
from django.db.models.deletion import ProtectedError
from django.utils import timezone

from accounts.models import StaffUser
from compliance.configuration import (
    ConfigurationConflict,
    ConfigurationUnavailable,
    configuration_scope_key,
    get_approved_configuration,
)
from compliance.models import AuditEvent, BusinessConfiguration


@pytest.fixture
def staff():
    return StaffUser.objects.create_user(username="foundation-fixture-staff", is_staff=True)


def configuration(**overrides):
    values = {"namespace": "trials", "key": "maximum-boxes", "scope": {}, "value": 4}
    return BusinessConfiguration(**(values | overrides))


def approved_configuration(staff, **overrides):
    instant = timezone.now() - timedelta(minutes=1)
    values = {
        "status": BusinessConfiguration.Status.ACTIVE,
        "approval_reference": "test-fixture-only: fictional approval evidence",
        "approval_recorded_by": staff,
        "approved_at": instant,
        "effective_from": instant,
    }
    revision = configuration(**(values | overrides))
    revision.save()
    return revision


@pytest.mark.parametrize("scope", [[], None, "global", {"market": 1}, {"": "BLR"}])
def test_invalid_configuration_scope_is_rejected(scope):
    with pytest.raises(ValidationError, match="Scope"):
        configuration(scope=scope).clean()


@pytest.mark.parametrize("value", [-1, 0, True, 1.5, "4", None])
def test_limit_is_a_positive_integer_not_a_coerced_policy(value):
    with pytest.raises(ValidationError, match="positive integer"):
        configuration(value=value).clean()


@pytest.mark.parametrize(
    ("namespace", "key", "value"),
    [
        ("cart", "expiry-seconds", 180),
        ("inventory", "reservation-timeout-seconds", 180),
        ("trials", "category-eligibility", False),
    ],
)
def test_task_one_configuration_contracts_accept_typed_scenarios(namespace, key, value):
    configuration(namespace=namespace, key=key, value=value).clean()


def test_eligibility_requires_a_boolean_and_unknown_keys_are_rejected():
    with pytest.raises(ValidationError, match="boolean"):
        configuration(key="category-eligibility", value=1).clean()
    with pytest.raises(ValidationError, match="Unknown configuration key"):
        configuration(key="unregistered-policy").clean()


def test_exact_scope_identity_is_canonical_and_global_scope_is_valid():
    first = {"market": "fixture-market", "category": "fixture-category"}
    second = dict(reversed(list(first.items())))
    assert configuration_scope_key(first) == configuration_scope_key(second)
    assert configuration_scope_key({}) == "global"
    assert configuration_scope_key(first) != configuration_scope_key({"market": "fixture-market"})
    candidate = configuration()
    candidate._meta.get_field("scope").clean(candidate.scope, candidate)
    candidate.clean()


def test_active_and_draft_states_do_not_silently_assert_approval():
    with pytest.raises(ValidationError, match="approval reference"):
        configuration(status=BusinessConfiguration.Status.ACTIVE).clean()
    with pytest.raises(ValidationError, match="cannot claim commercial approval"):
        configuration(approval_reference="an unsupported claim").clean()


@pytest.mark.django_db
def test_configuration_validates_normal_orm_writes_and_keeps_revisions():
    with pytest.raises(ValidationError, match="positive integer"):
        configuration(value=-2).save()
    original = configuration()
    original.save()
    original.value = 8
    with pytest.raises(TypeError, match="append-only"):
        original.save()
    with pytest.raises(TypeError, match="append-only"):
        BusinessConfiguration.objects.filter(pk=original.pk).update(value=8)
    with pytest.raises(TypeError, match="append-only"):
        original.delete()
    revised = configuration(value=8, version=2)
    revised.save()
    original.refresh_from_db()
    assert original.value == 4
    assert revised.version == 2


@pytest.mark.django_db
def test_lookup_does_not_infer_missing_or_draft_policy():
    with pytest.raises(ConfigurationUnavailable, match="No approved policy"):
        get_approved_configuration("trials", "maximum-boxes", scope={})
    configuration().save()
    with pytest.raises(ConfigurationUnavailable, match="No approved policy"):
        get_approved_configuration("trials", "maximum-boxes", scope={})


@pytest.mark.django_db
def test_lookup_uses_exact_scope_and_preserves_selected_revision(staff):
    global_revision = approved_configuration(staff)
    scoped = approved_configuration(staff, scope={"market": "fixture-market"}, value=8)
    assert get_approved_configuration("trials", "maximum-boxes", scope={}).pk == global_revision.pk
    assert (
        get_approved_configuration("trials", "maximum-boxes", scope={"market": "fixture-market"}).pk
        == scoped.pk
    )
    with pytest.raises(ConfigurationUnavailable):
        get_approved_configuration("trials", "maximum-boxes", scope={"market": "other-market"})


@pytest.mark.django_db
def test_lookup_refuses_conflicting_revisions_instead_of_picking_latest(staff):
    approved_configuration(staff)
    approved_configuration(staff, version=2, value=8)
    with pytest.raises(ConfigurationConflict):
        get_approved_configuration("trials", "maximum-boxes", scope={})


@pytest.mark.django_db
@pytest.mark.parametrize(
    "missing_evidence",
    [
        {"approval_reference": ""},
        {"approval_reference": " \t\n"},
        {"approval_reference": "\u00a0"},
        {"approval_recorded_by": None},
        {"approved_at": None},
    ],
)
def test_lookup_ignores_legacy_unapproved_rows_before_counting_conflicts(staff, missing_evidence):
    instant = timezone.now() - timedelta(minutes=1)
    legacy_rows = []
    for version in (1, 2):
        legacy = configuration(
            **(
                {
                    "version": version,
                    "status": BusinessConfiguration.Status.ACTIVE,
                    "effective_from": instant,
                    "approval_reference": "test-fixture-only: incomplete evidence",
                    "approval_recorded_by": staff,
                    "approved_at": instant,
                }
                | missing_evidence
            )
        )
        # Reproduce persisted legacy state without pretending it passes today's model validation.
        legacy.save_base(force_insert=True)
        legacy_rows.append(legacy)

    with pytest.raises(ConfigurationUnavailable, match="No approved policy"):
        get_approved_configuration("trials", "maximum-boxes", scope={})

    approved = approved_configuration(staff, version=3)
    assert get_approved_configuration("trials", "maximum-boxes", scope={}).pk == approved.pk
    for legacy in legacy_rows:
        legacy.refresh_from_db()
        for field, missing_value in missing_evidence.items():
            assert getattr(legacy, field) == missing_value


@pytest.mark.django_db
def test_lookup_does_not_count_a_later_approval_as_historical_evidence(staff):
    instant = timezone.now()
    original = approved_configuration(
        staff,
        effective_from=instant - timedelta(hours=2),
        approved_at=instant - timedelta(hours=2),
    )
    approved_configuration(
        staff,
        version=2,
        effective_from=instant - timedelta(hours=2),
        approved_at=instant - timedelta(minutes=30),
    )
    historical = get_approved_configuration(
        "trials", "maximum-boxes", scope={}, at=instant - timedelta(hours=1)
    )
    assert historical.pk == original.pk
    # Both approvals exist now; do not invent a latest-version supersession rule.
    with pytest.raises(ConfigurationConflict):
        get_approved_configuration("trials", "maximum-boxes", scope={}, at=instant)


@pytest.mark.django_db
def test_lookup_respects_effective_and_approval_times(staff):
    instant = timezone.now()
    revision = approved_configuration(
        staff,
        effective_from=instant - timedelta(hours=1),
        effective_to=instant + timedelta(hours=1),
        approved_at=instant - timedelta(minutes=30),
    )
    assert (
        get_approved_configuration("trials", "maximum-boxes", scope={}, at=instant).pk
        == revision.pk
    )
    for outside in (
        instant - timedelta(hours=2),
        instant - timedelta(minutes=45),
        instant + timedelta(hours=1),
    ):
        with pytest.raises(ConfigurationUnavailable):
            get_approved_configuration("trials", "maximum-boxes", scope={}, at=outside)
    with pytest.raises(ValueError, match="timezone-aware"):
        get_approved_configuration(
            "trials", "maximum-boxes", scope={}, at=instant.replace(tzinfo=None)
        )


@pytest.mark.django_db
def test_audit_actor_identity_is_durable_and_actor_deletion_is_protected(staff):
    event = AuditEvent.objects.create(
        actor=staff,
        action="foundation.fixture",
        resource_type="test",
        resource_identifier="fixture",
    )
    expected = f"staff:{staff.public_id}"
    staff.username = "renamed-fixture-staff"
    staff.save()
    event.refresh_from_db()
    assert event.actor_label == expected
    with pytest.raises(ProtectedError):
        staff.delete()


@pytest.mark.django_db
def test_audit_events_require_attribution():
    facts = {
        "action": "foundation.fixture",
        "resource_type": "test",
        "resource_identifier": "fixture",
    }
    with pytest.raises(ValidationError, match="staff actor or a named system actor"):
        AuditEvent.objects.create(**facts)
    event = AuditEvent.objects.create(**facts, actor_label="system:foundation-fixture")
    assert event.actor_label == "system:foundation-fixture"


@pytest.mark.django_db
def test_audit_event_rejects_model_queryset_and_bulk_mutations():
    event = AuditEvent.objects.create(
        actor_label="system:foundation-fixture",
        action="foundation.fixture",
        resource_type="test",
        resource_identifier="fixture",
    )
    event.reason = "changed"
    attempts = (
        lambda: event.save(),
        lambda: event.delete(),
        lambda: AuditEvent.objects.filter(pk=event.pk).update(reason="changed"),
        lambda: AuditEvent.objects.filter(pk=event.pk).delete(),
        lambda: AuditEvent.objects.bulk_update([event], ["reason"]),
        lambda: AuditEvent.objects.bulk_create(
            [event], update_conflicts=True, update_fields=["reason"], unique_fields=["event_id"]
        ),
    )
    for attempt in attempts:
        with pytest.raises(TypeError, match="append-only"):
            attempt()
    event.refresh_from_db()
    assert event.reason == ""


@pytest.mark.django_db
def test_audit_database_rejects_update_and_delete():
    event = AuditEvent.objects.create(
        actor_label="system:foundation-fixture",
        action="foundation.fixture",
        resource_type="test",
        resource_identifier="fixture",
    )
    for statement in (
        "UPDATE compliance_auditevent SET reason = 'changed' WHERE event_id = %s",
        "DELETE FROM compliance_auditevent WHERE event_id = %s",
    ):
        with pytest.raises(DatabaseError, match="append-only"), transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute(statement, [event.pk])
    event.refresh_from_db()
    assert event.reason == ""
