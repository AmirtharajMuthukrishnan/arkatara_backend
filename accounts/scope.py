"""Explicit operational scope evidence; endpoints must still authorize each action."""

from django.conf import settings
from django.db import models
from django.db.models import F, Q

from common.persistence import LifecycleModel
from common.verification import validate_observed_at


class StaffScopeGrant(LifecycleModel):
    """One action in a market, optionally restricted to one hub, with revocation evidence.

    This record never substitutes for active staff/MFA/current visit assignment.
    Refs: BUSINESS_RULES.md R-30, R-33, R-38; DECISIONS.md D-23.
    """

    staff = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="operational_scope_grants"
    )
    permission = models.ForeignKey("auth.Permission", on_delete=models.PROTECT)
    market = models.ForeignKey("markets.Market", on_delete=models.PROTECT)
    hub = models.ForeignKey("markets.Hub", null=True, blank=True, on_delete=models.PROTECT)
    granted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="made_scope_grants"
    )
    granted_at = models.DateTimeField(validators=[validate_observed_at])
    reason = models.TextField()
    revoked_at = models.DateTimeField(null=True, blank=True, validators=[validate_observed_at])
    revoked_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="revoked_scope_grants",
    )
    revocation_reason = models.TextField(blank=True)
    mutable_fields = ("revoked_at", "revoked_by_id", "revocation_reason")
    write_once_fields = mutable_fields

    class Meta(LifecycleModel.Meta):
        constraints = [
            models.CheckConstraint(
                condition=Q(revoked_at__isnull=True, revoked_by__isnull=True, revocation_reason="")
                | (
                    Q(
                        revoked_at__isnull=False,
                        revoked_at__gte=F("granted_at"),
                        revoked_by__isnull=False,
                    )
                    & ~Q(revocation_reason="")
                ),
                name="staff_scope_revocation",
            )
        ]
