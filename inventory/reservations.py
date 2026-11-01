"""Exclusive timed commitments; physical custody and QC remain separate facts."""

from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import F, Q
from django.utils import timezone

from common.persistence import LifecycleModel
from common.verification import validate_observed_at


class InventoryReservation(LifecycleModel):
    """Only explicit payment initiation acquires a hold, never draft/OTP creation.

    A partial unique constraint protects each unit even under concurrent SQL.
    Capture-pending holds cannot simply expire.
    Refs: BUSINESS_RULES.md R-08, R-14; DECISIONS.md D-27.
    """

    payment_context = models.ForeignKey(
        "payments.PaymentContext", on_delete=models.PROTECT, related_name="reservations"
    )
    unit = models.ForeignKey(
        "inventory.InventoryUnit", on_delete=models.PROTECT, related_name="reservations"
    )
    status = models.CharField(
        max_length=20,
        default="HELD",
        choices=[
            (s, s.replace("_", " ").title())
            for s in ("HELD", "CAPTURE_PENDING", "COMMITTED", "EXPIRED", "RELEASED")
        ],
    )
    held_at = models.DateTimeField(default=timezone.now, validators=[validate_observed_at])
    capture_started_at = models.DateTimeField(
        null=True, blank=True, validators=[validate_observed_at]
    )
    committed_at = models.DateTimeField(null=True, blank=True, validators=[validate_observed_at])
    released_at = models.DateTimeField(null=True, blank=True, validators=[validate_observed_at])
    release_reason = models.CharField(max_length=255, blank=True)
    resolution_reference = models.CharField(max_length=255, blank=True)
    resolution = models.ForeignKey(
        "payments.PaymentResolution", null=True, blank=True, on_delete=models.PROTECT
    )
    disposition = models.OneToOneField(
        "delivery.ReservationCustodyEvent",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="closed_reservation",
    )
    mutable_fields = (
        "status",
        "capture_started_at",
        "committed_at",
        "released_at",
        "release_reason",
        "resolution_reference",
        "resolution_id",
        "disposition_id",
    )
    write_once_fields = mutable_fields[1:]
    initial_status = "HELD"
    transitions = {
        "HELD": ("CAPTURE_PENDING", "EXPIRED", "RELEASED"),
        "CAPTURE_PENDING": ("COMMITTED", "RELEASED"),
        "COMMITTED": ("RELEASED",),
    }

    class Meta(LifecycleModel.Meta):
        constraints = [
            models.UniqueConstraint(
                fields=["unit"],
                condition=Q(status__in=["HELD", "CAPTURE_PENDING", "COMMITTED"]),
                name="one_live_reservation_per_unit",
            ),
            models.CheckConstraint(
                condition=Q(status__in=["EXPIRED", "RELEASED"], released_at__isnull=False)
                & ~Q(release_reason="")
                | (
                    Q(
                        status__in=["HELD", "CAPTURE_PENDING", "COMMITTED"],
                        released_at__isnull=True,
                        release_reason="",
                    )
                ),
                name="reservation_release_evidence",
            ),
            models.CheckConstraint(
                condition=Q(status__in=["COMMITTED", "RELEASED"], committed_at__isnull=False)
                | (~Q(status="COMMITTED") & Q(committed_at__isnull=True)),
                name="reservation_commit_time",
            ),
            models.CheckConstraint(
                condition=~Q(status__in=["CAPTURE_PENDING", "COMMITTED"])
                | Q(capture_started_at__isnull=False),
                name="reservation_capture_time",
            ),
            models.CheckConstraint(
                condition=Q(released_at__isnull=True) | Q(released_at__gte=F("held_at")),
                name="reservation_release_order",
            ),
        ]
        indexes = [
            models.Index(fields=["payment_context", "status"], name="reservation_context_idx")
        ]

    def clean(self):
        super().clean()
        if self.unit_id and self.payment_context_id:
            context = self.payment_context
            if self.unit.hub.market_id != context.booking_charge.booking.market_id:
                raise ValidationError("Reservation must use inventory in the booking city.")
            if self._state.adding and (
                self.unit.status != "AVAILABLE"
                or self.unit.custody != "HUB"
                or context.status != "OPEN"
                or self.held_at >= context.expires_at
            ):
                raise ValidationError("New holds require available hub stock and an open context.")
