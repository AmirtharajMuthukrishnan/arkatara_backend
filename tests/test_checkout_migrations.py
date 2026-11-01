"""Upgrade preserves each legacy credential and refuses evidence-losing rollback."""

import uuid
from datetime import timedelta

import pytest
from django.db import connection
from django.db.migrations.exceptions import IrreversibleError
from django.db.migrations.executor import MigrationExecutor
from django.utils import timezone

pytestmark = pytest.mark.django_db(transaction=True)


def test_upgrade_moves_existing_digest_without_changing_booking_or_grant():
    executor = MigrationExecutor(connection)
    latest = executor.loader.graph.leaf_nodes()
    earlier = {
        "trials": "0002_booking_evidence_guards",
        "payments": None,
        "notifications": None,
        "billing": None,
        "delivery": "0003_assignment_evidence_guards",
        "inventory": "0004_operational_downgrade_guard",
    }
    targets = [(app, earlier.get(app, name)) for app, name in latest]
    try:
        executor.migrate(targets)
        old = executor.loader.project_state([t for t in targets if t[1] is not None]).apps
        market = old.get_model("markets", "Market").objects.create(
            code="CHECKOUT-UPGRADE", name="Fictional migration city", country_code="IN"
        )
        booking = old.get_model("trials", "TrialBooking").objects.create(
            market=market,
            checkout_mode="GUEST",
            contact_name="Fictional recipient",
            contact_phone="+919000000030",
            address="Fictional address",
            country_code="IN",
            postal_code="560001",
            request_id=uuid.uuid4(),
            request_fingerprint="a" * 64,
        )
        now = timezone.now()
        grant = old.get_model("trials", "GuestBookingAccessGrant").objects.create(
            booking=booking,
            token_digest="b" * 64,
            issued_at=now - timedelta(hours=2),
            expires_at=now + timedelta(hours=1),
            revoked_at=now,
        )
        MigrationExecutor(connection).migrate(latest)
        new = MigrationExecutor(connection).loader.project_state(latest).apps
        retained = new.get_model("trials", "GuestBookingAccessGrant").objects.get(pk=grant.pk)
        session = retained.browser_session
        assert (
            retained.public_id,
            retained.booking_id,
            retained.issued_at,
            retained.expires_at,
            retained.revoked_at,
        ) == (
            grant.public_id,
            grant.booking_id,
            grant.issued_at,
            grant.expires_at,
            grant.revoked_at,
        )
        assert session.token_digest == "b" * 64
        assert session.revoked_at == grant.revoked_at
        assert session.expires_at == grant.expires_at
        retained_booking = new.get_model("trials", "TrialBooking").objects.get(pk=booking.pk)
        assert retained_booking.public_id == booking.public_id
        assert retained_booking.customer_account_id is None
        assert retained_booking.status == "DRAFT"
        assert not new.get_model("payments", "PaymentContext").objects.exists()
        with pytest.raises(IrreversibleError, match="forward recovery"):
            MigrationExecutor(connection).migrate(targets)
    finally:
        MigrationExecutor(connection).migrate(latest)
