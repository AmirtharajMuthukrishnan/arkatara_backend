"""Physical return/QC and final payment boundaries, using fictional reviewed evidence."""

import uuid
from datetime import timedelta
from types import SimpleNamespace

import pytest
from django.contrib.auth.hashers import make_password
from django.core.exceptions import ValidationError
from django.db import DatabaseError, connection, transaction
from django.utils import timezone

from accounts.models import StaffUser
from billing.models import (
    DocumentSeries,
    FinancialDocument,
    FinancialDocumentLine,
    Purchase,
    PurchaseBalanceEvidence,
    PurchaseItem,
    PurchaseObligation,
    TaxEvidence,
)
from delivery.models import (
    ArrivalOTP,
    DeliveryAssignment,
    HomeVisit,
    ReservationCustodyEvent,
    VisitEvidence,
)
from inventory.models import InventoryReservation
from payments.models import (
    PaymentAllocation,
    PaymentAttempt,
    PaymentContext,
    PaymentResolution,
    PaymentResolutionReceipt,
)
from tests.test_checkout_models import (
    begin_capture,
    booking_graph,
    collect,
    raw_insert,
    receipt,
    rejected,
    start_payment,
    stock,
)
from tests.test_checkout_models import setup as setup

pytestmark = pytest.mark.django_db


def issued_dispatch(graph, setup, actor):
    tax = TaxEvidence.objects.create(
        status="REVIEWED",
        treatment_snapshot={"fictional": True},
        reviewed_by=actor,
        reviewed_at=timezone.now(),
        review_reference="test-only-tax-review",
    )
    series = DocumentSeries.objects.create(
        issuer=setup.entity,
        code="fictional-series",
        scope_snapshot={"test_only": True},
        review_reference="test-only-numbering-review",
    )
    document = FinancialDocument.objects.create(
        booking=graph.booking,
        kind="DISPATCH",
        series=series,
        issuer_snapshot={"name": setup.entity.registered_name},
        recipient_snapshot={"test": True},
        tax_evidence=tax,
        amount_minor=0,
        currency="INR",
        review_reference="test-only-document-review",
    )
    FinancialDocumentLine.objects.create(
        document=document,
        line_number=1,
        amount_minor=0,
        reservation=graph.hold,
        snapshot={"test_only_manifest": True},
    )
    document.status = "ISSUED"
    document.number = 1
    document.issued_at = timezone.now()
    document.save()
    return document, tax, series


def observe(graph, kind, *, purchase_item=None):
    previous = graph.hold.custody_events.order_by("-id").first()
    with transaction.atomic():
        event = ReservationCustodyEvent.objects.create(
            reservation=graph.hold,
            visit=graph.visit,
            previous=previous,
            kind=kind,
            observed_at=timezone.now(),
            actor=graph.actor,
            reason="Fictional physical observation",
            evidence_reference="test-only-observation",
            policy_snapshot={"fictional_review": True},
            purchase_item=purchase_item,
            idempotency_key=uuid.uuid4(),
        )
        states = {
            "DISPATCH": ("OUT_FOR_DELIVERY", "AGENT"),
            "RETURNING": ("RETURNING_TO_HUB", "AGENT"),
            "HUB_RECEIPT": ("QC_PENDING", "HUB"),
            "QC_PASS": ("AVAILABLE", "HUB"),
            "QC_FAIL": ("QUARANTINED", "HUB"),
            "SALE": ("SOLD", "CUSTOMER"),
        }
        state, custody = states[kind]
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE inventory_inventoryunit SET status=%s,custody=%s WHERE id=%s",
                [state, custody, graph.hold.unit_id],
            )
        # Exercise the deferred consistency check within pytest's surrounding transaction.
        with connection.cursor() as cursor:
            cursor.execute("SET CONSTRAINTS custody_stock_consistency IMMEDIATE")
            cursor.execute("SET CONSTRAINTS custody_stock_consistency DEFERRED")
    return event


def record_visit(graph, *, kind="STATE", outcome=None):
    """Explicit fictional attribution for the intended visit projection."""
    visit = graph.visit
    with transaction.atomic():
        evidence = VisitEvidence.objects.create(
            visit=visit,
            previous=visit.current_evidence,
            assignment=visit.assignment,
            kind=kind,
            status=visit.status,
            outcome=visit.outcome if outcome is None else outcome,
            observed_at=timezone.now(),
            actor=graph.actor,
            reason="Fictional observed visit progression",
            evidence_reference="test-only-visit-observation",
            policy_snapshot={"fictional_review": True},
        )
        visit.current_evidence = evidence
        visit.save()
    return evidence


def trial_graph(setup):
    graph = booking_graph(setup)
    start_payment(graph, stock(setup))
    begin_capture(graph)
    collect(graph)
    graph.actor = StaffUser.objects.create_user(username="fictional-agent", is_staff=True)
    graph.assignment = DeliveryAssignment.objects.create(
        booking=graph.booking,
        agent=graph.actor,
        assigned_by=graph.actor,
        assigned_at=timezone.now(),
    )
    graph.document, graph.tax, graph.series = issued_dispatch(graph, setup, graph.actor)
    with transaction.atomic():
        graph.visit = HomeVisit.objects.create(booking=graph.booking, assignment=graph.assignment)
        record_visit(graph)
    graph.visit.dispatch_document = graph.document
    graph.visit.status = "READY"
    record_visit(graph)
    observe(graph, "DISPATCH")
    graph.visit.status = "DISPATCHED"
    graph.visit.dispatched_at = timezone.now()
    record_visit(graph)
    challenge = ArrivalOTP.objects.create(
        assignment=graph.assignment,
        phone=graph.booking.contact_phone,
        secret_hash=make_password("fictional-arrival"),
        expires_at=timezone.now() + timedelta(minutes=3),
        max_attempts=3,
    )
    challenge.attempts_used = 1
    challenge.verified_at = timezone.now()
    challenge.consumed_at = timezone.now()
    challenge.save()
    graph.visit.arrival_challenge = challenge
    graph.visit.trial_started_at = timezone.now()
    graph.visit.status = "IN_PERSON_TRIAL"
    record_visit(graph)
    return graph


def purchase_graph(setup):
    graph = trial_graph(setup)
    purchase = Purchase.objects.create(
        booking=graph.booking,
        revision=1,
        seller=setup.entity,
        seller_snapshot={"fictional": True},
        customer_snapshot={"fictional": True},
        tax_evidence=graph.tax,
        currency="INR",
        jewellery_total_minor=500,
        payable_minor=500,
        balance_snapshot={"test_only": True},
    )
    item = PurchaseItem.objects.create(
        purchase=purchase,
        reservation=graph.hold,
        line_number=1,
        amount_minor=500,
        currency="INR",
        product_snapshot={"test_only": True},
        price_snapshot={"test_only": True},
    )
    PurchaseBalanceEvidence.objects.create(
        purchase=purchase,
        amount_due_before_application=500,
        adjustment_minor=0,
        currency="INR",
        actor=graph.actor,
        observed_at=timezone.now(),
        reason="Fictional reviewed balance",
        evidence_reference="test-only-balance",
        calculation_snapshot={"test_only": True},
        policy_snapshot={"test_only": True},
    )
    purchase.status = "SEALED"
    purchase.sealed_at = timezone.now()
    purchase.save()
    obligation = PurchaseObligation.objects.create(
        purchase=purchase,
        amount_minor=500,
        currency="INR",
        quote_expires_at=timezone.now() + timedelta(minutes=5),
    )
    obligation.status = "DUE"
    obligation.accepted_at = timezone.now()
    obligation.save()
    context = PaymentContext.objects.create(
        purchase_obligation=obligation,
        version=1,
        expires_at=timezone.now() + timedelta(minutes=3),
        policy_snapshot={"test_only": True},
    )
    attempt = PaymentAttempt.objects.create(
        context=context,
        request_id=uuid.uuid4(),
        request_fingerprint="a" * 64,
        provider="FICTIONAL",
        provider_account="test-account",
        environment="TEST",
        amount_minor=500,
        currency="INR",
    )
    graph.final = SimpleNamespace(
        purchase=purchase, item=item, obligation=obligation, context=context, attempt=attempt
    )
    return graph


def pay_purchase(graph):
    begin_purchase_capture(graph)
    final = graph.final
    captured = receipt(final, amount_minor=500, provider_payment_id="final-payment")
    PaymentAllocation.objects.create(
        gateway_transaction=captured,
        purchase_obligation=final.obligation,
        amount_minor=500,
        currency="INR",
        idempotency_key=uuid.uuid4(),
    )
    final.attempt.status = "SUCCEEDED"
    final.attempt.last_verified_at = timezone.now()
    final.attempt.succeeded_at = timezone.now()
    final.attempt.save()
    final.obligation.status = "COLLECTED"
    final.obligation.save()
    final.context.status = "PAID"
    final.context.paid_at = timezone.now()
    final.context.save()
    final.purchase.status = "CONFIRMED"
    final.purchase.confirmed_at = timezone.now()
    final.purchase.save()


def begin_purchase_capture(graph):
    final = graph.final
    final.attempt.status = "PENDING"
    final.attempt.provider_order_id = str(uuid.uuid4())
    final.attempt.save()
    receipt(final, "AUTHORISATION", amount_minor=500, provider_payment_id="final-payment")
    final.attempt.status = "AUTHORIZED"
    final.attempt.last_verified_at = timezone.now()
    final.attempt.save()
    final.context.status = "CAPTURING"
    final.context.capture_started_at = timezone.now()
    final.context.save()
    final.attempt.status = "CAPTURE_PENDING"
    final.attempt.capture_requested_at = timezone.now()
    final.attempt.save()


def close_commitment(graph, event):
    graph.hold.status = "RELEASED"
    graph.hold.disposition = event
    graph.hold.released_at = timezone.now()
    graph.hold.release_reason = "Recorded physical disposition"
    graph.hold.save()


def test_return_requires_receipt_and_qc_before_commitment_closes(setup):
    graph = trial_graph(setup)
    committed_at = graph.hold.committed_at
    returning = observe(graph, "RETURNING")
    with pytest.raises(DatabaseError), transaction.atomic():
        close_commitment(graph, returning)
    graph.hold.refresh_from_db()
    rejected(
        "UPDATE inventory_inventoryunit SET status='AVAILABLE',custody='HUB' WHERE id=%s",
        [graph.hold.unit_id],
    )
    observe(graph, "HUB_RECEIPT")
    passed = observe(graph, "QC_PASS")
    close_commitment(graph, passed)
    graph.hold.refresh_from_db()
    assert graph.hold.committed_at == committed_at
    graph.hold.unit.refresh_from_db()
    assert graph.hold.unit.status == "AVAILABLE"


def test_financial_confirmation_precedes_sale_and_sold_stock_stays_unavailable(setup):
    graph = purchase_graph(setup)
    with pytest.raises(DatabaseError), transaction.atomic():
        observe(graph, "SALE", purchase_item=graph.final.item)
    pay_purchase(graph)
    sold = observe(graph, "SALE", purchase_item=graph.final.item)
    close_commitment(graph, sold)
    graph.hold.unit.refresh_from_db()
    assert graph.hold.unit.status == "SOLD"
    # Same piece is never reservable solely because the old reservation closed.
    another = booking_graph(setup)
    from tests.test_checkout_models import open_context, verify

    verify(another)
    open_context(another)
    with pytest.raises(DatabaseError), transaction.atomic():
        raw_insert(InventoryReservation, payment_context=another.context, unit=graph.hold.unit)


def test_upfront_receipt_cannot_pay_purchase_obligation(setup):
    graph = purchase_graph(setup)
    captured = graph.attempt.transactions.get(transaction_kind="CAPTURE")
    with pytest.raises(DatabaseError), transaction.atomic():
        raw_insert(
            PaymentAllocation,
            gateway_transaction=captured,
            purchase_obligation=graph.final.obligation,
            amount_minor=100,
            currency="INR",
            idempotency_key=uuid.uuid4(),
        )
    with pytest.raises(ValidationError):
        PaymentContext.objects.create(
            booking_charge=graph.charge,
            purchase_obligation=graph.final.obligation,
            version=2,
            expires_at=timezone.now() + timedelta(minutes=2),
            policy_snapshot={"test": True},
        )


def test_issued_financial_document_seals_header_lines_and_tax_facts(setup):
    graph = purchase_graph(setup)
    pay_purchase(graph)
    document = FinancialDocument.objects.create(
        booking=graph.booking,
        purchase_obligation=graph.final.obligation,
        kind="TAX_INVOICE",
        series=graph.series,
        issuer_snapshot={"fictional_seller": True},
        recipient_snapshot={"fictional_customer": True},
        tax_evidence=graph.tax,
        amount_minor=500,
        currency="INR",
        review_reference="fictional-reviewed-format",
    )
    line = FinancialDocumentLine.objects.create(
        document=document,
        line_number=1,
        amount_minor=500,
        snapshot={"fictional_tax_and_price": True},
        purchase_item=graph.final.item,
    )
    document.status = "ISSUED"
    document.number = 2
    document.issued_at = timezone.now()
    document.save()
    rejected("UPDATE billing_financialdocument SET amount_minor=1 WHERE id=%s", [document.pk])
    rejected("UPDATE billing_financialdocumentline SET amount_minor=1 WHERE id=%s", [line.pk])
    rejected("UPDATE billing_taxevidence SET treatment_snapshot='{}' WHERE id=%s", [graph.tax.pk])
    with pytest.raises(DatabaseError), transaction.atomic():
        FinancialDocumentLine.objects.create(
            document=document, line_number=2, amount_minor=0, snapshot={"extra": True}
        )


def test_unconfigured_tax_evidence_does_not_become_zero_tax(setup):
    graph = trial_graph(setup)
    unconfigured = TaxEvidence.objects.create(status="UNCONFIGURED")
    document = FinancialDocument.objects.create(
        booking=graph.booking,
        booking_charge=graph.charge,
        kind="UPFRONT_RECORD",
        series=graph.series,
        issuer_snapshot={"fictional": True},
        recipient_snapshot={"fictional": True},
        tax_evidence=unconfigured,
        amount_minor=100,
        currency="INR",
        review_reference="test-only",
    )
    FinancialDocumentLine.objects.create(
        document=document, line_number=1, amount_minor=100, snapshot={"test": True}
    )
    document.status = "ISSUED"
    document.number = 2
    document.issued_at = timezone.now()
    with pytest.raises(DatabaseError), transaction.atomic():
        document.save()


@pytest.mark.parametrize("already_collected", [False, True])
def test_final_capture_uncertainty_protects_returned_selected_stock(setup, already_collected):
    graph = purchase_graph(setup)
    begin_purchase_capture(graph)
    final = graph.final
    captured = receipt(final, amount_minor=500, provider_payment_id="final-payment")
    if already_collected:
        PaymentAllocation.objects.create(
            gateway_transaction=captured,
            purchase_obligation=final.obligation,
            amount_minor=500,
            currency="INR",
            idempotency_key=uuid.uuid4(),
        )
        final.attempt.status = "SUCCEEDED"
        final.attempt.succeeded_at = timezone.now()
        final.attempt.last_verified_at = timezone.now()
        final.attempt.save()
        final.obligation.status = "COLLECTED"
        final.obligation.save()
    observe(graph, "RETURNING")
    observe(graph, "HUB_RECEIPT")
    passed = observe(graph, "QC_PASS")
    with pytest.raises(DatabaseError), transaction.atomic():
        close_commitment(graph, passed)
    graph.hold.refresh_from_db()
    if already_collected:
        rejected(
            "UPDATE payments_paymentcontext SET status='PAID',paid_at=now() WHERE id=%s",
            [final.context.pk],
        )
    else:
        with pytest.raises(DatabaseError), transaction.atomic():
            PaymentAllocation.objects.create(
                gateway_transaction=captured,
                purchase_obligation=final.obligation,
                amount_minor=500,
                currency="INR",
                idempotency_key=uuid.uuid4(),
            )
    observed = timezone.now()
    if not already_collected:
        final.attempt.status = "REVIEW_REQUIRED"
    final.attempt.last_verified_at = observed
    final.attempt.save()
    final.context.status = "REVIEW_REQUIRED"
    final.context.save()
    resolution = PaymentResolution.objects.create(
        context=final.context,
        system_actor="fictional-provider-reconciler",
        reason="Captured funds not accepted for returned selection",
        evidence_reference="test-only-final-outcome",
        reconciled_at=observed,
        outcome="CAPTURE_NOT_ACCEPTED",
        captured_minor=500,
        allocated_minor=500 if already_collected else 0,
        currency="INR",
    )
    PaymentResolutionReceipt.objects.create(resolution=resolution, gateway_transaction=captured)
    final.context.status = "CANCELLED"
    final.context.resolution = resolution
    final.context.closed_at = timezone.now()
    final.context.save()
    if not already_collected:
        final.obligation.status = "VOID"
        final.obligation.save()
    final.purchase.status = "VOID"
    final.purchase.save()
    close_commitment(graph, passed)
    assert graph.hold.status == "RELEASED"
    assert captured.allocations.count() == int(already_collected)
    assert final.attempt.status == ("SUCCEEDED" if already_collected else "REVIEW_REQUIRED")
