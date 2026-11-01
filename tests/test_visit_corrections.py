"""Reassignment, attributable outcome correction and never-dispatched release regressions."""

import uuid
from datetime import timedelta
from importlib import import_module
from types import SimpleNamespace

import pytest
from django.apps import apps
from django.core.exceptions import ValidationError
from django.db import DatabaseError, connection, transaction
from django.db.migrations.exceptions import IrreversibleError
from django.utils import timezone

from accounts.models import StaffUser
from delivery.models import DeliveryAssignment, HomeVisit, ReservationCustodyEvent, VisitEvidence
from tests.test_checkout_models import (
    begin_capture,
    booking_graph,
    collect,
    raw_insert,
    rejected,
    start_payment,
    stock,
)
from tests.test_checkout_models import setup as setup
from tests.test_lifecycle_financial_models import (
    close_commitment,
    issued_dispatch,
    observe,
    record_visit,
    trial_graph,
)

pytestmark = pytest.mark.django_db


def prepared_graph(setup, ready=False):
    graph = booking_graph(setup)
    start_payment(graph, stock(setup))
    begin_capture(graph)
    collect(graph)
    graph.actor = StaffUser.objects.create_user(username=str(uuid.uuid4()), is_staff=True)
    graph.assignment = DeliveryAssignment.objects.create(
        booking=graph.booking,
        agent=graph.actor,
        assigned_by=graph.actor,
        assigned_at=timezone.now(),
    )
    with transaction.atomic():
        graph.visit = HomeVisit.objects.create(booking=graph.booking, assignment=graph.assignment)
        record_visit(graph)
    graph.document, graph.tax, graph.series = issued_dispatch(graph, setup, graph.actor)
    if ready:
        graph.visit.dispatch_document = graph.document
        graph.visit.status = "READY"
        record_visit(graph)
    return graph


def force_consistency():
    with connection.cursor() as cursor:
        cursor.execute(
            "SET CONSTRAINTS visit_evidence_consistency,visit_manifest_consistency,"
            "custody_stock_consistency IMMEDIATE"
        )
        cursor.execute(
            "SET CONSTRAINTS visit_evidence_consistency,visit_manifest_consistency,"
            "custody_stock_consistency DEFERRED"
        )


@pytest.mark.parametrize("ready", [False, True])
def test_predispatch_reassignment_preserves_visit_and_can_dispatch(setup, ready):
    graph = prepared_graph(setup, ready)
    old_assignment = graph.assignment
    old_evidence = graph.visit.current_evidence
    old_assignment.ended_at = timezone.now()
    old_assignment.end_reason = "Fictional preparation responsibility reassignment"
    old_assignment.save()
    replacement = StaffUser.objects.create_user(username=str(uuid.uuid4()), is_staff=True)
    graph.assignment = DeliveryAssignment.objects.create(
        booking=graph.booking,
        agent=replacement,
        assigned_by=graph.actor,
        assigned_at=timezone.now(),
    )
    graph.visit.assignment = graph.assignment
    supersession = record_visit(graph, kind="REASSIGN")
    assert supersession.previous_id == old_evidence.pk
    assert old_evidence.assignment_id == old_assignment.pk
    assert graph.booking.assignments.count() == 2
    assert HomeVisit.objects.filter(booking=graph.booking).count() == 1
    if not ready:
        graph.visit.dispatch_document = graph.document
        graph.visit.status = "READY"
        record_visit(graph)
    observe(graph, "DISPATCH")
    graph.visit.status = "DISPATCHED"
    graph.visit.dispatched_at = timezone.now()
    record_visit(graph)
    force_consistency()
    graph.hold.unit.refresh_from_db()
    assert graph.hold.unit.status == "OUT_FOR_DELIVERY"
    assert graph.visit.assignment_id == graph.assignment.pk


@pytest.mark.parametrize("ready", [False, True])
def test_undispatched_closure_releases_stock_and_preserves_collected_money(setup, ready):
    graph = prepared_graph(setup, ready)
    collected = graph.attempt.transactions.get(transaction_kind="CAPTURE")
    committed_at = graph.hold.committed_at
    graph.visit.status = "CLOSED_BEFORE_DISPATCH"
    graph.visit.concluded_at = timezone.now()
    graph.visit.outcome = "ABORTED"
    with transaction.atomic():
        record_visit(graph, kind="PREDISPATCH_CLOSE")
        disposition = ReservationCustodyEvent.objects.create(
            reservation=graph.hold,
            visit=graph.visit,
            kind="NEVER_DISPATCHED",
            observed_at=timezone.now(),
            actor=graph.actor,
            reason="Fictional observed preparation ended with jewellery still in hub",
            evidence_reference="test-only-undispatched-observation",
            policy_snapshot={"test_only_physical_observation": True},
            idempotency_key=uuid.uuid4(),
        )
        close_commitment(graph, disposition)
        force_consistency()
    graph.booking.refresh_from_db()
    graph.context.refresh_from_db()
    graph.charge.refresh_from_db()
    graph.hold.refresh_from_db()
    graph.hold.unit.refresh_from_db()
    assert graph.booking.status == "CONFIRMED"
    assert graph.charge.status == "COLLECTED"
    assert graph.context.status == "PAID"
    assert collected.allocations.count() == 1
    assert graph.hold.status == "RELEASED"
    assert graph.hold.committed_at == committed_at
    assert graph.hold.unit.status == "AVAILABLE" and graph.hold.unit.custody == "HUB"
    another = booking_graph(setup)
    start_payment(another, graph.hold.unit)
    assert another.hold.unit_id == graph.hold.unit_id
    with pytest.raises(ValidationError):
        HomeVisit.objects.create(booking=graph.booking, assignment=graph.assignment)


def test_outcome_correction_is_attributed_and_preserves_original_observation(setup):
    graph = trial_graph(setup)
    graph.visit.status = "CONCLUDED"
    graph.visit.concluded_at = timezone.now()
    graph.visit.outcome = "NO_PURCHASE"
    original = record_visit(graph)
    correction = record_visit(graph, kind="OUTCOME_CORRECTION", outcome="SELECTED")
    force_consistency()
    graph.visit.refresh_from_db()
    assert graph.visit.outcome == "NO_PURCHASE"
    assert graph.visit.effective_outcome == "SELECTED"
    assert correction.previous_id == original.pk and original.outcome == "NO_PURCHASE"
    graph.hold.refresh_from_db()
    assert graph.hold.status == "COMMITTED"
    graph.hold.unit.refresh_from_db()
    assert graph.hold.unit.status == "OUT_FOR_DELIVERY"
    rejected("UPDATE delivery_visitevidence SET outcome='ABORTED' WHERE id=%s", [original.pk])
    rejected("UPDATE delivery_homevisit SET outcome='SELECTED' WHERE id=%s", [graph.visit.pk])


def test_ended_dispatched_assignment_and_undispatched_relabel_are_rejected(setup):
    graph = trial_graph(setup)
    graph.assignment.ended_at = timezone.now()
    graph.assignment.end_reason = "Cannot silently abandon dispatched custody"
    with pytest.raises(DatabaseError), transaction.atomic():
        graph.assignment.save()
    with pytest.raises(DatabaseError), transaction.atomic():
        raw_insert(
            ReservationCustodyEvent,
            reservation=graph.hold,
            visit=graph.visit,
            kind="NEVER_DISPATCHED",
            observed_at=timezone.now(),
            actor=graph.actor,
            reason="Invalid relabel",
            evidence_reference="test-only",
            policy_snapshot={"test": True},
            idempotency_key=uuid.uuid4(),
        )
    with pytest.raises(DatabaseError), transaction.atomic():
        raw_insert(
            VisitEvidence,
            visit=graph.visit,
            previous=graph.visit.current_evidence,
            assignment=graph.assignment,
            kind="PREDISPATCH_CLOSE",
            status="CLOSED_BEFORE_DISPATCH",
            outcome="ABORTED",
            observed_at=timezone.now(),
            actor=graph.actor,
            reason="Invalid relabel",
            evidence_reference="test-only",
            policy_snapshot={"test": True},
            idempotency_key=uuid.uuid4(),
        )


@pytest.mark.parametrize(
    "invalid", ["inactive_actor", "future_time", "wrong_assignment", "stale_chain"]
)
def test_visit_evidence_rejects_invalid_actor_time_identity_and_chain(setup, invalid):
    graph = prepared_graph(setup, ready=True)
    fields = dict(
        visit=graph.visit,
        previous=graph.visit.current_evidence,
        assignment=graph.assignment,
        kind="STATE",
        status="READY",
        outcome="",
        observed_at=timezone.now(),
        actor=graph.actor,
        reason="Fictional observation",
        evidence_reference="test-only",
        policy_snapshot={"test": True},
        idempotency_key=uuid.uuid4(),
    )
    if invalid == "inactive_actor":
        fields["actor"] = StaffUser.objects.create_user(username=str(uuid.uuid4()), is_active=False)
    elif invalid == "future_time":
        fields["observed_at"] = timezone.now() + timedelta(days=1)
    elif invalid == "wrong_assignment":
        other = booking_graph(setup)
        fields["assignment"] = DeliveryAssignment.objects.create(
            booking=other.booking,
            agent=graph.actor,
            assigned_by=graph.actor,
            assigned_at=timezone.now(),
        )
    else:
        fields["previous"] = graph.visit.current_evidence.previous
    with pytest.raises(DatabaseError), transaction.atomic():
        raw_insert(VisitEvidence, **fields)


def test_projection_and_evidence_must_commit_together_and_history_blocks_downgrade(setup):
    graph = prepared_graph(setup)
    with pytest.raises(DatabaseError), transaction.atomic():
        VisitEvidence.objects.create(
            visit=graph.visit,
            previous=graph.visit.current_evidence,
            assignment=graph.assignment,
            kind="STATE",
            status="PREPARING",
            outcome="",
            observed_at=timezone.now(),
            actor=graph.actor,
            reason="Evidence without projection",
            evidence_reference="test-only",
            policy_snapshot={"test": True},
        )
        force_consistency()
    with pytest.raises(IrreversibleError, match="Visit evidence exists"):
        import_module("trials.migrations.0008_visit_corrections").reverse_guards(
            apps,
            SimpleNamespace(connection=connection),
        )
