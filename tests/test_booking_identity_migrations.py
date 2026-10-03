"""Additive upgrades and evidence-preserving rollback boundaries on PostgreSQL."""

import uuid

import pytest
from django.db import DatabaseError, connection, transaction
from django.db.migrations.exceptions import IrreversibleError
from django.db.migrations.executor import MigrationExecutor
from django.db.migrations.recorder import MigrationRecorder
from django.utils import timezone

from accounts.models import CustomerAccount, StaffUser
from compliance.models import AuditEvent, PolicyDocumentRevision
from delivery.models import DeliveryAssignment
from markets.models import Market
from trials.models import TrialBooking

pytestmark = pytest.mark.django_db(transaction=True)


def migrate(targets):
    executor = MigrationExecutor(connection)
    executor.migrate(targets)
    return executor.loader.project_state([node for node in targets if node[1] is not None]).apps


def test_identity_upgrade_preserves_staff_and_audit_without_inventing_customers_or_orders():
    current = MigrationExecutor(connection).loader.graph.leaf_nodes()
    earlier = {
        "accounts": "0001_initial",
        "compliance": "0002_foundation_integrity",
        "trials": None,
        "delivery": None,
    }
    previous = [(app, earlier.get(app, name)) for app, name in current]
    try:
        old = migrate(previous)
        staff = old.get_model("accounts", "StaffUser").objects.create(
            username="legacy-identity-scenario", password="!", is_staff=True
        )
        audit = old.get_model("compliance", "AuditEvent").objects.create(
            actor=staff,
            actor_label=f"staff:{staff.public_id}",
            action="scenario.pre_identity_models",
            resource_type="migration-test",
            resource_identifier=str(staff.public_id),
        )
        identity = (staff.pk, staff.public_id, staff.date_joined, staff.password, staff.is_staff)
        audit_identity = (audit.event_id, audit.actor_id, audit.occurred_at)
        new = migrate(current)
        retained = new.get_model("accounts", "StaffUser").objects.get(pk=staff.pk)
        assert (
            retained.pk,
            retained.public_id,
            retained.date_joined,
            retained.password,
            retained.is_staff,
        ) == identity
        retained_audit = new.get_model("compliance", "AuditEvent").objects.get(pk=audit.pk)
        assert (
            retained_audit.event_id,
            retained_audit.actor_id,
            retained_audit.occurred_at,
        ) == audit_identity
        for app, model in (
            ("accounts", "CustomerAccount"),
            ("compliance", "PolicyDocumentRevision"),
            ("trials", "TrialBooking"),
            ("trials", "BookingPhoneChallenge"),
            ("trials", "GuestBookingAccessGrant"),
            ("delivery", "DeliveryAssignment"),
            ("delivery", "ArrivalOTP"),
        ):
            assert not new.get_model(app, model).objects.exists()
        # Empty new tables can be removed and recreated without touching legacy history.
        migrate(previous)
        migrate(current)
        assert StaffUser.objects.get(pk=staff.pk).public_id == identity[1]
        assert AuditEvent.objects.get(pk=audit.pk).occurred_at == audit_identity[2]
    finally:
        migrate(current)


def make_booking():
    market = Market.objects.create(
        code="ROLLBACK-SCENARIO", name="Fictional migration market", country_code="IN"
    )
    return TrialBooking.objects.create(
        checkout_mode="GUEST",
        market=market,
        contact_name="Fictional recipient",
        contact_phone="+919000000001",
        address="Fictional migration address",
        country_code="IN",
        postal_code="560001",
        request_id=uuid.uuid4(),
        request_fingerprint="a" * 64,
    )


@pytest.mark.parametrize(
    "app,schema,guard,kind",
    [
        ("accounts", "0002_customeraccount", "0003_customer_identity_guard", "customer"),
        ("compliance", "0003_policydocumentrevision", "0004_policy_history_guard", "policy"),
        ("trials", "0001_initial", "0002_booking_evidence_guards", "booking"),
        ("delivery", "0002_initial", "0003_assignment_evidence_guards", "assignment"),
    ],
)
def test_downgrade_cannot_remove_guards_once_identity_or_evidence_exists(app, schema, guard, kind):
    current = MigrationExecutor(connection).loader.graph.leaf_nodes()
    if kind == "customer":
        record = CustomerAccount.objects.create()
    elif kind == "policy":
        record = PolicyDocumentRevision.objects.create(
            document_code="rollback-scenario",
            revision=1,
            language_code="en",
            content="Fictional migration policy, not approved terms.",
        )
    elif kind == "booking":
        record = make_booking()
    else:
        booking = make_booking()
        staff = StaffUser.objects.create_user(username="rollback-scenario-agent")
        record = DeliveryAssignment.objects.create(
            booking=booking, agent=staff, assigned_by=staff, assigned_at=timezone.now()
        )
    try:
        with pytest.raises(IrreversibleError, match="forward recovery"):
            migrate([(app, schema)])
        assert (app, guard) in MigrationRecorder(connection).applied_migrations()
        assert type(record).objects.get(pk=record.pk).public_id == record.public_id
        with pytest.raises(DatabaseError), transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute(f"DELETE FROM {record._meta.db_table} WHERE id=%s", [record.pk])
    finally:
        migrate(current)
