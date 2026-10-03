"""Challenge storage only; no OTP issuance, verification or authentication endpoints."""

from datetime import datetime

from django.contrib.auth.hashers import identify_hasher
from django.core.exceptions import ValidationError
from django.core.validators import RegexValidator
from django.db import models
from django.db.models import F, Q
from django.utils import timezone

from common.evidence import RetainedModel

phone_validator = RegexValidator(r"\A\+[1-9][0-9]{1,14}\Z", "Use an E.164 phone number.")
digest_validator = RegexValidator(r"\A[0-9a-f]{64}\Z", "Use a lowercase SHA-256 digest.")


def validate_observed_at(value):
    if not isinstance(value, datetime) or timezone.is_naive(value):
        raise ValidationError("Use a timezone-aware timestamp.")
    if value > timezone.now():
        raise ValidationError("An observed event cannot be in the future.")


class VerificationChallenge(RetainedModel):
    """Bind a hashed secret and monotonic lifecycle to a concrete purpose/subject.

    No security limits are chosen here. The future verification service must lock
    this row, check the secret, increment attempts and consume it atomically.
    Refs: BUSINESS_RULES.md R-20, R-36; DECISIONS.md D-25, BD-15.
    """

    phone = models.CharField(max_length=16, validators=[phone_validator])
    secret_hash = models.CharField(max_length=255)
    issued_at = models.DateTimeField(default=timezone.now, validators=[validate_observed_at])
    expires_at = models.DateTimeField()
    max_attempts = models.PositiveSmallIntegerField()
    attempts_used = models.PositiveSmallIntegerField(default=0)
    verified_at = models.DateTimeField(null=True, blank=True, validators=[validate_observed_at])
    consumed_at = models.DateTimeField(null=True, blank=True, validators=[validate_observed_at])
    invalidated_at = models.DateTimeField(null=True, blank=True, validators=[validate_observed_at])

    immutable_fields = (
        "public_id",
        "phone",
        "secret_hash",
        "issued_at",
        "expires_at",
        "max_attempts",
    )

    class Meta(RetainedModel.Meta):
        abstract = True
        constraints = [
            models.CheckConstraint(
                condition=Q(max_attempts__gt=0) & Q(attempts_used__lte=F("max_attempts")),
                name="%(app_label)s_%(class)s_attempts",
            ),
            models.CheckConstraint(
                condition=Q(expires_at__gt=F("issued_at")),
                name="%(app_label)s_%(class)s_expiry",
            ),
            models.CheckConstraint(
                condition=Q(verified_at__isnull=True)
                | (
                    Q(verified_at__gte=F("issued_at"))
                    & Q(verified_at__lt=F("expires_at"))
                    & Q(attempts_used__gt=0)
                ),
                name="%(app_label)s_%(class)s_verified",
            ),
            models.CheckConstraint(
                condition=Q(consumed_at__isnull=True)
                | (
                    Q(verified_at__isnull=False)
                    & Q(consumed_at__gte=F("verified_at"))
                    & Q(consumed_at__lt=F("expires_at"))
                ),
                name="%(app_label)s_%(class)s_consumed",
            ),
            models.CheckConstraint(
                condition=Q(invalidated_at__isnull=True)
                | (
                    Q(invalidated_at__gte=F("issued_at"))
                    & (Q(verified_at__isnull=True) | Q(verified_at__lte=F("invalidated_at")))
                    & (Q(consumed_at__isnull=True) | Q(consumed_at__lte=F("invalidated_at")))
                ),
                name="%(app_label)s_%(class)s_invalidated",
            ),
        ]

    def clean(self):
        super().clean()
        try:
            identify_hasher(self.secret_hash).decode(self.secret_hash)
        except (ValueError, TypeError) as error:
            raise ValidationError(
                {"secret_hash": "Use a supported encoded secret hash."}
            ) from error
        if self.expires_at and timezone.is_naive(self.expires_at):
            raise ValidationError({"expires_at": "Use a timezone-aware expiry."})
        if self._state.adding and (
            self.attempts_used or self.verified_at or self.consumed_at or self.invalidated_at
        ):
            raise ValidationError("New challenges start unused and unverified.")

    def validate_existing_identity(self, previous):
        super().validate_existing_identity(previous)
        if self.attempts_used < previous.attempts_used:
            raise ValidationError("Challenge attempts cannot decrease.")
        if previous.verified_at and self.attempts_used != previous.attempts_used:
            raise ValidationError("Verified challenges cannot accept further attempts.")
        if (
            self.verified_at
            and not previous.verified_at
            and (self.attempts_used <= previous.attempts_used)
        ):
            raise ValidationError("Verification must record a new bounded attempt.")
        for field in ("verified_at", "consumed_at", "invalidated_at"):
            old = getattr(previous, field)
            if old is not None and getattr(self, field) != old:
                raise ValidationError("Recorded challenge outcomes cannot be rewritten.")
        if previous.invalidated_at or previous.consumed_at:
            if any(
                getattr(self, field) != getattr(previous, field)
                for field in ("attempts_used", "verified_at", "consumed_at", "invalidated_at")
            ):
                raise ValidationError("A closed challenge cannot be reused.")
