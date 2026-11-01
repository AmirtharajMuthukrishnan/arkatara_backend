"""Durable delivery intent and provider work, without sending messages or payments."""

from django.db import models
from django.db.models import F, Q
from django.utils import timezone

from common.persistence import LifecycleModel
from common.verification import phone_validator, validate_observed_at


class NotificationEvent(LifecycleModel):
    """One service notification intent, never marketing consent or proof of payment.

    OTP plaintext is not stored here. A short-lived protected payload reference
    can support asynchronous sending; it must be purged by Task 9's sender.
    Refs: BUSINESS_RULES.md R-21, R-24, R-32, R-36; DECISIONS.md D-27, LR-04.
    """

    booking = models.ForeignKey("trials.TrialBooking", on_delete=models.PROTECT)
    purpose = models.CharField(
        max_length=32,
        choices=[
            (s, s.replace("_", " ").title())
            for s in (
                "BOOKING_PHONE_OTP",
                "BOOKING_CONFIRMATION",
                "GUEST_RECOVERY_OTP",
                "ARRIVAL_OTP",
            )
        ],
    )
    booking_challenge = models.ForeignKey(
        "trials.BookingPhoneChallenge", null=True, blank=True, on_delete=models.PROTECT
    )
    recovery_challenge = models.ForeignKey(
        "trials.GuestRecoveryPhoneChallenge", null=True, blank=True, on_delete=models.PROTECT
    )
    arrival_challenge = models.ForeignKey(
        "delivery.ArrivalOTP", null=True, blank=True, on_delete=models.PROTECT
    )
    recipient_phone = models.CharField(max_length=16, validators=[phone_validator])
    template_reference = models.CharField(max_length=255)
    dedupe_key = models.UUIDField(unique=True)
    payload_reference = models.CharField(max_length=500, blank=True)
    expires_at = models.DateTimeField()
    status = models.CharField(
        max_length=16,
        default="PENDING",
        choices=[(s, s.title()) for s in ("PENDING", "SENT", "FAILED", "EXPIRED")],
    )
    sent_at = models.DateTimeField(null=True, blank=True, validators=[validate_observed_at])
    mutable_fields = ("status", "sent_at")
    write_once_fields = ("sent_at",)
    initial_status = "PENDING"
    transitions = {"PENDING": ("SENT", "FAILED", "EXPIRED")}

    class Meta(LifecycleModel.Meta):
        constraints = [
            models.CheckConstraint(
                condition=(
                    Q(
                        purpose="BOOKING_PHONE_OTP",
                        booking_challenge__isnull=False,
                        recovery_challenge__isnull=True,
                        arrival_challenge__isnull=True,
                    )
                    | Q(
                        purpose="BOOKING_CONFIRMATION",
                        booking_challenge__isnull=True,
                        recovery_challenge__isnull=True,
                        arrival_challenge__isnull=True,
                    )
                    | Q(
                        purpose="GUEST_RECOVERY_OTP",
                        booking_challenge__isnull=True,
                        recovery_challenge__isnull=False,
                        arrival_challenge__isnull=True,
                    )
                    | Q(
                        purpose="ARRIVAL_OTP",
                        booking_challenge__isnull=True,
                        recovery_challenge__isnull=True,
                        arrival_challenge__isnull=False,
                    )
                ),
                name="notification_purpose_binding",
            ),
            models.CheckConstraint(
                condition=Q(status="SENT", sent_at__isnull=False)
                | (~Q(status="SENT") & Q(sent_at__isnull=True)),
                name="notification_sent_time",
            ),
        ]


class NotificationAttempt(LifecycleModel):
    """Channel delivery progress for one intent; different channels share its OTP."""

    event = models.ForeignKey(NotificationEvent, on_delete=models.PROTECT, related_name="attempts")
    number = models.PositiveIntegerField()
    channel = models.CharField(max_length=16, choices=[("SMS", "SMS"), ("WHATSAPP", "WhatsApp")])
    provider = models.CharField(max_length=64)
    provider_account = models.CharField(max_length=128)
    environment = models.CharField(max_length=4, choices=[("TEST", "Test"), ("LIVE", "Live")])
    # NULL permits independent attempts before the provider supplies an identity.
    provider_message_id = models.CharField(max_length=255, null=True, blank=True)  # noqa: DJ001
    status = models.CharField(
        max_length=16,
        default="PENDING",
        choices=[(s, s.title()) for s in ("PENDING", "ACCEPTED", "DELIVERED", "FAILED", "UNKNOWN")],
    )
    attempted_at = models.DateTimeField(default=timezone.now, validators=[validate_observed_at])
    accepted_at = models.DateTimeField(null=True, blank=True, validators=[validate_observed_at])
    delivered_at = models.DateTimeField(null=True, blank=True, validators=[validate_observed_at])
    error_code = models.CharField(max_length=64, blank=True)
    mutable_fields = ("provider_message_id", "status", "accepted_at", "delivered_at", "error_code")
    write_once_fields = ("provider_message_id", "accepted_at", "delivered_at")
    initial_status = "PENDING"
    transitions = {
        "PENDING": ("ACCEPTED", "FAILED", "UNKNOWN"),
        "ACCEPTED": ("DELIVERED", "FAILED", "UNKNOWN"),
        "UNKNOWN": ("ACCEPTED", "DELIVERED", "FAILED"),
    }

    class Meta(LifecycleModel.Meta):
        constraints = [
            models.UniqueConstraint(
                fields=["event", "channel", "number"], name="notification_attempt_number"
            ),
            models.CheckConstraint(condition=Q(number__gt=0), name="notification_attempt_positive"),
            models.CheckConstraint(
                condition=Q(channel__in=["SMS", "WHATSAPP"]), name="notification_channel"
            ),
            models.CheckConstraint(
                condition=Q(environment__in=["TEST", "LIVE"]), name="notification_environment"
            ),
            models.UniqueConstraint(
                fields=["provider", "provider_account", "environment", "provider_message_id"],
                condition=Q(provider_message_id__isnull=False),
                name="notification_provider_identity",
            ),
            models.CheckConstraint(
                condition=~Q(status="DELIVERED") | Q(delivered_at__isnull=False),
                name="notification_delivery_time",
            ),
        ]


class OutboxCommand(LifecycleModel):
    """Persist intended provider effects alongside the business transaction.

    Leases fence workers; dedupe keys correlate retries but cannot promise remote
    exactly-once execution. Unknown outcomes need reconciliation.
    Refs: BUSINESS_RULES.md R-24; DECISIONS.md D-27.
    """

    operation = models.CharField(
        max_length=24,
        choices=[
            ("CREATE_PROVIDER_ORDER", "Create provider order"),
            ("CAPTURE_PAYMENT", "Capture payment"),
            ("SEND_NOTIFICATION", "Send notification"),
        ],
    )
    payment_attempt = models.ForeignKey(
        "payments.PaymentAttempt", null=True, blank=True, on_delete=models.PROTECT
    )
    notification_attempt = models.OneToOneField(
        NotificationAttempt, null=True, blank=True, on_delete=models.PROTECT
    )
    dedupe_key = models.UUIDField(unique=True)
    status = models.CharField(
        max_length=20,
        default="PENDING",
        choices=[
            (s, s.replace("_", " ").title())
            for s in ("PENDING", "PROCESSING", "SUCCEEDED", "FAILED", "REVIEW_REQUIRED")
        ],
    )
    attempts = models.PositiveIntegerField(default=0)
    available_at = models.DateTimeField(default=timezone.now)
    lease_token = models.UUIDField(null=True, blank=True)
    lease_until = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True, validators=[validate_observed_at])
    last_error_code = models.CharField(max_length=64, blank=True)
    mutable_fields = (
        "status",
        "attempts",
        "available_at",
        "lease_token",
        "lease_until",
        "completed_at",
        "last_error_code",
    )
    write_once_fields = ("completed_at",)
    initial_status = "PENDING"
    transitions = {
        "PENDING": ("PROCESSING", "FAILED"),
        "PROCESSING": ("PENDING", "SUCCEEDED", "FAILED", "REVIEW_REQUIRED"),
        "REVIEW_REQUIRED": ("PENDING", "SUCCEEDED", "FAILED"),
    }

    class Meta(LifecycleModel.Meta):
        constraints = [
            models.CheckConstraint(
                condition=Q(
                    operation="SEND_NOTIFICATION",
                    notification_attempt__isnull=False,
                    payment_attempt__isnull=True,
                )
                | Q(
                    operation__in=["CREATE_PROVIDER_ORDER", "CAPTURE_PAYMENT"],
                    notification_attempt__isnull=True,
                    payment_attempt__isnull=False,
                ),
                name="outbox_operation_target",
            ),
            models.UniqueConstraint(
                fields=["payment_attempt", "operation"],
                condition=Q(payment_attempt__isnull=False),
                name="outbox_payment_operation",
            ),
            models.CheckConstraint(
                condition=Q(
                    status="PROCESSING",
                    lease_token__isnull=False,
                    lease_until__gt=F("available_at"),
                )
                | (~Q(status="PROCESSING") & Q(lease_token__isnull=True, lease_until__isnull=True)),
                name="outbox_lease",
            ),
            models.CheckConstraint(
                condition=Q(status__in=["SUCCEEDED", "FAILED"], completed_at__isnull=False)
                | (~Q(status__in=["SUCCEEDED", "FAILED"]) & Q(completed_at__isnull=True)),
                name="outbox_completion",
            ),
        ]
        indexes = [models.Index(fields=["status", "available_at"], name="outbox_work_idx")]
