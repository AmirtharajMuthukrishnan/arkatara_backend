"""Failure-focused review regressions with fictional provider evidence."""

import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier

import pytest
from django.core.exceptions import ValidationError
from django.db import DatabaseError, close_old_connections, connection, transaction
from django.utils import timezone

from compliance.models import BusinessConfiguration
from payments.models import (
    GatewayTransaction,
    GatewayTransactionMatch,
    PaymentAllocation,
    PaymentResolution,
    PaymentResolutionReceipt,
)
from tests.test_checkout_models import (
    begin_capture,
    booking_graph,
    open_context,
    raw_insert,
    receipt,
    reconcile,
    rejected,
    start_payment,
    stock,
    verify,
)
from tests.test_checkout_models import setup as setup
from trials.models import GuestBookingAccessGrant, TrialPlanRevision

pytestmark = pytest.mark.django_db


def test_capture_after_context_closure_requires_fresh_resolution_before_release(setup):
    graph = booking_graph(setup)
    start_payment(graph, stock(setup))
    begin_capture(graph)
    original = reconcile(graph, release=False)
    late = receipt(graph)
    with pytest.raises(DatabaseError), transaction.atomic():
        PaymentResolutionReceipt.objects.create(resolution=original, gateway_transaction=late)
    rejected(
        "UPDATE inventory_inventoryreservation SET status='RELEASED',released_at=now(),"
        "release_reason='stale',resolution_id=%s WHERE id=%s",
        [original.pk, graph.hold.pk],
    )
    observed = timezone.now()
    graph.attempt.last_verified_at = observed
    graph.attempt.save()
    fresh = PaymentResolution.objects.create(
        context=graph.context,
        system_actor="fictional-reconciler",
        reason="Verified late capture is not accepted for fulfilment",
        evidence_reference="test-only-late",
        reconciled_at=observed,
        outcome="CAPTURE_NOT_ACCEPTED",
        captured_minor=100,
        allocated_minor=0,
        currency="INR",
    )
    PaymentResolutionReceipt.objects.create(resolution=fresh, gateway_transaction=late)
    graph.hold.status = "RELEASED"
    graph.hold.released_at = timezone.now()
    graph.hold.release_reason = "Fresh reconciliation"
    graph.hold.resolution = fresh
    graph.hold.save()
    graph.context.refresh_from_db()
    assert graph.context.resolution_id == original.pk
    assert graph.hold.resolution_id == fresh.pk


def test_wrong_currency_capture_can_be_reconciled_without_relabelling_money(setup):
    graph = booking_graph(setup)
    start_payment(graph, stock(setup))
    begin_capture(graph)
    wrong = receipt(graph, currency="USD")
    resolved = reconcile(graph, captured_minor=0, outcome="CAPTURE_NOT_ACCEPTED")
    assert resolved.receipts.get().gateway_transaction.currency == "USD"
    assert resolved.captured_minor == 0
    assert wrong.currency == "USD"
    assert graph.hold.status == "RELEASED"


@pytest.mark.django_db(transaction=True)
def test_cancellation_allocation_and_attempt_updates_serialize_without_deadlock(setup):
    graph = booking_graph(setup)
    start_payment(graph, stock(setup))
    begin_capture(graph)
    captured = receipt(graph)
    barrier = Barrier(3)

    def writer(operation):
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            with transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute("SET LOCAL lock_timeout='4s'")
                    if operation == "cancel":
                        cursor.execute(
                            "UPDATE trials_trialbooking SET status='CANCELLED' WHERE id=%s",
                            [graph.booking.pk],
                        )
                    elif operation == "observe":
                        cursor.execute(
                            "UPDATE payments_paymentattempt SET last_verified_at=now() WHERE id=%s",
                            [graph.attempt.pk],
                        )
                    else:
                        raw_insert(
                            PaymentAllocation,
                            gateway_transaction_id=captured.pk,
                            booking_charge_id=graph.charge.pk,
                            amount_minor=100,
                            currency="INR",
                            idempotency_key=uuid.uuid4(),
                        )
            return operation + ":ok"
        except DatabaseError as exc:
            return operation + ":" + str(getattr(exc.__cause__, "sqlstate", None))
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(writer, ["cancel", "observe", "allocate"]))
    assert sorted(results) == ["allocate:ok", "cancel:23514", "observe:ok"]
    assert captured.allocations.get().amount_minor == 100
    graph.booking.refresh_from_db()
    assert graph.booking.status == "PAYMENT_PENDING"


@pytest.mark.parametrize("phase", ["OPEN", "CAPTURING", "ALLOCATED"])
def test_terminal_booking_and_charge_cannot_bypass_live_payment(setup, phase):
    graph = booking_graph(setup)
    start_payment(graph, stock(setup))
    if phase != "OPEN":
        begin_capture(graph)
    if phase == "ALLOCATED":
        captured = receipt(graph)
        PaymentAllocation.objects.create(
            gateway_transaction=captured,
            booking_charge=graph.charge,
            amount_minor=20,
            currency="INR",
            idempotency_key=uuid.uuid4(),
        )
    for status in ("CANCELLED", "PAYMENT_EXPIRED"):
        rejected("UPDATE trials_trialbooking SET status=%s WHERE id=%s", [status, graph.booking.pk])
    rejected("UPDATE payments_bookingcharge SET status='VOID' WHERE id=%s", [graph.charge.pk])


@pytest.mark.parametrize("amount,allocated", [(40, 0), (40, 20), (100, 0), (140, 0)])
def test_captured_exception_can_resolve_without_erasing_money(setup, amount, allocated):
    graph = booking_graph(setup)
    start_payment(graph, stock(setup))
    begin_capture(graph)
    captured = receipt(graph, amount_minor=amount)
    if allocated:
        PaymentAllocation.objects.create(
            gateway_transaction=captured,
            booking_charge=graph.charge,
            amount_minor=allocated,
            currency="INR",
            idempotency_key=uuid.uuid4(),
        )
    resolution = reconcile(graph, captured_minor=amount, allocated_minor=allocated)
    graph.charge.status = "VOID"
    graph.charge.save()
    graph.booking.status = "CANCELLED"
    graph.booking.save()
    assert captured.allocations.count() == bool(allocated)
    assert GatewayTransaction.objects.get(pk=captured.pk).amount_minor == amount
    assert resolution.allocated_minor == allocated
    rejected(
        "UPDATE payments_paymentresolution SET reason='replacement' WHERE id=%s", [resolution.pk]
    )
    rejected(
        "UPDATE payments_paymentcontext SET status='CAPTURING' WHERE id=%s", [graph.context.pk]
    )
    with pytest.raises(DatabaseError), transaction.atomic():
        raw_insert(
            PaymentAllocation,
            gateway_transaction=captured,
            booking_charge=graph.charge,
            amount_minor=1,
            currency="INR",
            idempotency_key=uuid.uuid4(),
        )


def test_unmatched_capture_is_retained_and_match_is_attributable(setup):
    graph = booking_graph(setup)
    start_payment(graph, stock(setup))
    begin_capture(graph)
    captured = receipt(graph, attempt=None)
    assert captured.attempt_id is None
    with pytest.raises((ValidationError, DatabaseError)), transaction.atomic():
        receipt(graph, attempt=None)
    match = GatewayTransactionMatch.objects.create(
        gateway_transaction=captured,
        attempt=graph.attempt,
        system_actor="fictional-matcher",
        reason="Verified order identity",
        evidence_reference="test-only",
    )
    PaymentAllocation.objects.create(
        gateway_transaction=captured,
        booking_charge=graph.charge,
        amount_minor=100,
        currency="INR",
        idempotency_key=uuid.uuid4(),
    )
    captured.refresh_from_db()
    assert captured.attempt_id is None
    rejected("UPDATE payments_gatewaytransactionmatch SET reason='rewrite' WHERE id=%s", [match.pk])


@pytest.mark.parametrize(
    "field,value", [("provider_account", "other"), ("currency", "USD"), ("environment", "LIVE")]
)
def test_later_receipt_match_rejects_wrong_identity(setup, field, value):
    graph = booking_graph(setup)
    start_payment(graph, stock(setup))
    captured = receipt(graph, attempt=None, **{field: value})
    with pytest.raises(DatabaseError), transaction.atomic():
        raw_insert(
            GatewayTransactionMatch,
            gateway_transaction=captured,
            attempt=graph.attempt,
            system_actor="fictional-matcher",
            reason="test",
            evidence_reference="test",
        )


@pytest.mark.parametrize("state", ["revoked", "expired"])
def test_grant_cannot_be_issued_against_closed_session(setup, state):
    graph = booking_graph(setup)
    if state == "revoked":
        graph.session.revoked_at = timezone.now()
        graph.session.save()
    else:
        # A newly constructed already-expired session models expiry without rewriting identity.
        from trials.models import GuestBrowserSession

        graph.session = GuestBrowserSession.objects.create(
            token_digest=uuid.uuid4().hex * 2,
            issued_at=timezone.now() - timedelta(hours=2),
            expires_at=timezone.now() - timedelta(hours=1),
        )
    values = dict(
        booking=graph.booking,
        browser_session=graph.session,
        issued_at=graph.session.issued_at,
        expires_at=graph.session.expires_at,
    )
    with pytest.raises(ValidationError):
        GuestBookingAccessGrant.objects.create(**values)
    with pytest.raises(DatabaseError), transaction.atomic():
        raw_insert(GuestBookingAccessGrant, **values)


@pytest.mark.parametrize(
    "reference", ["market", "hub", "material", "category", "product", "variant"]
)
def test_inactive_references_rejected_at_hold_acquisition(setup, reference):
    graph = booking_graph(setup)
    verify(graph)
    open_context(graph)
    unit = stock(setup)
    target = getattr(setup, reference)
    with connection.cursor() as cursor:
        cursor.execute(
            f"UPDATE {target._meta.db_table} SET status='COMING_SOON' WHERE id=%s", [target.pk]
        )
    from inventory.models import InventoryReservation

    with pytest.raises(DatabaseError), transaction.atomic():
        raw_insert(InventoryReservation, payment_context=graph.context, unit=unit)


def test_policy_identity_checked_through_models_and_sql(setup):
    values = dict(
        code="wrong-plan",
        version=1,
        market=setup.market,
        category=setup.category,
        policy_revision=setup.charge_policy,
        policy_snapshot={"test": True},
        effective_from=timezone.now(),
    )
    with pytest.raises(ValidationError):
        TrialPlanRevision.objects.create(**values)
    with pytest.raises(DatabaseError), transaction.atomic():
        raw_insert(TrialPlanRevision, **values)


def test_unattributed_resolution_cannot_be_used(setup):
    graph = booking_graph(setup)
    start_payment(graph, stock(setup))
    values = dict(
        context=graph.context,
        reason="test",
        evidence_reference="test",
        outcome="NO_CAPTURE",
        captured_minor=0,
        allocated_minor=0,
        currency="INR",
    )
    with pytest.raises(ValidationError):
        PaymentResolution.objects.create(**values)
    with pytest.raises(DatabaseError), transaction.atomic():
        raw_insert(PaymentResolution, **values)


def test_policy_scope_is_not_silently_reinterpreted(setup):
    wrong = BusinessConfiguration.objects.create(
        namespace="trials",
        key="plan-policy-reference",
        scope={"market": "OTHER-FICTIONAL-CITY"},
        value="fictional-policy",
    )
    values = dict(
        code="scoped-plan",
        version=1,
        market=setup.market,
        category=setup.category,
        policy_revision=wrong,
        policy_snapshot={"test_only": True},
        effective_from=timezone.now(),
    )
    with pytest.raises(ValidationError):
        TrialPlanRevision.objects.create(**values)
    with pytest.raises(DatabaseError), transaction.atomic():
        raw_insert(TrialPlanRevision, **values)
    correct = BusinessConfiguration.objects.create(
        namespace="trials",
        key="plan-policy-reference",
        scope={"market": str(setup.market.public_id), "category": str(setup.category.public_id)},
        value="fictional-policy",
    )
    values["policy_revision"] = correct
    plan = TrialPlanRevision.objects.create(**values)
    assert plan.policy_revision.status == "DRAFT"


def test_deactivation_between_capture_and_commitment_retains_money_and_hold(setup):
    graph = booking_graph(setup)
    start_payment(graph, stock(setup))
    begin_capture(graph)
    captured = receipt(graph)
    PaymentAllocation.objects.create(
        gateway_transaction=captured,
        booking_charge=graph.charge,
        amount_minor=100,
        currency="INR",
        idempotency_key=uuid.uuid4(),
    )
    graph.attempt.status = "SUCCEEDED"
    graph.attempt.succeeded_at = timezone.now()
    graph.attempt.last_verified_at = timezone.now()
    graph.attempt.save()
    with connection.cursor() as cursor:
        cursor.execute(
            "UPDATE catalog_material SET status='INACTIVE' WHERE id=%s", [setup.material.pk]
        )
    rejected(
        "UPDATE inventory_inventoryreservation SET status='COMMITTED',committed_at=now() "
        "WHERE id=%s",
        [graph.hold.pk],
    )
    graph.hold.refresh_from_db()
    assert graph.hold.status == "CAPTURE_PENDING"
    assert captured.allocations.get().amount_minor == 100
    # Successful collection is retained even when fulfilment cannot be accepted.
    graph.charge.status = "COLLECTED"
    graph.charge.save()
    reconcile(graph, captured_minor=100, allocated_minor=100)
    graph.booking.status = "CANCELLED"
    graph.booking.save()
    graph.attempt.refresh_from_db()
    graph.charge.refresh_from_db()
    assert graph.attempt.status == "SUCCEEDED"
    assert graph.charge.status == "COLLECTED"
    assert captured.allocations.get().amount_minor == 100
