"""Persisted checkout invariants. All amounts, contacts and times are fictional fixtures."""

import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from types import SimpleNamespace

import pytest
from django.contrib.auth.hashers import make_password
from django.core.exceptions import ValidationError
from django.db import DatabaseError, close_old_connections, connection, transaction
from django.db.models import QuerySet
from django.utils import timezone

from accounts.models import CustomerAccount
from catalog.models import Category, Material, Product, ProductVariant
from catalog.storefront import public_variants
from compliance.models import BusinessConfiguration, LegalEntity, PolicyDocumentRevision
from inventory.models import InventoryReservation, InventoryUnit
from markets.models import Hub, Market
from notifications.models import NotificationAttempt, NotificationEvent, OutboxCommand
from payments.models import (
    BookingCharge,
    GatewayEvent,
    GatewayTransaction,
    PaymentAllocation,
    PaymentAttempt,
    PaymentContext,
    PaymentResolution,
    PaymentResolutionReceipt,
)
from trials.models import (
    BookingPhoneChallenge,
    BookingPolicyAcceptance,
    GuestBookingAccessGrant,
    GuestBookingRecoveryGrant,
    GuestBrowserSession,
    GuestRecoveryPhoneChallenge,
    IdempotencyRecord,
    TrialBooking,
    TrialBox,
    TrialBoxItem,
    TrialItemAllocation,
    TrialPlanRevision,
)

pytestmark = pytest.mark.django_db


def rejected(sql, params):
    with pytest.raises(DatabaseError), transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute(sql, params)


def raw_insert(model, **values):
    return QuerySet(model=model).bulk_create([model(**values)])[0]


@pytest.fixture
def setup():
    market = Market.objects.create(code="CHECKOUT-TEST", name="Fictional city", country_code="IN")
    category = Category.objects.create(code="CHECKOUT-RING", name="Fictional rings")
    material = Material.objects.create(code="CHECKOUT-METAL", name="Fictional metal")
    product = Product.objects.create(
        code="CHECKOUT-PRODUCT", name="Fictional design", category=category
    )
    variant = ProductVariant.objects.create(
        sku="CHECKOUT-VARIANT", product=product, material=material
    )
    hub = Hub.objects.create(code="CHECKOUT-HUB", name="Fictional hub", market=market)
    # Disposable activation fixtures are not an operational approval workflow.
    with connection.cursor() as cursor:
        for reference in (market, category, material, product, variant, hub):
            cursor.execute(
                f"UPDATE {reference._meta.db_table} SET status='ACTIVE' WHERE id=%s", [reference.pk]
            )
            reference.refresh_from_db()
    plan_policy = BusinessConfiguration.objects.create(
        namespace="trials", key="plan-policy-reference", value="fictional-plan-policy"
    )
    charge_policy = BusinessConfiguration.objects.create(
        namespace="payments", key="booking-charge-policy-reference", value="fictional-charge-policy"
    )
    plan = TrialPlanRevision.objects.create(
        code="test-plan",
        version=1,
        market=market,
        category=category,
        policy_revision=plan_policy,
        policy_snapshot={"test_only": True},
        effective_from=timezone.now(),
    )
    entity = LegalEntity.objects.create(
        code="test-payee", registered_name="Fictional payee", country_code="IN"
    )
    policy = PolicyDocumentRevision.objects.create(
        document_code="test-terms", language_code="en", revision=1, content="Fictional test terms."
    )
    return SimpleNamespace(**locals())


def booking_graph(setup, *, account=None, quantity=1):
    now = timezone.now()
    booking = TrialBooking.objects.create(
        market=setup.market,
        checkout_mode="ACCOUNT" if account else "GUEST",
        customer_account=account,
        contact_name="Fictional guest",
        contact_phone="+919000000010",
        address="Fictional address",
        country_code="IN",
        postal_code="560001",
        request_fingerprint="a" * 64,
    )
    box = TrialBox.objects.create(
        booking=booking,
        category=setup.category,
        plan_revision=setup.plan,
        plan_snapshot={"test_only": True},
    )
    item = TrialBoxItem.objects.create(
        box=box,
        variant=setup.variant,
        quantity=quantity,
        selection_snapshot={"variant": str(setup.variant.public_id)},
    )
    BookingPolicyAcceptance.objects.create(
        booking=booking,
        policy_revision=setup.policy,
        accepted_at=now,
        request_id=uuid.uuid4(),
        request_fingerprint="b" * 64,
    )
    challenge = BookingPhoneChallenge.objects.create(
        booking=booking,
        phone=booking.contact_phone,
        secret_hash=make_password("fictional-code"),
        issued_at=now,
        expires_at=now + timedelta(minutes=3),
        max_attempts=3,
    )
    charge = BookingCharge.objects.create(
        booking=booking,
        revision=1,
        payee_legal_entity=setup.entity,
        payee_snapshot={"name": setup.entity.registered_name},
        policy_revision=setup.charge_policy,
        policy_snapshot={"test_only": True},
        amount_minor=100,
        currency="INR",
        quote_expires_at=now + timedelta(minutes=4),
    )
    session = grant = None
    if account is None:
        session = GuestBrowserSession.objects.create(
            token_digest=uuid.uuid4().hex * 2, issued_at=now, expires_at=now + timedelta(hours=2)
        )
        grant = GuestBookingAccessGrant.objects.create(
            booking=booking, browser_session=session, issued_at=now, expires_at=session.expires_at
        )
    return SimpleNamespace(**locals())


def verify(graph):
    graph.challenge.attempts_used = 1
    graph.challenge.verified_at = timezone.now()
    graph.challenge.consumed_at = timezone.now()
    graph.challenge.save()
    graph.booking.sealed_at = timezone.now()
    graph.booking.acceptance_snapshot = {"test_only": True}
    graph.booking.verified_phone_challenge = graph.challenge
    graph.booking.verification_valid_until = timezone.now() + timedelta(minutes=4)
    graph.booking.status = "VERIFIED"
    graph.booking.save()


def open_context(graph):
    graph.charge.status = "DUE"
    graph.charge.accepted_at = timezone.now()
    graph.charge.save()
    context = PaymentContext.objects.create(
        booking_charge=graph.charge,
        version=1,
        expires_at=timezone.now() + timedelta(minutes=2),
        policy_snapshot={"test_only": True},
    )
    graph.context = context
    return context


def stock(setup):
    unit = InventoryUnit.objects.create(variant=setup.variant, hub=setup.hub)
    # Fixture setup only: operational receiving/QC is covered by its own service tests.
    with connection.cursor() as cursor:
        cursor.execute(
            "UPDATE inventory_inventoryunit SET status='AVAILABLE',custody='HUB' WHERE id=%s",
            [unit.pk],
        )
    unit.refresh_from_db()
    return unit


def reserve(graph, unit):
    hold = InventoryReservation.objects.create(payment_context=graph.context, unit=unit)
    allocation = TrialItemAllocation.objects.create(trial_box_item=graph.item, reservation=hold)
    return hold, allocation


def start_payment(graph, unit):
    verify(graph)
    open_context(graph)
    graph.hold, graph.allocation = reserve(graph, unit)
    graph.attempt = PaymentAttempt.objects.create(
        context=graph.context,
        request_id=uuid.uuid4(),
        request_fingerprint="c" * 64,
        provider="FICTIONAL",
        provider_account="test-account",
        environment="TEST",
        amount_minor=100,
        currency="INR",
    )
    graph.booking.status = "PAYMENT_PENDING"
    graph.booking.save()
    graph.attempt.provider_order_id = str(uuid.uuid4())
    graph.attempt.status = "PENDING"
    graph.attempt.save()


def receipt(graph, kind="CAPTURE", **overrides):
    values = dict(
        attempt=graph.attempt,
        provider="FICTIONAL",
        provider_account="test-account",
        environment="TEST",
        provider_payment_id="test-payment",
        transaction_kind=kind,
        amount_minor=100,
        currency="INR",
        occurred_at=timezone.now(),
        verified_at=timezone.now(),
        verification_reference="test-only-provider-evidence",
    )
    return GatewayTransaction.objects.create(**(values | overrides))


def begin_capture(graph):
    receipt(graph, "AUTHORISATION")
    graph.attempt.status = "AUTHORIZED"
    graph.attempt.last_verified_at = timezone.now()
    graph.attempt.save()
    graph.context.status = "CAPTURING"
    graph.context.capture_started_at = timezone.now()
    graph.context.save()
    graph.hold.status = "CAPTURE_PENDING"
    graph.hold.capture_started_at = graph.context.capture_started_at
    graph.hold.save()
    graph.attempt.status = "CAPTURE_PENDING"
    graph.attempt.capture_requested_at = timezone.now()
    graph.attempt.save()


def collect(graph):
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
    graph.hold.status = "COMMITTED"
    graph.hold.committed_at = timezone.now()
    graph.hold.save()
    graph.charge.status = "COLLECTED"
    graph.charge.save()
    graph.context.status = "PAID"
    graph.context.paid_at = timezone.now()
    graph.context.save()
    graph.booking.status = "CONFIRMED"
    graph.booking.confirmed_at = timezone.now()
    graph.booking.save()
    return captured


def reconcile(graph, *, captured_minor=0, allocated_minor=0, release=True, outcome=None):
    observed = timezone.now()
    if graph.attempt.status != "SUCCEEDED":
        graph.attempt.status = "REVIEW_REQUIRED"
    graph.attempt.last_verified_at = observed
    graph.attempt.save()
    resolution = PaymentResolution.objects.create(
        context=graph.context,
        system_actor="fictional-reconciliation",
        reason="Fictional provider verification; commercial treatment remains pending",
        evidence_reference="test-only-reconciliation",
        reconciled_at=observed,
        outcome=outcome or ("CAPTURE_NOT_ACCEPTED" if captured_minor else "NO_CAPTURE"),
        captured_minor=captured_minor,
        allocated_minor=allocated_minor,
        currency="INR",
    )
    for captured in graph.attempt.transactions.filter(transaction_kind="CAPTURE"):
        PaymentResolutionReceipt.objects.create(resolution=resolution, gateway_transaction=captured)
    if graph.context.status == "CAPTURING":
        graph.context.status = "REVIEW_REQUIRED"
        graph.context.save()
    graph.context.status = "CANCELLED"
    graph.context.closed_at = timezone.now()
    graph.context.resolution = resolution
    graph.context.save()
    if not release:
        return resolution
    graph.hold.status = "RELEASED"
    graph.hold.released_at = timezone.now()
    graph.hold.release_reason = "Reconciled unfulfilled test booking"
    graph.hold.resolution = resolution
    graph.hold.save()
    graph.allocation.released_at = timezone.now()
    graph.allocation.save()
    return resolution


@pytest.mark.parametrize("account_mode", [False, True])
def test_model_graph_reaches_confirmed_only_with_verified_collection(setup, account_mode):
    account = CustomerAccount.objects.create() if account_mode else None
    graph = booking_graph(setup, account=account)
    start_payment(graph, stock(setup))
    begin_capture(graph)
    captured = collect(graph)
    assert graph.booking.customer_account_id == (account.pk if account else None)
    assert graph.booking.status == "CONFIRMED"
    assert captured.allocations.count() == 1
    assert graph.hold.status == "COMMITTED"
    rejected("UPDATE payments_gatewaytransaction SET amount_minor=1 WHERE id=%s", [captured.pk])
    rejected(
        "DELETE FROM payments_paymentallocation WHERE gateway_transaction_id=%s", [captured.pk]
    )
    rejected("UPDATE trials_trialbooking SET status='DRAFT' WHERE id=%s", [graph.booking.pk])


def test_draft_and_otp_verification_do_not_reserve_any_stock(setup):
    graph = booking_graph(setup)
    verify(graph)
    assert not InventoryReservation.objects.exists()
    assert not PaymentContext.objects.exists()
    with pytest.raises((ValidationError, DatabaseError)), transaction.atomic():
        PaymentContext.objects.create(
            booking_charge=graph.charge,
            version=1,
            expires_at=timezone.now() + timedelta(minutes=1),
            policy_snapshot={"test": True},
        )


def test_unverified_booking_cannot_accept_charge_or_hold_stock(setup):
    graph = booking_graph(setup)
    rejected(
        "UPDATE payments_bookingcharge SET status='DUE',accepted_at=now() WHERE id=%s",
        [graph.charge.pk],
    )


def test_cross_booking_phone_proof_and_sealed_content_are_rejected(setup):
    first, second = booking_graph(setup), booking_graph(setup)
    verify(first)
    rejected(
        "UPDATE trials_trialbooking SET verified_phone_challenge_id=%s WHERE id=%s",
        [first.challenge.pk, second.booking.pk],
    )
    with pytest.raises(ValidationError):
        TrialBoxItem.objects.create(
            box=first.box, variant=setup.variant, quantity=1, selection_snapshot={"test": True}
        )
    with pytest.raises(DatabaseError), transaction.atomic():
        raw_insert(
            TrialBoxItem,
            box=first.box,
            variant=setup.variant,
            quantity=1,
            selection_snapshot={"test": True},
        )


def test_guest_session_rotation_preserves_multiple_booking_grants(setup):
    first, second = booking_graph(setup), booking_graph(setup)
    GuestBookingAccessGrant.objects.create(
        booking=second.booking, browser_session=first.session, expires_at=first.session.expires_at
    )
    first.session.token_digest = "d" * 64
    first.session.rotated_at = timezone.now()
    first.session.save()
    assert first.session.booking_grants.count() == 2
    assert "token_digest" not in {f.name for f in GuestBookingAccessGrant._meta.fields}
    rejected(
        "UPDATE trials_guestbrowsersession SET token_digest=%s WHERE id=%s",
        ["e" * 64, first.session.pk],
    )
    first.session.revoked_at = timezone.now()
    first.session.save()
    rejected(
        "UPDATE trials_guestbrowsersession SET revoked_at=NULL WHERE id=%s", [first.session.pk]
    )


def test_idempotency_is_scoped_and_does_not_link_guest_to_account(setup):
    graph = booking_graph(setup)
    key = uuid.uuid4()
    record = IdempotencyRecord.objects.create(
        guest_session=graph.session,
        scope="CREATE_BOOKING",
        key=key,
        request_fingerprint="a" * 64,
        booking=graph.booking,
    )
    with pytest.raises(ValidationError):
        IdempotencyRecord.objects.create(
            guest_session=graph.session,
            scope="CREATE_BOOKING",
            key=key,
            request_fingerprint="b" * 64,
        )
    other = booking_graph(setup)
    IdempotencyRecord.objects.create(
        guest_session=other.session, scope="CREATE_BOOKING", key=key, request_fingerprint="a" * 64
    )
    rejected(
        "UPDATE trials_idempotencyrecord SET request_fingerprint=%s WHERE id=%s",
        ["b" * 64, record.pk],
    )
    with pytest.raises(DatabaseError), transaction.atomic():
        raw_insert(
            IdempotencyRecord,
            customer_account=CustomerAccount.objects.create(),
            scope="CREATE_BOOKING",
            key=uuid.uuid4(),
            request_fingerprint="a" * 64,
            booking=graph.booking,
        )


def test_recovery_requires_its_own_single_use_phone_proof(setup):
    graph = booking_graph(setup)
    recovery = GuestBookingRecoveryGrant.objects.create(
        booking=graph.booking,
        token_digest="f" * 64,
        expires_at=timezone.now() + timedelta(minutes=5),
    )
    rejected(
        "UPDATE trials_guestbookingrecoverygrant "
        "SET redeemed_at=now(),redeemed_session_id=%s WHERE id=%s",
        [graph.session.pk, recovery.pk],
    )
    proof = GuestRecoveryPhoneChallenge.objects.create(
        recovery_grant=recovery,
        phone=graph.booking.contact_phone,
        secret_hash=make_password("fictional-recovery-code"),
        expires_at=timezone.now() + timedelta(minutes=2),
        max_attempts=3,
    )
    proof.attempts_used = 1
    proof.verified_at = timezone.now()
    proof.consumed_at = timezone.now()
    proof.save()
    recovery.verified_challenge = proof
    recovery.redeemed_session = graph.session
    recovery.redeemed_at = timezone.now()
    recovery.save()
    rejected(
        "UPDATE trials_guestbookingrecoverygrant SET redeemed_at=NULL WHERE id=%s", [recovery.pk]
    )
    rejected(
        "UPDATE trials_guestrecoveryphonechallenge SET consumed_at=NULL WHERE id=%s", [proof.pk]
    )


def test_reservation_checks_city_variant_and_requested_quantity(setup):
    graph = booking_graph(setup)
    verify(graph)
    open_context(graph)
    reserve(graph, stock(setup))
    extra = InventoryReservation.objects.create(payment_context=graph.context, unit=stock(setup))
    with pytest.raises(DatabaseError), transaction.atomic():
        raw_insert(TrialItemAllocation, trial_box_item=graph.item, reservation=extra)
    city = Market.objects.create(code="OTHER-CITY", name="Other fictional city", country_code="IN")
    hub = Hub.objects.create(code="OTHER-HUB", name="Other hub", market=city)
    unit = InventoryUnit.objects.create(variant=setup.variant, hub=hub)
    with pytest.raises((ValidationError, DatabaseError)), transaction.atomic():
        InventoryReservation.objects.create(payment_context=graph.context, unit=unit)


@pytest.mark.django_db(transaction=True)
def test_concurrent_reservations_cannot_commit_the_same_unit(setup):
    graphs = [booking_graph(setup), booking_graph(setup)]
    for graph in graphs:
        verify(graph)
        open_context(graph)
    unit = stock(setup)
    barrier = Barrier(2)

    def attempt(graph):
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            with transaction.atomic():
                raw_insert(
                    InventoryReservation, unit_id=unit.pk, payment_context_id=graph.context.pk
                )
            return "held"
        except DatabaseError:
            return "conflict"
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(attempt, graphs)) == ["conflict", "held"]


def test_capture_uncertainty_cannot_expire_or_release_inventory(setup):
    graph = booking_graph(setup)
    start_payment(graph, stock(setup))
    begin_capture(graph)
    rejected(
        "UPDATE payments_paymentcontext SET status='EXPIRED',closed_at=now() WHERE id=%s",
        [graph.context.pk],
    )
    rejected(
        "UPDATE inventory_inventoryreservation "
        "SET status='EXPIRED',released_at=now(),release_reason='idle' WHERE id=%s",
        [graph.hold.pk],
    )
    rejected(
        "UPDATE trials_trialbooking SET status='CONFIRMED',confirmed_at=now() WHERE id=%s",
        [graph.booking.pk],
    )


def test_expired_context_retains_late_receipt_without_reopening_booking(setup):
    graph = booking_graph(setup)
    start_payment(graph, stock(setup))
    reconcile(graph)
    late = receipt(graph)
    with pytest.raises(DatabaseError), transaction.atomic():
        PaymentAllocation.objects.create(
            gateway_transaction=late,
            booking_charge=graph.charge,
            amount_minor=100,
            currency="INR",
            idempotency_key=uuid.uuid4(),
        )
    assert GatewayTransaction.objects.filter(pk=late.pk).exists()
    assert graph.hold.status == "RELEASED"


@pytest.mark.parametrize("amount", [0, -1, 1.25, True, "100"])
def test_money_refuses_nonpositive_or_coerced_amounts(setup, amount):
    graph = booking_graph(setup)
    with pytest.raises(ValidationError):
        BookingCharge.objects.create(
            booking=graph.booking,
            revision=2,
            payee_legal_entity=setup.entity,
            payee_snapshot={"test": True},
            policy_revision=setup.charge_policy,
            policy_snapshot={"test": True},
            amount_minor=amount,
            currency="INR",
            quote_expires_at=timezone.now() + timedelta(minutes=2),
        )
    with pytest.raises((ValidationError, DatabaseError)), transaction.atomic():
        raw_insert(
            BookingCharge,
            booking=graph.booking,
            revision=2,
            payee_legal_entity=setup.entity,
            payee_snapshot={"test": True},
            policy_revision=setup.charge_policy,
            policy_snapshot={"test": True},
            amount_minor=amount,
            currency="INR",
            quote_expires_at=timezone.now() + timedelta(minutes=2),
        )


def test_duplicate_and_cross_environment_provider_evidence_rejected(setup):
    graph = booking_graph(setup)
    start_payment(graph, stock(setup))
    begin_capture(graph)
    capture = receipt(graph)
    with pytest.raises(ValidationError):
        receipt(graph)
    with pytest.raises(DatabaseError), transaction.atomic():
        receipt(graph, environment="LIVE")
    event_values = dict(
        provider="FICTIONAL",
        provider_account="test-account",
        environment="TEST",
        provider_event_id="event-1",
        event_type="payment.captured",
        payload_digest="a" * 64,
        verified_at=timezone.now(),
    )
    GatewayEvent.objects.create(**event_values)
    with pytest.raises(ValidationError):
        GatewayEvent.objects.create(**event_values)
    assert capture.transaction_kind == "CAPTURE"


@pytest.mark.django_db(transaction=True)
def test_concurrent_receipts_cannot_overallocate_one_charge(setup):
    graph = booking_graph(setup)
    start_payment(graph, stock(setup))
    begin_capture(graph)
    receipts = [receipt(graph, provider_payment_id=str(uuid.uuid4())) for _ in range(2)]
    barrier = Barrier(2)

    def allocate(captured):
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            with transaction.atomic():
                raw_insert(
                    PaymentAllocation,
                    gateway_transaction_id=captured.pk,
                    booking_charge_id=graph.charge.pk,
                    amount_minor=100,
                    currency="INR",
                    idempotency_key=uuid.uuid4(),
                )
            return "allocated"
        except DatabaseError:
            return "rejected"
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(allocate, receipts)) == ["allocated", "rejected"]
    assert GatewayTransaction.objects.filter(transaction_kind="CAPTURE").count() == 2


def test_notifications_bind_same_challenge_and_outbox_deduplicates(setup):
    graph = booking_graph(setup)
    event = NotificationEvent.objects.create(
        booking=graph.booking,
        purpose="BOOKING_PHONE_OTP",
        booking_challenge=graph.challenge,
        recipient_phone=graph.booking.contact_phone,
        template_reference="test-otp-v1",
        dedupe_key=uuid.uuid4(),
        expires_at=graph.challenge.expires_at,
        payload_reference="test-protected-payload",
    )
    for channel in ("SMS", "WHATSAPP"):
        attempt = NotificationAttempt.objects.create(
            event=event,
            number=1,
            channel=channel,
            provider="FICTIONAL",
            provider_account="test-account",
            environment="TEST",
        )
        command = OutboxCommand.objects.create(
            operation="SEND_NOTIFICATION", notification_attempt=attempt, dedupe_key=uuid.uuid4()
        )
        with pytest.raises(ValidationError):
            OutboxCommand.objects.create(
                operation="SEND_NOTIFICATION",
                notification_attempt=attempt,
                dedupe_key=command.dedupe_key,
            )
    assert event.attempts.count() == 2
    assert event.booking_challenge_id == graph.challenge.pk
    other = booking_graph(setup)
    with pytest.raises(DatabaseError), transaction.atomic():
        raw_insert(
            NotificationEvent,
            booking=other.booking,
            purpose="BOOKING_PHONE_OTP",
            booking_challenge=graph.challenge,
            recipient_phone=other.booking.contact_phone,
            template_reference="test-only",
            dedupe_key=uuid.uuid4(),
            expires_at=graph.challenge.expires_at,
        )


def test_reserved_hub_stock_is_not_publicly_available_and_cannot_change_disposition(setup):
    for model, instance in (
        (Market, setup.market),
        (Hub, setup.hub),
        (Material, setup.material),
        (Category, setup.category),
        (Product, setup.product),
        (ProductVariant, setup.variant),
    ):
        # Fixture activation is not a production approval workflow.
        QuerySet(model=model).filter(pk=instance.pk).update(status="ACTIVE")
    unit = stock(setup)
    assert public_variants(setup.market, None).get(pk=setup.variant.pk).at_hub is True
    graph = booking_graph(setup)
    start_payment(graph, unit)
    assert public_variants(setup.market, None).get(pk=setup.variant.pk).at_hub is False
    rejected("UPDATE inventory_inventoryunit SET status='QUARANTINED' WHERE id=%s", [unit.pk])


def test_partial_selection_cannot_start_provider_payment(setup):
    graph = booking_graph(setup, quantity=2)
    verify(graph)
    open_context(graph)
    reserve(graph, stock(setup))
    rejected(
        "UPDATE trials_trialbooking SET status='PAYMENT_PENDING' WHERE id=%s", [graph.booking.pk]
    )
    with pytest.raises(DatabaseError), transaction.atomic():
        raw_insert(
            PaymentAttempt,
            context=graph.context,
            request_id=uuid.uuid4(),
            request_fingerprint="c" * 64,
            provider="FICTIONAL",
            provider_account="test-account",
            environment="TEST",
            amount_minor=100,
            currency="INR",
        )
