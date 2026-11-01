"""Combined visit and observed physical custody evidence, without operational endpoints."""

import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q

from common.evidence import AppendOnlyModel
from common.persistence import LifecycleModel
from common.verification import validate_observed_at


class HomeVisit(LifecycleModel):
    """One normal combined visit; arrival uses the existing single-purpose challenge.

    Scheduling/revisit policies remain BD-07. Evidence does not authorize dispatch.
    Refs: BUSINESS_RULES.md R-02, R-20, R-21, R-30; DECISIONS.md BD-07, BD-09.
    """

    booking = models.OneToOneField(
        "trials.TrialBooking", on_delete=models.PROTECT, related_name="visit"
    )
    assignment = models.ForeignKey("delivery.DeliveryAssignment", on_delete=models.PROTECT)
    status = models.CharField(
        max_length=24,
        default="PREPARING",
        choices=[
            (s, s.replace("_", " ").title())
            for s in (
                "PREPARING",
                "READY",
                "DISPATCHED",
                "IN_PERSON_TRIAL",
                "CONCLUDED",
                "CLOSED_BEFORE_DISPATCH",
            )
        ],
    )
    dispatch_document = models.ForeignKey(
        "billing.FinancialDocument", null=True, blank=True, on_delete=models.PROTECT
    )
    arrival_challenge = models.OneToOneField(
        "delivery.ArrivalOTP", null=True, blank=True, on_delete=models.PROTECT
    )
    dispatched_at = models.DateTimeField(null=True, blank=True, validators=[validate_observed_at])
    trial_started_at = models.DateTimeField(
        null=True, blank=True, validators=[validate_observed_at]
    )
    concluded_at = models.DateTimeField(null=True, blank=True, validators=[validate_observed_at])
    outcome = models.CharField(
        max_length=32,
        blank=True,
        choices=[
            (s, s.replace("_", " ").title())
            for s in ("SELECTED", "NO_PURCHASE", "FAILED_VISIT", "ABORTED")
        ],
    )
    current_evidence = models.ForeignKey(
        "VisitEvidence",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="current_for_visits",
    )
    mutable_fields = (
        "status",
        "dispatch_document_id",
        "arrival_challenge_id",
        "dispatched_at",
        "trial_started_at",
        "concluded_at",
        "outcome",
        "assignment_id",
        "current_evidence_id",
    )
    write_once_fields = mutable_fields[1:7]
    initial_status = "PREPARING"
    transitions = {
        "PREPARING": ("READY", "CLOSED_BEFORE_DISPATCH"),
        "READY": ("DISPATCHED", "CLOSED_BEFORE_DISPATCH"),
        "DISPATCHED": ("IN_PERSON_TRIAL", "CONCLUDED"),
        "IN_PERSON_TRIAL": ("CONCLUDED",),
    }

    class Meta(LifecycleModel.Meta):
        constraints = [
            models.CheckConstraint(
                condition=Q(
                    status__in=["CONCLUDED", "CLOSED_BEFORE_DISPATCH"],
                    concluded_at__isnull=False,
                )
                & ~Q(outcome="")
                | (
                    ~Q(status__in=["CONCLUDED", "CLOSED_BEFORE_DISPATCH"])
                    & Q(concluded_at__isnull=True, outcome="")
                ),
                name="visit_outcome_evidence",
            )
        ]

    @property
    def effective_outcome(self):
        """Corrections retain the original outcome and expose separately observed evidence."""
        return self.current_evidence.outcome if self.current_evidence_id else self.outcome

    def validate_existing_identity(self, previous):
        super().validate_existing_identity(previous)
        if self.assignment_id != previous.assignment_id and (
            previous.status not in ("PREPARING", "READY")
            or previous.dispatched_at is not None
            or previous.arrival_challenge_id is not None
            or previous.custody_events.exists()
        ):
            # Reassignment preserves responsibility history; custody transfer is a later policy.
            # Refs: BUSINESS_RULES.md R-02, R-30; DECISIONS.md BD-07, BD-09.
            raise ValidationError({"assignment": "Dispatched visit responsibility cannot change."})


class VisitEvidence(AppendOnlyModel):
    """Attributable visit progression, responsibility replacement and outcome corrections.

    An outcome correction never rewrites the initial observation or changes money/custody.
    The latest evidence and visit projection must commit together. No revisit is authorized.
    Refs: BUSINESS_RULES.md R-02, R-30; DECISIONS.md D-24, BD-07, BD-09.
    """

    visit = models.ForeignKey(HomeVisit, on_delete=models.PROTECT, related_name="evidence")
    previous = models.OneToOneField(
        "self", null=True, blank=True, on_delete=models.PROTECT, related_name="following"
    )
    assignment = models.ForeignKey("delivery.DeliveryAssignment", on_delete=models.PROTECT)
    kind = models.CharField(
        max_length=24,
        choices=[
            (s, s.replace("_", " ").title())
            for s in (
                "STATE",
                "REASSIGN",
                "PREDISPATCH_CLOSE",
                "OUTCOME_CORRECTION",
            )
        ],
    )
    status = models.CharField(max_length=24)
    outcome = models.CharField(max_length=32, blank=True)
    observed_at = models.DateTimeField(validators=[validate_observed_at])
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    reason = models.TextField()
    evidence_reference = models.CharField(max_length=500)
    policy_snapshot = models.JSONField()
    idempotency_key = models.UUIDField(default=uuid.uuid4, unique=True)

    class Meta(AppendOnlyModel.Meta):
        constraints = [
            models.UniqueConstraint(
                fields=["visit"],
                condition=Q(previous__isnull=True),
                name="visit_one_evidence_origin",
            ),
        ]

    def clean(self):
        super().clean()
        if not self.reason.strip() or not self.evidence_reference.strip():
            raise ValidationError("Visit evidence requires a reason and supporting reference.")
        if not isinstance(self.policy_snapshot, dict) or not self.policy_snapshot:
            raise ValidationError({"policy_snapshot": "Retain the applicable evidence scope."})
        if self.actor_id and not self.actor.is_active:
            raise ValidationError({"actor": "New evidence requires an active staff identity."})
        if self.assignment_id and self.visit_id:
            if self.assignment.booking_id != self.visit.booking_id:
                raise ValidationError({"assignment": "Responsibility must belong to this booking."})
        if self.previous_id and self.previous.visit_id != self.visit_id:
            raise ValidationError({"previous": "Evidence cannot switch visits."})


class ReservationCustodyEvent(AppendOnlyModel):
    """Append-only observed dispatch, receipt/QC, sale and exception facts.

    Releasing the commitment never changes physical stock to available by itself.
    Refs: BUSINESS_RULES.md R-14, R-15, R-16, R-17, R-30; DECISIONS.md BD-09, CA-02.
    """

    reservation = models.ForeignKey(
        "inventory.InventoryReservation", on_delete=models.PROTECT, related_name="custody_events"
    )
    visit = models.ForeignKey(HomeVisit, on_delete=models.PROTECT, related_name="custody_events")
    previous = models.OneToOneField(
        "self", null=True, blank=True, on_delete=models.PROTECT, related_name="following"
    )
    kind = models.CharField(
        max_length=24,
        choices=[
            (s, s.replace("_", " ").title())
            for s in (
                "DISPATCH",
                "RETURNING",
                "HUB_RECEIPT",
                "QC_PASS",
                "QC_FAIL",
                "SALE",
                "EXCEPTION",
                "NEVER_DISPATCHED",
            )
        ],
    )
    observed_at = models.DateTimeField(validators=[validate_observed_at])
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    reason = models.TextField()
    evidence_reference = models.CharField(max_length=500)
    policy_snapshot = models.JSONField()
    purchase_item = models.ForeignKey(
        "billing.PurchaseItem", null=True, blank=True, on_delete=models.PROTECT
    )
    idempotency_key = models.UUIDField(unique=True)

    class Meta(AppendOnlyModel.Meta):
        constraints = [
            models.UniqueConstraint(
                fields=["reservation"],
                condition=Q(previous__isnull=True),
                name="reservation_one_custody_origin",
            ),
            models.CheckConstraint(
                condition=Q(kind="SALE", purchase_item__isnull=False)
                | (~Q(kind="SALE") & Q(purchase_item__isnull=True)),
                name="custody_sale_link",
            ),
        ]
