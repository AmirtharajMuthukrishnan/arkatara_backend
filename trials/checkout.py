"""Anonymous access, immutable selections and request replay persistence."""

from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import F, Q
from django.utils import timezone

from common.evidence import AppendOnlyModel
from common.persistence import LifecycleModel, expiry_constraints
from common.verification import VerificationChallenge, digest_validator, validate_observed_at
from compliance.configuration import validate_policy_binding


class GuestBrowserSession(LifecycleModel):
    """Digest of a random browser credential, with no phone or customer-account FK.

    Task 6 issues a host-only Secure/HttpOnly cookie and enforces CSRF/expiry.
    Rotation changes the credential without losing per-booking grants.
    Refs: BUSINESS_RULES.md R-34, R-37; DECISIONS.md D-27.
    """

    token_digest = models.CharField(max_length=64, unique=True, validators=[digest_validator])
    issued_at = models.DateTimeField(default=timezone.now, validators=[validate_observed_at])
    expires_at = models.DateTimeField()
    revoked_at = models.DateTimeField(null=True, blank=True, validators=[validate_observed_at])
    rotated_at = models.DateTimeField(null=True, blank=True, validators=[validate_observed_at])

    mutable_fields = ("token_digest", "revoked_at", "rotated_at")
    write_once_fields = ("revoked_at",)

    class Meta(LifecycleModel.Meta):
        constraints = [
            *expiry_constraints("guest_session"),
            models.CheckConstraint(
                condition=Q(token_digest__regex=r"^[0-9a-f]{64}$"), name="guest_session_digest"
            ),
            models.CheckConstraint(
                condition=Q(rotated_at__isnull=True)
                | (Q(rotated_at__gte=F("issued_at")) & Q(rotated_at__lt=F("expires_at"))),
                name="guest_session_rotation_time",
            ),
        ]
        indexes = [models.Index(fields=["expires_at"], name="guest_session_expiry_idx")]

    def validate_existing_identity(self, previous):
        super().validate_existing_identity(previous)
        changed = self.token_digest != previous.token_digest
        if changed != (self.rotated_at != previous.rotated_at):
            raise ValidationError("Credential rotation must record a new digest and timestamp.")
        if changed and (
            previous.revoked_at
            or not self.rotated_at
            or self.rotated_at <= (previous.rotated_at or previous.issued_at)
        ):
            raise ValidationError("Closed credentials cannot rotate or move backwards.")


class GuestBookingRecoveryGrant(LifecycleModel):
    """Single-booking recovery secret; redemption also requires fresh recovery OTP.

    A link alone never reveals history and a phone never searches all bookings.
    Refs: BUSINESS_RULES.md R-34, R-37; DECISIONS.md D-27, LR-04.
    """

    booking = models.ForeignKey("trials.TrialBooking", on_delete=models.PROTECT)
    token_digest = models.CharField(max_length=64, unique=True, validators=[digest_validator])
    issued_at = models.DateTimeField(default=timezone.now, validators=[validate_observed_at])
    expires_at = models.DateTimeField()
    revoked_at = models.DateTimeField(null=True, blank=True, validators=[validate_observed_at])
    redeemed_at = models.DateTimeField(null=True, blank=True, validators=[validate_observed_at])
    redeemed_session = models.ForeignKey(
        GuestBrowserSession, null=True, blank=True, on_delete=models.PROTECT
    )
    verified_challenge = models.OneToOneField(
        "GuestRecoveryPhoneChallenge",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="redeemed_grant",
    )
    mutable_fields = ("revoked_at", "redeemed_at", "redeemed_session_id", "verified_challenge_id")
    write_once_fields = mutable_fields

    class Meta(LifecycleModel.Meta):
        constraints = [
            *expiry_constraints("guest_recovery"),
            models.CheckConstraint(
                condition=Q(
                    redeemed_at__isnull=True,
                    redeemed_session__isnull=True,
                    verified_challenge__isnull=True,
                )
                | (
                    Q(redeemed_at__gte=F("issued_at"), redeemed_at__lt=F("expires_at"))
                    & Q(redeemed_session__isnull=False, verified_challenge__isnull=False)
                ),
                name="guest_recovery_redemption",
            ),
        ]

    def clean(self):
        super().clean()
        if self.booking_id and self.booking.checkout_mode != "GUEST":
            raise ValidationError("Recovery requires a guest booking.")
        if self.verified_challenge_id:
            proof = self.verified_challenge
            if proof.recovery_grant_id != self.pk or not proof.consumed_at or proof.invalidated_at:
                raise ValidationError("Recovery requires its own consumed phone challenge.")


class GuestRecoveryPhoneChallenge(VerificationChallenge):
    """Recovery proof is a different purpose from booking, arrival or account login."""

    recovery_grant = models.ForeignKey(
        GuestBookingRecoveryGrant, on_delete=models.PROTECT, related_name="phone_challenges"
    )
    immutable_fields = (*VerificationChallenge.immutable_fields, "recovery_grant_id")

    def clean(self):
        super().clean()
        if self.recovery_grant_id and self.phone != self.recovery_grant.booking.contact_phone:
            raise ValidationError("Recovery phone must match the booking snapshot.")


class TrialPlanRevision(AppendOnlyModel):
    """Versioned offering evidence; no sample box counts, amounts or metal rules.

    A recorded revision does not activate an offering. Task 6 checks approved
    configuration and its effective scope.
    Refs: BUSINESS_RULES.md R-28; DECISIONS.md BD-03, BD-13.
    """

    code = models.SlugField(max_length=64)
    version = models.PositiveIntegerField()
    market = models.ForeignKey("markets.Market", on_delete=models.PROTECT)
    category = models.ForeignKey("catalog.Category", on_delete=models.PROTECT)
    policy_revision = models.ForeignKey(
        "compliance.BusinessConfiguration", on_delete=models.PROTECT
    )
    # Technical identity version preserves old scopes without rewriting policy history.
    # Refs: BUSINESS_RULES.md R-07, R-28.
    policy_scope_version = models.PositiveSmallIntegerField(default=2)
    policy_snapshot = models.JSONField()
    effective_from = models.DateTimeField()
    effective_until = models.DateTimeField(null=True, blank=True)

    class Meta(AppendOnlyModel.Meta):
        constraints = [
            models.UniqueConstraint(
                fields=["market", "category", "code", "version"], name="trial_plan_revision_unique"
            ),
            models.CheckConstraint(condition=Q(version__gt=0), name="trial_plan_version_positive"),
            models.CheckConstraint(
                condition=Q(policy_scope_version__in=[1, 2]), name="plan_policy_scope_version"
            ),
            models.CheckConstraint(
                condition=Q(effective_until__isnull=True)
                | Q(effective_until__gt=F("effective_from")),
                name="trial_plan_effective_window",
            ),
        ]

    def clean(self):
        super().clean()
        if self._state.adding and self.policy_scope_version != 2:
            raise ValidationError("New policy bindings require public UUID scope identifiers.")
        if self.policy_revision_id and self.market_id and self.category_id:
            validate_policy_binding(
                self.policy_revision,
                "trials",
                "plan-policy-reference",
                market=self.market,
                category=self.category,
                scope_version=self.policy_scope_version,
            )


class TrialBoxItem(AppendOnlyModel):
    """Requested variant and quantity; separate from physical stock allocation."""

    box = models.ForeignKey("trials.TrialBox", on_delete=models.PROTECT, related_name="items")
    variant = models.ForeignKey("catalog.ProductVariant", on_delete=models.PROTECT)
    quantity = models.PositiveIntegerField()
    selection_snapshot = models.JSONField()

    class Meta(AppendOnlyModel.Meta):
        constraints = [
            models.CheckConstraint(
                condition=Q(quantity__gt=0), name="trial_item_quantity_positive"
            ),
        ]

    def clean(self):
        super().clean()
        if self.box_id and self.variant_id:
            if self.variant.product.category_id != self.box.category_id:
                raise ValidationError("Selected variant must belong to the box category.")
            if self.box.booking.sealed_at or self.box.booking.status != "DRAFT":
                raise ValidationError("Sealed booking selections cannot be extended.")


class TrialItemAllocation(LifecycleModel):
    """One requested piece fulfilled by one exclusive physical reservation.

    Binding checks also run in PostgreSQL; releasing this row cannot free the
    unit while its reservation remains live. Refs: BUSINESS_RULES.md R-08, R-14.
    """

    trial_box_item = models.ForeignKey(
        TrialBoxItem, on_delete=models.PROTECT, related_name="allocations"
    )
    reservation = models.OneToOneField(
        "inventory.InventoryReservation", on_delete=models.PROTECT, related_name="allocation"
    )
    allocated_at = models.DateTimeField(default=timezone.now, validators=[validate_observed_at])
    released_at = models.DateTimeField(null=True, blank=True, validators=[validate_observed_at])
    mutable_fields = ("released_at",)
    write_once_fields = mutable_fields

    class Meta(LifecycleModel.Meta):
        constraints = [
            models.CheckConstraint(
                condition=Q(released_at__isnull=True) | Q(released_at__gte=F("allocated_at")),
                name="trial_allocation_release_time",
            )
        ]

    def clean(self):
        super().clean()
        if self.trial_box_item_id and self.reservation_id:
            item, hold = self.trial_box_item, self.reservation
            if (
                item.variant_id != hold.unit.variant_id
                or item.box.booking_id != hold.payment_context.booking_charge.booking_id
            ):
                raise ValidationError("Allocation must match the booking and requested variant.")


class IdempotencyRecord(LifecycleModel):
    """A request key is scoped to principal and operation, never an access credential.

    No stored response body may leak cookies/OTPs. Task 6 reauthorizes retries and
    reconstructs responses from result references.
    Refs: BUSINESS_RULES.md R-24, R-34; DECISIONS.md D-27.
    """

    scope = models.CharField(max_length=64)
    key = models.UUIDField()
    guest_session = models.ForeignKey(
        GuestBrowserSession, null=True, blank=True, on_delete=models.PROTECT
    )
    customer_account = models.ForeignKey(
        "accounts.CustomerAccount", null=True, blank=True, on_delete=models.PROTECT
    )
    request_fingerprint = models.CharField(max_length=64, validators=[digest_validator])
    status = models.CharField(
        max_length=16,
        default="IN_PROGRESS",
        choices=[("IN_PROGRESS", "In progress"), ("COMPLETED", "Completed"), ("FAILED", "Failed")],
    )
    booking = models.ForeignKey(
        "trials.TrialBooking", null=True, blank=True, on_delete=models.PROTECT
    )
    payment_attempt = models.ForeignKey(
        "payments.PaymentAttempt", null=True, blank=True, on_delete=models.PROTECT
    )
    response_status = models.PositiveSmallIntegerField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True, validators=[validate_observed_at])
    mutable_fields = (
        "status",
        "booking_id",
        "payment_attempt_id",
        "response_status",
        "completed_at",
    )
    write_once_fields = mutable_fields[1:]
    initial_status = "IN_PROGRESS"
    transitions = {"IN_PROGRESS": ("COMPLETED", "FAILED")}

    class Meta(LifecycleModel.Meta):
        constraints = [
            models.CheckConstraint(
                condition=Q(guest_session__isnull=False, customer_account__isnull=True)
                | Q(guest_session__isnull=True, customer_account__isnull=False),
                name="request_one_principal",
            ),
            models.UniqueConstraint(
                fields=["guest_session", "scope", "key"],
                condition=Q(guest_session__isnull=False),
                name="request_guest_key",
            ),
            models.UniqueConstraint(
                fields=["customer_account", "scope", "key"],
                condition=Q(customer_account__isnull=False),
                name="request_account_key",
            ),
            models.CheckConstraint(
                condition=Q(
                    status="IN_PROGRESS", completed_at__isnull=True, response_status__isnull=True
                )
                | Q(
                    status__in=["COMPLETED", "FAILED"],
                    completed_at__isnull=False,
                    response_status__gte=100,
                    response_status__lte=599,
                ),
                name="request_completion",
            ),
        ]
