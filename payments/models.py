"""Upfront collection evidence. No gateway calls or accounting classification here."""

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import F, Q
from django.utils import timezone

from common.evidence import AppendOnlyModel
from common.fields import MinorUnitField
from common.persistence import LifecycleModel, currency_validator, money_constraints
from common.verification import digest_validator, validate_observed_at
from compliance.configuration import validate_policy_binding


class BookingCharge(LifecycleModel):
    """Versioned upfront obligation, deliberately neutral about deposit/fee treatment.

    Legal party, amount, currency and policy facts survive configuration changes.
    Refs: BUSINESS_RULES.md R-25, R-26, R-28; DECISIONS.md D-27, BD-01, CA-01.
    """

    booking = models.ForeignKey(
        "trials.TrialBooking", on_delete=models.PROTECT, related_name="charges"
    )
    revision = models.PositiveIntegerField()
    payee_legal_entity = models.ForeignKey("compliance.LegalEntity", on_delete=models.PROTECT)
    payee_snapshot = models.JSONField()
    policy_revision = models.ForeignKey(
        "compliance.BusinessConfiguration", on_delete=models.PROTECT
    )
    # Legacy bindings remain readable; new commitments use public UUID identities.
    # Refs: BUSINESS_RULES.md R-07, R-28.
    policy_scope_version = models.PositiveSmallIntegerField(default=2)
    policy_snapshot = models.JSONField()
    amount_minor = MinorUnitField()
    currency = models.CharField(max_length=3, validators=[currency_validator])
    quote_expires_at = models.DateTimeField()
    accepted_at = models.DateTimeField(null=True, blank=True, validators=[validate_observed_at])
    status = models.CharField(
        max_length=16,
        default="QUOTED",
        choices=[
            ("QUOTED", "Quoted"),
            ("DUE", "Due"),
            ("COLLECTED", "Collected"),
            ("VOID", "Voided"),
        ],
    )
    mutable_fields = ("status", "accepted_at")
    write_once_fields = ("accepted_at",)
    initial_status = "QUOTED"
    transitions = {"QUOTED": ("DUE", "VOID"), "DUE": ("COLLECTED", "VOID")}

    class Meta(LifecycleModel.Meta):
        constraints = [
            *money_constraints("booking_charge"),
            models.UniqueConstraint(fields=["booking", "revision"], name="booking_charge_revision"),
            models.CheckConstraint(condition=Q(revision__gt=0), name="charge_revision_positive"),
            models.CheckConstraint(
                condition=Q(policy_scope_version__in=[1, 2]), name="charge_policy_scope_version"
            ),
            models.CheckConstraint(
                condition=Q(accepted_at__isnull=True, status__in=["QUOTED", "VOID"])
                | Q(
                    accepted_at__isnull=False,
                    accepted_at__lt=F("quote_expires_at"),
                    status__in=["DUE", "COLLECTED", "VOID"],
                ),
                name="charge_acceptance_time",
            ),
            models.UniqueConstraint(
                fields=["booking"],
                condition=Q(status__in=["DUE", "COLLECTED"]),
                name="booking_one_live_charge",
            ),
        ]

    def clean(self):
        super().clean()
        if self._state.adding and self.policy_scope_version != 2:
            raise ValidationError("New policy bindings require public UUID scope identifiers.")
        if self.policy_revision_id and self.booking_id:
            validate_policy_binding(
                self.policy_revision,
                "payments",
                "booking-charge-policy-reference",
                market=self.booking.market,
                scope_version=self.policy_scope_version,
            )


class PaymentContext(LifecycleModel):
    """One timed attempt to collect an obligation and hold a complete trial selection.

    Capture uncertainty keeps inventory protected until provider reconciliation.
    Refs: BUSINESS_RULES.md R-14, R-24; DECISIONS.md D-27, BD-02.
    """

    booking_charge = models.ForeignKey(
        BookingCharge, null=True, blank=True, on_delete=models.PROTECT, related_name="contexts"
    )
    purchase_obligation = models.ForeignKey(
        "billing.PurchaseObligation",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="contexts",
    )
    version = models.PositiveIntegerField()
    opened_at = models.DateTimeField(default=timezone.now, validators=[validate_observed_at])
    expires_at = models.DateTimeField()
    policy_snapshot = models.JSONField()
    status = models.CharField(
        max_length=20,
        default="OPEN",
        choices=[
            (s, s.replace("_", " ").title())
            for s in ("OPEN", "CAPTURING", "PAID", "EXPIRED", "CANCELLED", "REVIEW_REQUIRED")
        ],
    )
    capture_started_at = models.DateTimeField(
        null=True, blank=True, validators=[validate_observed_at]
    )
    paid_at = models.DateTimeField(null=True, blank=True, validators=[validate_observed_at])
    closed_at = models.DateTimeField(null=True, blank=True, validators=[validate_observed_at])
    resolution_reference = models.CharField(max_length=255, blank=True)
    resolution = models.ForeignKey(
        "PaymentResolution",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="closed_contexts",
    )
    mutable_fields = (
        "status",
        "capture_started_at",
        "paid_at",
        "closed_at",
        "resolution_reference",
        "resolution_id",
    )
    write_once_fields = mutable_fields[1:]
    initial_status = "OPEN"
    transitions = {
        "OPEN": ("CAPTURING", "EXPIRED", "CANCELLED", "REVIEW_REQUIRED"),
        "CAPTURING": ("PAID", "REVIEW_REQUIRED"),
        "REVIEW_REQUIRED": ("PAID", "CANCELLED"),
    }

    class Meta(LifecycleModel.Meta):
        constraints = [
            models.CheckConstraint(
                condition=Q(booking_charge__isnull=False, purchase_obligation__isnull=True)
                | Q(booking_charge__isnull=True, purchase_obligation__isnull=False),
                name="context_one_obligation",
            ),
            models.UniqueConstraint(
                fields=["purchase_obligation", "version"], name="purchase_context_version"
            ),
            models.UniqueConstraint(
                fields=["purchase_obligation"],
                condition=Q(status__in=["OPEN", "CAPTURING", "PAID", "REVIEW_REQUIRED"]),
                name="purchase_one_live_context",
            ),
            models.UniqueConstraint(
                fields=["booking_charge", "version"], name="payment_context_version"
            ),
            models.UniqueConstraint(
                fields=["booking_charge"],
                condition=Q(status__in=["OPEN", "CAPTURING", "PAID", "REVIEW_REQUIRED"]),
                name="charge_one_live_context",
            ),
            models.CheckConstraint(condition=Q(version__gt=0), name="context_version_positive"),
            models.CheckConstraint(
                condition=Q(expires_at__gt=F("opened_at")), name="context_expiry"
            ),
            models.CheckConstraint(
                condition=Q(status="PAID", paid_at__isnull=False)
                | (~Q(status="PAID") & Q(paid_at__isnull=True)),
                name="context_paid_time",
            ),
            models.CheckConstraint(
                condition=~Q(status__in=["CAPTURING", "PAID"])
                | Q(capture_started_at__isnull=False),
                name="context_capture_time",
            ),
            models.CheckConstraint(
                condition=Q(status__in=["EXPIRED", "CANCELLED"], closed_at__isnull=False)
                | (~Q(status__in=["EXPIRED", "CANCELLED"]) & Q(closed_at__isnull=True)),
                name="context_closed_time",
            ),
        ]
        indexes = [models.Index(fields=["status", "expires_at"], name="context_expiry_scan_idx")]

    @property
    def obligation(self):
        return self.booking_charge if self.booking_charge_id else self.purchase_obligation

    def clean(self):
        super().clean()
        if self.purchase_obligation_id and self.purchase_obligation.amount_minor == 0:
            # Already-applied funds settle this balance without another provider request.
            # Refs: BUSINESS_RULES.md R-25, R-26; DECISIONS.md BD-01, CA-01.
            raise ValidationError("A zero balance does not require a provider collection context.")


class PaymentAttempt(LifecycleModel):
    """Provider operation identity, separate from captured receipt facts.

    One provider order may generate several webhook events; neither callback
    arrival nor authorization alone means paid.
    Refs: BUSINESS_RULES.md R-23, R-24; DECISIONS.md D-27.
    """

    context = models.ForeignKey(PaymentContext, on_delete=models.PROTECT, related_name="attempts")
    request_id = models.UUIDField(unique=True)
    request_fingerprint = models.CharField(max_length=64, validators=[digest_validator])
    provider = models.CharField(max_length=32)
    provider_account = models.CharField(max_length=128)
    environment = models.CharField(max_length=4, choices=[("TEST", "Test"), ("LIVE", "Live")])
    # NULL permits multiple not-yet-created orders under composite uniqueness.
    provider_order_id = models.CharField(max_length=255, null=True, blank=True)  # noqa: DJ001
    amount_minor = MinorUnitField()
    currency = models.CharField(max_length=3, validators=[currency_validator])
    status = models.CharField(
        max_length=20,
        default="CREATING",
        choices=[
            (s, s.replace("_", " ").title())
            for s in (
                "CREATING",
                "PENDING",
                "AUTHORIZED",
                "CAPTURE_PENDING",
                "SUCCEEDED",
                "FAILED",
                "EXPIRED",
                "REVIEW_REQUIRED",
            )
        ],
    )
    capture_requested_at = models.DateTimeField(
        null=True, blank=True, validators=[validate_observed_at]
    )
    last_verified_at = models.DateTimeField(
        null=True, blank=True, validators=[validate_observed_at]
    )
    succeeded_at = models.DateTimeField(null=True, blank=True, validators=[validate_observed_at])
    exception_code = models.CharField(max_length=64, blank=True)
    mutable_fields = (
        "provider_order_id",
        "status",
        "capture_requested_at",
        "last_verified_at",
        "succeeded_at",
        "exception_code",
    )
    write_once_fields = ("provider_order_id", "capture_requested_at", "succeeded_at")
    initial_status = "CREATING"
    transitions = {
        "CREATING": ("PENDING", "FAILED", "EXPIRED", "REVIEW_REQUIRED"),
        "PENDING": ("AUTHORIZED", "FAILED", "EXPIRED", "REVIEW_REQUIRED"),
        "AUTHORIZED": ("CAPTURE_PENDING", "FAILED", "EXPIRED", "REVIEW_REQUIRED"),
        "CAPTURE_PENDING": ("SUCCEEDED", "REVIEW_REQUIRED"),
        "REVIEW_REQUIRED": ("PENDING", "AUTHORIZED", "CAPTURE_PENDING", "SUCCEEDED", "FAILED"),
    }

    class Meta(LifecycleModel.Meta):
        constraints = [
            *money_constraints("payment_attempt"),
            models.UniqueConstraint(
                fields=["provider", "provider_account", "environment", "provider_order_id"],
                condition=Q(provider_order_id__isnull=False),
                name="provider_order_identity",
            ),
            models.UniqueConstraint(
                fields=["context"],
                condition=~Q(status__in=["FAILED", "EXPIRED"]),
                name="context_one_live_attempt",
            ),
            models.CheckConstraint(
                condition=Q(environment__in=["TEST", "LIVE"]), name="attempt_environment"
            ),
            models.CheckConstraint(
                condition=Q(status="SUCCEEDED", succeeded_at__isnull=False)
                | (~Q(status="SUCCEEDED") & Q(succeeded_at__isnull=True)),
                name="attempt_success_time",
            ),
            models.CheckConstraint(
                condition=~Q(status__in=["CAPTURE_PENDING", "SUCCEEDED"])
                | Q(capture_requested_at__isnull=False),
                name="attempt_capture_time",
            ),
        ]

    def clean(self):
        super().clean()
        if self.context_id and (self.amount_minor, self.currency) != (
            self.context.obligation.amount_minor,
            self.context.obligation.currency,
        ):
            raise ValidationError("Attempt amount and currency must match the obligation.")


class GatewayEvent(LifecycleModel):
    """Authenticated webhook inbox metadata; no secrets or unrestricted raw payloads.

    Verification occurs before insert in Task 8. An inbox row does not prove
    collection or authorize a booking. Refs: BUSINESS_RULES.md R-24, R-26.
    """

    provider = models.CharField(max_length=32)
    provider_account = models.CharField(max_length=128)
    environment = models.CharField(max_length=4, choices=[("TEST", "Test"), ("LIVE", "Live")])
    provider_event_id = models.CharField(max_length=255)
    event_type = models.CharField(max_length=128)
    payload_digest = models.CharField(max_length=64, validators=[digest_validator])
    evidence_reference = models.CharField(max_length=500, blank=True)
    received_at = models.DateTimeField(default=timezone.now, validators=[validate_observed_at])
    verified_at = models.DateTimeField(validators=[validate_observed_at])
    processed_at = models.DateTimeField(null=True, blank=True, validators=[validate_observed_at])
    status = models.CharField(
        max_length=20,
        default="RECEIVED",
        choices=[(s, s.title()) for s in ("RECEIVED", "PROCESSED", "REVIEW_REQUIRED")],
    )
    mutable_fields = ("status", "processed_at")
    write_once_fields = ("processed_at",)
    initial_status = "RECEIVED"
    transitions = {"RECEIVED": ("PROCESSED", "REVIEW_REQUIRED"), "REVIEW_REQUIRED": ("PROCESSED",)}

    class Meta(LifecycleModel.Meta):
        constraints = [
            models.UniqueConstraint(
                fields=["provider", "provider_account", "environment", "provider_event_id"],
                name="gateway_event_identity",
            ),
            models.CheckConstraint(
                condition=Q(environment__in=["TEST", "LIVE"]), name="event_environment"
            ),
            models.CheckConstraint(
                condition=Q(status="PROCESSED", processed_at__isnull=False)
                | (~Q(status="PROCESSED") & Q(processed_at__isnull=True)),
                name="event_processed_time",
            ),
        ]


class GatewayTransaction(AppendOnlyModel):
    """Verified provider fact, including late/extra receipts that cannot pay this booking.

    Authorization and capture are different facts for the same provider payment.
    Preserve mismatches for reconciliation; allocation is the stricter boundary.
    Refs: BUSINESS_RULES.md R-24, R-26, R-28; DECISIONS.md D-27.
    """

    attempt = models.ForeignKey(
        PaymentAttempt,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="transactions",
    )
    provider = models.CharField(max_length=32)
    provider_account = models.CharField(max_length=128)
    environment = models.CharField(max_length=4, choices=[("TEST", "Test"), ("LIVE", "Live")])
    provider_payment_id = models.CharField(max_length=255)
    transaction_kind = models.CharField(
        max_length=16, choices=[("AUTHORISATION", "Authorisation"), ("CAPTURE", "Capture")]
    )
    amount_minor = MinorUnitField()
    currency = models.CharField(max_length=3, validators=[currency_validator])
    occurred_at = models.DateTimeField(validators=[validate_observed_at])
    verified_at = models.DateTimeField(validators=[validate_observed_at])
    source_event = models.ForeignKey(GatewayEvent, null=True, blank=True, on_delete=models.PROTECT)
    verification_reference = models.CharField(max_length=500)

    class Meta(AppendOnlyModel.Meta):
        constraints = [
            *money_constraints("gateway_transaction"),
            models.UniqueConstraint(
                fields=[
                    "provider",
                    "provider_account",
                    "environment",
                    "provider_payment_id",
                    "transaction_kind",
                ],
                name="gateway_transaction_identity",
            ),
            models.CheckConstraint(
                condition=Q(transaction_kind__in=["AUTHORISATION", "CAPTURE"]),
                name="gateway_transaction_kind",
            ),
            models.CheckConstraint(
                condition=Q(environment__in=["TEST", "LIVE"]), name="transaction_environment"
            ),
        ]


class PaymentAllocation(AppendOnlyModel):
    """Apply a captured receipt to the exact obligation without double application.

    Database row locks serialize sums at receipt and charge. Allocating funds is
    not revenue recognition or applying a deposit to a sale.
    Refs: BUSINESS_RULES.md R-24, R-25, R-26, R-28.
    """

    gateway_transaction = models.ForeignKey(
        GatewayTransaction, on_delete=models.PROTECT, related_name="allocations"
    )
    booking_charge = models.ForeignKey(
        BookingCharge, null=True, blank=True, on_delete=models.PROTECT, related_name="allocations"
    )
    purchase_obligation = models.ForeignKey(
        "billing.PurchaseObligation",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="allocations",
    )
    amount_minor = MinorUnitField()
    currency = models.CharField(max_length=3, validators=[currency_validator])
    allocated_at = models.DateTimeField(default=timezone.now, validators=[validate_observed_at])
    idempotency_key = models.UUIDField(unique=True)

    class Meta(AppendOnlyModel.Meta):
        constraints = [
            *money_constraints("payment_allocation"),
            models.CheckConstraint(
                condition=Q(booking_charge__isnull=False, purchase_obligation__isnull=True)
                | Q(booking_charge__isnull=True, purchase_obligation__isnull=False),
                name="allocation_one_obligation",
            ),
            models.UniqueConstraint(
                fields=["gateway_transaction", "purchase_obligation"],
                name="receipt_purchase_allocation",
            ),
            models.UniqueConstraint(
                fields=["gateway_transaction", "booking_charge"],
                name="receipt_charge_allocation_unique",
            ),
        ]


class PaymentResolution(AppendOnlyModel):
    """Attributable provider reconciliation, without choosing refund/retention policy.

    Captured funds may remain a separate financial liability after stock is freed.
    Receipt and allocation totals are frozen observations, never erased history.
    Refs: BUSINESS_RULES.md R-26, R-28, R-30, R-40; DECISIONS.md BD-02, CA-01.
    """

    context = models.ForeignKey(
        PaymentContext, on_delete=models.PROTECT, related_name="resolutions"
    )
    staff_actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT
    )
    system_actor = models.CharField(max_length=128, blank=True)
    reason = models.TextField()
    evidence_reference = models.CharField(max_length=500)
    reconciled_at = models.DateTimeField(default=timezone.now, validators=[validate_observed_at])
    outcome = models.CharField(
        max_length=32,
        choices=[
            ("NO_CAPTURE", "No capture"),
            ("CAPTURE_NOT_ACCEPTED", "Captured funds not accepted for fulfilment"),
        ],
    )
    captured_minor = models.PositiveBigIntegerField()
    allocated_minor = models.PositiveBigIntegerField()
    currency = models.CharField(max_length=3, validators=[currency_validator])

    class Meta(AppendOnlyModel.Meta):
        constraints = [
            models.CheckConstraint(
                condition=Q(staff_actor__isnull=False, system_actor="")
                | (Q(staff_actor__isnull=True) & ~Q(system_actor="")),
                name="resolution_actor",
            ),
            models.CheckConstraint(
                condition=Q(outcome="NO_CAPTURE", captured_minor=0, allocated_minor=0)
                | Q(outcome="CAPTURE_NOT_ACCEPTED"),
                name="resolution_capture_outcome",
            ),
            models.CheckConstraint(
                condition=Q(allocated_minor__lte=F("captured_minor")),
                name="resolution_allocated_bound",
            ),
        ]


class GatewayTransactionMatch(AppendOnlyModel):
    """Later matching adds evidence without rewriting an unmatched receipt.

    Refs: BUSINESS_RULES.md R-26, R-30.
    """

    gateway_transaction = models.OneToOneField(
        GatewayTransaction, on_delete=models.PROTECT, related_name="match"
    )
    attempt = models.ForeignKey(PaymentAttempt, on_delete=models.PROTECT)
    staff_actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT
    )
    system_actor = models.CharField(max_length=128, blank=True)
    reason = models.TextField()
    evidence_reference = models.CharField(max_length=500)
    matched_at = models.DateTimeField(default=timezone.now, validators=[validate_observed_at])

    class Meta(AppendOnlyModel.Meta):
        constraints = [
            models.CheckConstraint(
                condition=Q(staff_actor__isnull=False, system_actor="")
                | (Q(staff_actor__isnull=True) & ~Q(system_actor="")),
                name="receipt_match_actor",
            )
        ]


class PaymentResolutionReceipt(AppendOnlyModel):
    """Exact captured facts considered by reconciliation, including currency mismatches.

    Totals on PaymentResolution describe its stated currency only; other currencies
    remain explicitly attributable through their original receipts.
    Refs: BUSINESS_RULES.md R-26, R-28, R-30, R-40.
    """

    resolution = models.ForeignKey(
        PaymentResolution, on_delete=models.PROTECT, related_name="receipts"
    )
    gateway_transaction = models.ForeignKey(GatewayTransaction, on_delete=models.PROTECT)

    class Meta(AppendOnlyModel.Meta):
        constraints = [
            models.UniqueConstraint(
                fields=["resolution", "gateway_transaction"], name="resolution_receipt_once"
            )
        ]
