"""Booking persistence for guest and account checkout; workflows remain Task 6."""

import uuid

from django.core.exceptions import ValidationError
from django.core.validators import RegexValidator
from django.db import models
from django.db.models import F, Q
from django.utils import timezone

from common.evidence import AppendOnlyModel, RetainedModel
from common.persistence import LifecycleModel
from common.verification import (
    VerificationChallenge,
    digest_validator,
    phone_validator,
    validate_observed_at,
)


class TrialBooking(LifecycleModel):
    """One immutable checkout identity and original recipient snapshot for both modes.

    Lifecycle storage does not issue OTPs, reserve stock or verify provider payments.
    Phone edits require new intent, never rewriting a previously verified snapshot.
    Refs: BUSINESS_RULES.md R-01, R-02, R-34, R-35; DECISIONS.md D-24, D-25.
    """

    class CheckoutMode(models.TextChoices):
        GUEST = "GUEST", "Guest"
        ACCOUNT = "ACCOUNT", "Customer account"

    class Status(models.TextChoices):
        DRAFT = "DRAFT", "Draft / not confirmed"
        VERIFIED = "VERIFIED", "Phone verified"
        PAYMENT_PENDING = "PAYMENT_PENDING", "Payment in progress"
        CONFIRMED = "CONFIRMED", "Booking confirmed"
        PAYMENT_EXPIRED = "PAYMENT_EXPIRED", "Payment window expired"
        CANCELLED = "CANCELLED", "Cancelled before confirmation"

    checkout_mode = models.CharField(max_length=8, choices=CheckoutMode.choices)
    customer_account = models.ForeignKey(
        "accounts.CustomerAccount",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="bookings",
    )
    market = models.ForeignKey("markets.Market", on_delete=models.PROTECT, related_name="bookings")
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.DRAFT)
    contact_name = models.CharField(max_length=200)
    contact_phone = models.CharField(max_length=16, validators=[phone_validator])
    address = models.TextField()
    country_code = models.CharField(
        max_length=2, validators=[RegexValidator(r"\A[A-Z]{2}\Z", "Use a country code.")]
    )
    postal_code = models.CharField(max_length=20)
    request_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    request_fingerprint = models.CharField(max_length=64, validators=[digest_validator])
    verified_phone_challenge = models.OneToOneField(
        "BookingPhoneChallenge",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="verified_booking",
    )
    verification_valid_until = models.DateTimeField(null=True, blank=True)
    sealed_at = models.DateTimeField(null=True, blank=True, validators=[validate_observed_at])
    confirmed_at = models.DateTimeField(null=True, blank=True, validators=[validate_observed_at])
    acceptance_snapshot = models.JSONField(default=dict, blank=True)

    mutable_fields = (
        "status",
        "verified_phone_challenge_id",
        "verification_valid_until",
        "sealed_at",
        "confirmed_at",
        "acceptance_snapshot",
    )
    write_once_fields = mutable_fields[1:]
    initial_status = Status.DRAFT
    transitions = {
        "DRAFT": ("VERIFIED", "CANCELLED"),
        "VERIFIED": ("PAYMENT_PENDING", "CANCELLED"),
        "PAYMENT_PENDING": ("CONFIRMED", "PAYMENT_EXPIRED", "CANCELLED"),
    }

    immutable_fields = (
        "public_id",
        "checkout_mode",
        "customer_account_id",
        "market_id",
        "contact_name",
        "contact_phone",
        "address",
        "country_code",
        "postal_code",
        "request_id",
        "request_fingerprint",
    )

    class Meta(RetainedModel.Meta):
        constraints = [
            models.CheckConstraint(
                condition=Q(checkout_mode="GUEST", customer_account__isnull=True)
                | Q(checkout_mode="ACCOUNT", customer_account__isnull=False),
                name="booking_checkout_owner_valid",
            ),
            models.CheckConstraint(
                condition=Q(
                    status__in=[
                        "DRAFT",
                        "VERIFIED",
                        "PAYMENT_PENDING",
                        "CONFIRMED",
                        "PAYMENT_EXPIRED",
                        "CANCELLED",
                    ]
                ),
                name="booking_status_valid",
            ),
            models.CheckConstraint(
                condition=Q(status__in=["DRAFT", "CANCELLED"])
                | (
                    Q(verified_phone_challenge__isnull=False)
                    & Q(verification_valid_until__isnull=False)
                    & Q(sealed_at__isnull=False)
                    & ~Q(acceptance_snapshot={})
                ),
                name="booking_verification_required",
            ),
            models.CheckConstraint(
                condition=Q(status="CONFIRMED", confirmed_at__isnull=False)
                | (~Q(status="CONFIRMED") & Q(confirmed_at__isnull=True)),
                name="booking_confirmation_time",
            ),
            models.CheckConstraint(
                condition=~Q(contact_name="")
                & ~Q(contact_phone="")
                & ~Q(address="")
                & ~Q(country_code="")
                & ~Q(postal_code=""),
                name="booking_contact_required",
            ),
        ]
        indexes = [
            models.Index(
                fields=["market", "status", "created_at"], name="booking_market_state_idx"
            ),
            models.Index(
                fields=["customer_account", "created_at"], name="booking_customer_time_idx"
            ),
        ]

    def clean(self):
        super().clean()
        for field in ("contact_name", "address", "postal_code"):
            value = getattr(self, field)
            if not isinstance(value, str) or not value.strip() or value != value.strip():
                raise ValidationError({field: "Provide nonblank text without outer whitespace."})
        if self.country_code == "IN" and (
            len(self.postal_code) != 6
            or not self.postal_code.isascii()
            or not self.postal_code.isdigit()
            or self.postal_code.startswith("0")
        ):
            raise ValidationError({"postal_code": "Provide a valid six-digit Indian PIN."})
        if self.market_id and self.market.country_code != self.country_code:
            raise ValidationError(
                {"country_code": "The address must use the selected market country."}
            )
        if self.verified_phone_challenge_id:
            proof = self.verified_phone_challenge
            if (
                proof.booking_id != self.pk
                or not proof.consumed_at
                or proof.invalidated_at
                or not self.verification_valid_until
                or self.verification_valid_until <= proof.consumed_at
            ):
                raise ValidationError("Use consumed phone proof belonging to this booking.")
        if self.status == self.Status.CONFIRMED:
            from payments.models import PaymentContext

            if not PaymentContext.objects.filter(
                booking_charge__booking_id=self.pk, status="PAID"
            ).exists():
                raise ValidationError("Confirmation requires a paid booking context.")

    def __str__(self):
        return str(self.public_id)


class TrialBox(AppendOnlyModel):
    """Category container, not a separate checkout or an approved plan/stock hold.

    Repeated categories are not prohibited while BD-03 remains open.
    Refs: BUSINESS_RULES.md R-01, R-02; DECISIONS.md BD-03.
    """

    booking = models.ForeignKey(TrialBooking, on_delete=models.PROTECT, related_name="boxes")
    category = models.ForeignKey(
        "catalog.Category", on_delete=models.PROTECT, related_name="trial_boxes"
    )
    plan_revision = models.ForeignKey(
        "TrialPlanRevision",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="boxes",
    )
    plan_snapshot = models.JSONField(default=dict, blank=True)

    def clean(self):
        super().clean()
        if self.booking_id and (self.booking.sealed_at or self.booking.status != "DRAFT"):
            raise ValidationError("Sealed booking contents cannot be extended.")
        if self.plan_revision_id and (
            self.plan_revision.market_id != self.booking.market_id
            or self.plan_revision.category_id != self.category_id
            or not self.plan_snapshot
        ):
            raise ValidationError("Box plan and snapshot must match its market and category.")


class BookingPhoneChallenge(VerificationChallenge):
    booking = models.ForeignKey(
        TrialBooking, on_delete=models.PROTECT, related_name="phone_challenges"
    )

    immutable_fields = (*VerificationChallenge.immutable_fields, "booking_id")

    def clean(self):
        super().clean()
        if self.booking_id and self.phone != self.booking.contact_phone:
            raise ValidationError({"phone": "Challenge phone must match this booking snapshot."})


class GuestBookingAccessGrant(RetainedModel):
    """Booking permission for an anonymous browser, never customer ownership.

    Token generation, authorization and recovery remain Task 6 workflows.
    Refs: BUSINESS_RULES.md R-34, R-37; DECISIONS.md D-24.
    """

    booking = models.ForeignKey(TrialBooking, on_delete=models.PROTECT, related_name="guest_grants")
    browser_session = models.ForeignKey(
        "GuestBrowserSession", on_delete=models.PROTECT, related_name="booking_grants"
    )
    scope = models.CharField(max_length=16, default="BOOKING", choices=[("BOOKING", "Booking")])
    issued_at = models.DateTimeField(default=timezone.now, validators=[validate_observed_at])
    expires_at = models.DateTimeField()
    revoked_at = models.DateTimeField(null=True, blank=True, validators=[validate_observed_at])

    immutable_fields = (
        "public_id",
        "booking_id",
        "browser_session_id",
        "scope",
        "issued_at",
        "expires_at",
    )

    class Meta(RetainedModel.Meta):
        constraints = [
            models.CheckConstraint(condition=Q(scope="BOOKING"), name="guest_grant_scope"),
            models.CheckConstraint(
                condition=Q(expires_at__gt=F("issued_at")), name="guest_grant_expiry"
            ),
            models.CheckConstraint(
                condition=Q(revoked_at__isnull=True) | Q(revoked_at__gte=F("issued_at")),
                name="guest_grant_revocation",
            ),
        ]

    def clean(self):
        super().clean()
        if self.booking_id and self.booking.checkout_mode != TrialBooking.CheckoutMode.GUEST:
            raise ValidationError({"booking": "Guest access cannot target an account booking."})
        if self.expires_at and timezone.is_naive(self.expires_at):
            raise ValidationError({"expires_at": "Use a timezone-aware expiry."})
        if self.browser_session_id and (
            self.issued_at < self.browser_session.issued_at
            or self.expires_at > self.browser_session.expires_at
        ):
            raise ValidationError("Grant validity must stay within its browser session.")
        # Issuance cannot revive revoked/expired browser authority. Refs: R-39; D-27.
        if (
            self._state.adding
            and self.browser_session_id
            and (
                self.browser_session.revoked_at is not None
                or self.browser_session.expires_at <= timezone.now()
                or self.expires_at <= timezone.now()
            )
        ):
            raise ValidationError("New grants require a live browser session.")

    def validate_existing_identity(self, previous):
        super().validate_existing_identity(previous)
        if previous.revoked_at is not None and self.revoked_at != previous.revoked_at:
            raise ValidationError("A revoked credential cannot be restored or rewritten.")


class BookingPolicyAcceptance(AppendOnlyModel):
    """Acceptance evidence is booking-scoped, with no indirect guest account link.

    OTP possession is not consent; the checkout workflow supplies actual acceptance.
    Refs: BUSINESS_RULES.md R-32, R-34, R-36; DECISIONS.md LR-04.
    """

    booking = models.ForeignKey(
        TrialBooking, on_delete=models.PROTECT, related_name="policy_acceptances"
    )
    policy_revision = models.ForeignKey(
        "compliance.PolicyDocumentRevision",
        on_delete=models.PROTECT,
        related_name="booking_acceptances",
    )
    accepted_at = models.DateTimeField(validators=[validate_observed_at])
    request_id = models.UUIDField(unique=True)
    request_fingerprint = models.CharField(max_length=64, validators=[digest_validator])


# Register the split persistence module without importing services or providers.
from .checkout import (  # noqa: E402, F401
    GuestBookingRecoveryGrant,
    GuestBrowserSession,
    GuestRecoveryPhoneChallenge,
    IdempotencyRecord,
    TrialBoxItem,
    TrialItemAllocation,
    TrialPlanRevision,
)
