import uuid

from django.contrib.auth.models import AbstractUser
from django.core.exceptions import ValidationError
from django.db import models

from common.evidence import RetainedModel
from common.verification import validate_observed_at


class StaffUser(AbstractUser):
    """Staff identity leaves guest browsing and booking free of a login requirement.

    Assignment permissions belong to the later staff workflow, not this identity alone.
    Registered customers use CustomerAccount, never this permission-bearing table.
    Refs: BUSINESS_RULES.md R-04, R-18, R-33; DECISIONS.md D-23.
    """

    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)

    class Meta:
        verbose_name = "staff user"
        verbose_name_plural = "staff users"


class CustomerAccount(RetainedModel):
    """Stable customer identity, not an authenticated principal or a staff account.

    Contact/credential recovery and login transport require separate design. No
    phone-based lookup, auth property, staff permission or signup is enabled here.
    Refs: BUSINESS_RULES.md R-33, R-34, R-37; DECISIONS.md D-23.
    """

    class Status(models.TextChoices):
        PROVISIONED = "PROVISIONED", "Provisioned / not enabled"
        ACTIVE = "ACTIVE", "Active"
        SUSPENDED = "SUSPENDED", "Suspended"
        CLOSED = "CLOSED", "Closed"

    display_name = models.CharField(max_length=200, blank=True)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PROVISIONED)
    closed_at = models.DateTimeField(null=True, blank=True, validators=[validate_observed_at])

    class Meta(RetainedModel.Meta):
        constraints = [
            models.CheckConstraint(
                condition=models.Q(status__in=["PROVISIONED", "ACTIVE", "SUSPENDED", "CLOSED"]),
                name="customer_account_status_valid",
            ),
            models.CheckConstraint(
                condition=models.Q(status="CLOSED", closed_at__isnull=False)
                | (~models.Q(status="CLOSED") & models.Q(closed_at__isnull=True)),
                name="customer_account_closure_valid",
            ),
        ]

    def validate_existing_identity(self, previous):
        super().validate_existing_identity(previous)
        if previous.status == self.Status.CLOSED and (
            self.status != previous.status or self.closed_at != previous.closed_at
        ):
            raise ValidationError("Closed customer identity cannot be reactivated or reassigned.")

    def __str__(self):
        return str(self.public_id)
