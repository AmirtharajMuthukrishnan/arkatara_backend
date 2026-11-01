"""Assignment and arrival evidence, without dispatch, payment or return side effects."""

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import F, Q

from common.evidence import RetainedModel
from common.verification import VerificationChallenge, validate_observed_at

from .visits import HomeVisit, ReservationCustodyEvent, VisitEvidence  # noqa: F401


class DeliveryAssignment(RetainedModel):
    """Retain who was responsible rather than overwriting an agent foreign key.

    End the current assignment before recording its replacement. This grants no
    permission to dispatch a draft booking, split visits or bypass custody checks.
    Refs: BUSINESS_RULES.md R-02, R-18, R-30, R-38; DECISIONS.md D-24, BD-07.
    """

    booking = models.ForeignKey(
        "trials.TrialBooking", on_delete=models.PROTECT, related_name="assignments"
    )
    agent = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="delivery_assignments"
    )
    assigned_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="made_delivery_assignments"
    )
    assigned_at = models.DateTimeField(validators=[validate_observed_at])
    ended_at = models.DateTimeField(null=True, blank=True, validators=[validate_observed_at])
    end_reason = models.TextField(blank=True)

    immutable_fields = ("public_id", "booking_id", "agent_id", "assigned_by_id", "assigned_at")

    class Meta(RetainedModel.Meta):
        constraints = [
            models.UniqueConstraint(
                fields=["booking"],
                condition=Q(ended_at__isnull=True),
                name="booking_one_open_assignment",
            ),
            models.CheckConstraint(
                condition=Q(ended_at__isnull=True, end_reason="")
                | (
                    Q(ended_at__gte=F("assigned_at"))
                    & Q(ended_at__isnull=False)
                    & ~Q(end_reason="")
                ),
                name="assignment_end_evidence",
            ),
        ]
        indexes = [
            models.Index(fields=["agent", "ended_at", "assigned_at"], name="agent_assignments_idx")
        ]

    def clean(self):
        super().clean()
        if self.ended_at and not self.end_reason.strip():
            raise ValidationError({"end_reason": "Explain the end of this assignment."})
        if self._state.adding:
            for field in ("agent", "assigned_by"):
                if getattr(self, f"{field}_id") and not getattr(self, field).is_active:
                    raise ValidationError(
                        {field: "New assignments require active staff identities."}
                    )

    def validate_existing_identity(self, previous):
        super().validate_existing_identity(previous)
        if previous.ended_at is not None and (
            self.ended_at != previous.ended_at or self.end_reason != previous.end_reason
        ):
            raise ValidationError("Ended assignments cannot be reopened or rewritten.")


class ArrivalOTP(VerificationChallenge):
    """A different purpose and table from pre-payment booking phone verification.

    Both messaging channels deliver this one challenge; no purchase OTP exists.
    Refs: BUSINESS_RULES.md R-20, R-21, R-22, R-36.
    """

    assignment = models.ForeignKey(
        DeliveryAssignment, on_delete=models.PROTECT, related_name="arrival_challenges"
    )

    immutable_fields = (*VerificationChallenge.immutable_fields, "assignment_id")

    def clean(self):
        super().clean()
        if self.assignment_id and self.phone != self.assignment.booking.contact_phone:
            raise ValidationError({"phone": "Arrival phone must match the assigned booking."})
