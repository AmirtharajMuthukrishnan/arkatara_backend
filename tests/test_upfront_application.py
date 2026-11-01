"""Traceable upfront use without selecting a commercial credit/refund policy."""

import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from decimal import Decimal
from threading import Barrier

import pytest
from django.core.exceptions import ValidationError
from django.db import DatabaseError, close_old_connections, connection, transaction
from django.utils import timezone

from billing.models import (
    FinancialDocument,
    FinancialDocumentLine,
    Purchase,
    PurchaseBalanceEvidence,
    PurchaseItem,
    PurchaseObligation,
    UpfrontApplication,
)
from common.fields import SignedMinorUnitField
from payments.models import PaymentContext
from tests.test_checkout_models import booking_graph, raw_insert, rejected
from tests.test_checkout_models import setup as setup
from tests.test_lifecycle_financial_models import close_commitment, observe, trial_graph

pytestmark = pytest.mark.django_db


def draft_purchase(graph, setup, *, revision=1, payable=450, booking=None):
    purchase = Purchase.objects.create(
        booking=booking or graph.booking,
        revision=revision,
        seller=setup.entity,
        seller_snapshot={"fictional": True},
        customer_snapshot={"fictional": True},
        tax_evidence=graph.tax,
        currency="INR",
        jewellery_total_minor=500,
        payable_minor=payable,
        balance_snapshot={"fictional": True},
    )
    if booking is not None:
        return purchase, None
    item = PurchaseItem.objects.create(
        purchase=purchase,
        reservation=graph.hold,
        line_number=1,
        amount_minor=500,
        currency="INR",
        product_snapshot={"fictional": True},
        price_snapshot={"fictional": True},
    )
    return purchase, item


def balance_values(graph, purchase, *, adjustment=0):
    return dict(
        purchase=purchase,
        amount_due_before_application=500 + adjustment,
        adjustment_minor=adjustment,
        currency="INR",
        actor=graph.actor,
        observed_at=timezone.now(),
        reason="Fictional reviewed calculation",
        evidence_reference="test-only-calculation",
        calculation_snapshot={"fictional_components": True},
        policy_snapshot={"fictional_review": True},
    )


def application_values(graph, balance, *, amount=50):
    return dict(
        balance_evidence=balance,
        source_allocation=graph.charge.allocations.get(),
        amount_minor=amount,
        currency="INR",
        actor=graph.actor,
        observed_at=timezone.now(),
        reason="Fictional approved application",
        evidence_reference="test-only-application",
        policy_snapshot={"fictional_review": True},
        idempotency_key=uuid.uuid4(),
    )


def seal(purchase):
    purchase.status = "SEALED"
    purchase.sealed_at = timezone.now()
    purchase.save()


def test_applied_funds_change_balance_without_repricing_jewellery(setup):
    graph = trial_graph(setup)
    purchase, _ = draft_purchase(graph, setup)
    basis = PurchaseBalanceEvidence.objects.create(**balance_values(graph, purchase))
    application = UpfrontApplication.objects.create(**application_values(graph, basis))
    seal(purchase)
    assert purchase.jewellery_total_minor == 500
    assert purchase.payable_minor == 450
    assert application.source_allocation.booking_charge == graph.charge
    assert basis.upfront_applications.get() == application


def test_balance_equation_and_missing_evidence_fail_at_seal(setup):
    graph = trial_graph(setup)
    purchase, _ = draft_purchase(graph, setup)
    rejected(
        "UPDATE billing_purchase SET status='SEALED',sealed_at=now() WHERE id=%s",
        [purchase.pk],
    )
    basis = PurchaseBalanceEvidence.objects.create(**balance_values(graph, purchase))
    UpfrontApplication.objects.create(**application_values(graph, basis, amount=49))
    rejected(
        "UPDATE billing_purchase SET status='SEALED',sealed_at=now() WHERE id=%s",
        [purchase.pk],
    )
    assert purchase.balance_snapshot == {"fictional": True}


def test_explicit_adjustment_may_raise_due_basis_without_inventing_credit(setup):
    graph = trial_graph(setup)
    purchase, _ = draft_purchase(graph, setup, payable=525)
    basis = PurchaseBalanceEvidence.objects.create(**balance_values(graph, purchase, adjustment=25))
    seal(purchase)
    assert purchase.jewellery_total_minor == 500
    assert basis.adjustment_minor == 25
    assert basis.amount_due_before_application == purchase.payable_minor == 525
    assert not basis.upfront_applications.exists()


@pytest.mark.parametrize("invalid", ["basis", "currency", "attribution", "policy"])
def test_balance_basis_requires_consistent_attributable_evidence(setup, invalid):
    graph = trial_graph(setup)
    purchase, _ = draft_purchase(graph, setup)
    values = balance_values(graph, purchase)
    values.update(
        {
            "basis": {"amount_due_before_application": 501},
            "currency": {"currency": "USD"},
            "attribution": {"reason": " "},
            "policy": {"policy_snapshot": {}},
        }[invalid]
    )
    with pytest.raises(ValidationError):
        PurchaseBalanceEvidence.objects.create(**values)
    with pytest.raises(DatabaseError), transaction.atomic():
        raw_insert(PurchaseBalanceEvidence, **values)


def test_source_budget_remains_consumed_after_voided_purchase(setup):
    graph = trial_graph(setup)
    first, _ = draft_purchase(graph, setup, payable=400)
    basis = PurchaseBalanceEvidence.objects.create(**balance_values(graph, first))
    UpfrontApplication.objects.create(**application_values(graph, basis, amount=100))
    first.status = "VOID"
    first.save()
    second, _ = draft_purchase(graph, setup, revision=2)
    second_basis = PurchaseBalanceEvidence.objects.create(**balance_values(graph, second))
    values = application_values(graph, second_basis, amount=1)
    with pytest.raises(ValidationError):
        UpfrontApplication.objects.create(**values)
    with pytest.raises(DatabaseError), transaction.atomic():
        raw_insert(UpfrontApplication, **values)
    assert graph.charge.allocations.get().purchase_applications.count() == 1


def test_application_cannot_cross_currency_or_closed_calculation(setup):
    graph = trial_graph(setup)
    purchase, _ = draft_purchase(graph, setup)
    basis = PurchaseBalanceEvidence.objects.create(**balance_values(graph, purchase))
    values = application_values(graph, basis)
    values["currency"] = "USD"
    with pytest.raises(ValidationError):
        UpfrontApplication.objects.create(**values)
    with pytest.raises(DatabaseError), transaction.atomic():
        raw_insert(UpfrontApplication, **values)
    application = UpfrontApplication.objects.create(**application_values(graph, basis))
    seal(purchase)
    with pytest.raises(DatabaseError), transaction.atomic():
        raw_insert(UpfrontApplication, **application_values(graph, basis, amount=1))
    rejected("UPDATE billing_upfrontapplication SET amount_minor=1 WHERE id=%s", [application.pk])
    rejected("DELETE FROM billing_upfrontapplication WHERE id=%s", [application.pk])
    rejected(
        "UPDATE billing_purchasebalanceevidence SET adjustment_minor=1 WHERE id=%s", [basis.pk]
    )
    rejected("DELETE FROM billing_purchasebalanceevidence WHERE id=%s", [basis.pk])


def test_application_cannot_redirect_other_bookings_collection(setup):
    graph = trial_graph(setup)
    other = booking_graph(setup)
    purchase, _ = draft_purchase(graph, setup, booking=other.booking)
    basis = PurchaseBalanceEvidence.objects.create(**balance_values(graph, purchase))
    values = application_values(graph, basis)
    with pytest.raises(ValidationError):
        UpfrontApplication.objects.create(**values)
    with pytest.raises(DatabaseError), transaction.atomic():
        raw_insert(UpfrontApplication, **values)


def test_application_cannot_exceed_recorded_balance_basis(setup):
    graph = trial_graph(setup)
    purchase, _ = draft_purchase(graph, setup, payable=0)
    basis = PurchaseBalanceEvidence.objects.create(
        **balance_values(graph, purchase, adjustment=-405)
    )
    values = application_values(graph, basis, amount=100)
    with pytest.raises(ValidationError):
        UpfrontApplication.objects.create(**values)
    with pytest.raises(DatabaseError), transaction.atomic():
        raw_insert(UpfrontApplication, **values)


def test_application_observation_cannot_predate_source_collection(setup):
    graph = trial_graph(setup)
    purchase, _ = draft_purchase(graph, setup)
    source = graph.charge.allocations.get()
    values = balance_values(graph, purchase)
    values["observed_at"] = source.allocated_at - timedelta(seconds=1)
    basis = PurchaseBalanceEvidence.objects.create(**values)
    values = application_values(graph, basis)
    values["observed_at"] = source.allocated_at - timedelta(microseconds=1)
    with pytest.raises(ValidationError):
        UpfrontApplication.objects.create(**values)
    with pytest.raises(DatabaseError), transaction.atomic():
        raw_insert(UpfrontApplication, **values)


def test_application_observation_cannot_predate_calculation_basis(setup):
    graph = trial_graph(setup)
    purchase, _ = draft_purchase(graph, setup)
    basis = PurchaseBalanceEvidence.objects.create(**balance_values(graph, purchase))
    values = application_values(graph, basis)
    values["observed_at"] = basis.observed_at - timedelta(microseconds=1)
    with pytest.raises(ValidationError):
        UpfrontApplication.objects.create(**values)
    with pytest.raises(DatabaseError), transaction.atomic():
        raw_insert(UpfrontApplication, **values)


def test_fully_source_funded_purchase_needs_no_new_provider_collection(setup):
    graph = trial_graph(setup)
    purchase, item = draft_purchase(graph, setup, payable=0)
    basis = PurchaseBalanceEvidence.objects.create(
        **balance_values(graph, purchase, adjustment=-400)
    )
    UpfrontApplication.objects.create(**application_values(graph, basis, amount=100))
    seal(purchase)
    obligation = PurchaseObligation.objects.create(
        purchase=purchase,
        amount_minor=0,
        currency="INR",
        quote_expires_at=timezone.now() + timedelta(minutes=5),
    )
    obligation.status = "DUE"
    obligation.accepted_at = timezone.now()
    obligation.save()
    context_values = dict(
        purchase_obligation=obligation,
        version=1,
        expires_at=timezone.now() + timedelta(minutes=3),
        policy_snapshot={"fictional": True},
    )
    with pytest.raises(ValidationError):
        PaymentContext.objects.create(**context_values)
    with pytest.raises(DatabaseError), transaction.atomic():
        raw_insert(PaymentContext, **context_values)
    with transaction.atomic():
        obligation.status = "COLLECTED"
        obligation.save()
        purchase.status = "CONFIRMED"
        purchase.confirmed_at = timezone.now()
        purchase.save()
        with connection.cursor() as cursor:
            cursor.execute("SET CONSTRAINTS source_funded_confirmation_consistency IMMEDIATE")
            cursor.execute("SET CONSTRAINTS source_funded_confirmation_consistency DEFERRED")
    observe(graph, "SALE", purchase_item=item)
    assert purchase.jewellery_total_minor == 500
    assert purchase.payable_minor == 0
    assert not obligation.contexts.exists()
    document = FinancialDocument.objects.create(
        booking=graph.booking,
        purchase_obligation=obligation,
        kind="TAX_INVOICE",
        series=graph.series,
        issuer_snapshot={"fictional": True},
        recipient_snapshot={"fictional": True},
        tax_evidence=graph.tax,
        amount_minor=500,
        currency="INR",
        review_reference="test-only-invoice-review",
    )
    FinancialDocumentLine.objects.create(
        document=document,
        line_number=1,
        amount_minor=500,
        snapshot={"fictional": True},
        purchase_item=item,
    )
    document.status = "ISSUED"
    document.number = 2
    document.issued_at = timezone.now()
    document.save()
    assert document.amount_minor == 500


@pytest.mark.django_db(transaction=True)
def test_interrupted_zero_collection_rolls_back_and_returned_stock_can_close(setup):
    graph = trial_graph(setup)
    purchase, _ = draft_purchase(graph, setup, payable=0)
    basis = PurchaseBalanceEvidence.objects.create(
        **balance_values(graph, purchase, adjustment=-400)
    )
    application = UpfrontApplication.objects.create(**application_values(graph, basis, amount=100))
    seal(purchase)
    obligation = PurchaseObligation.objects.create(
        purchase=purchase,
        amount_minor=0,
        currency="INR",
        quote_expires_at=timezone.now() + timedelta(minutes=5),
    )
    obligation.status = "DUE"
    obligation.accepted_at = timezone.now()
    obligation.save()
    with pytest.raises(DatabaseError) as error, transaction.atomic():
        obligation.status = "COLLECTED"
        obligation.save()
    assert error.value.__cause__.sqlstate == "23514"
    obligation.refresh_from_db()
    assert obligation.status == "DUE"
    obligation.status = "VOID"
    obligation.save()
    purchase.status = "VOID"
    purchase.save()
    observe(graph, "RETURNING")
    observe(graph, "HUB_RECEIPT")
    passed = observe(graph, "QC_PASS")
    close_commitment(graph, passed)
    graph.hold.refresh_from_db()
    assert graph.hold.status == "RELEASED"
    assert application.source_allocation.purchase_applications.get() == application
    graph.charge.refresh_from_db()
    assert graph.charge.status == "COLLECTED"


@pytest.mark.django_db(transaction=True)
def test_competing_purchases_cannot_reuse_source_funds(setup):
    graph = trial_graph(setup)
    bases = []
    for revision in (1, 2):
        purchase, _ = draft_purchase(graph, setup, revision=revision, payable=420)
        bases.append(PurchaseBalanceEvidence.objects.create(**balance_values(graph, purchase)))
    barrier = Barrier(2)

    def apply_to(basis):
        close_old_connections()
        try:
            with transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute("SET LOCAL lock_timeout='4s'")
                barrier.wait(timeout=10)
                UpfrontApplication.objects.create(**application_values(graph, basis, amount=80))
            return "applied"
        except ValidationError:
            return "rejected"
        except DatabaseError as error:
            assert error.__cause__.sqlstate == "23514"
            return "rejected"
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(apply_to, basis) for basis in bases]
        outcomes = [future.result(timeout=20) for future in futures]
    assert sorted(outcomes) == ["applied", "rejected"]
    assert graph.charge.allocations.get().purchase_applications.get().amount_minor == 80


@pytest.mark.parametrize("value", [1.5, Decimal("1.5"), "1", True])
def test_signed_minor_units_reject_non_integer_values_without_coercion(value):
    field = SignedMinorUnitField()
    with pytest.raises(ValidationError):
        field.to_python(value)
    with pytest.raises(ValidationError):
        field.get_prep_value(value)
    assert field.get_prep_value(-400) == -400
