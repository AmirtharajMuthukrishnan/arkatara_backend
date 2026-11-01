"""Shared integrity primitives for retained checkout evidence, not workflow services."""

from django.core.exceptions import ValidationError
from django.core.validators import RegexValidator
from django.db import models
from django.db.models import F, Q

from common.evidence import RetainedModel

currency_validator = RegexValidator(r"\A[A-Z]{3}\Z", "Use a three-letter currency code.")


def money_constraints(prefix):
    return [
        models.CheckConstraint(condition=Q(amount_minor__gt=0), name=f"{prefix}_positive_amount"),
        models.CheckConstraint(
            condition=Q(currency__regex=r"^[A-Z]{3}$"), name=f"{prefix}_currency"
        ),
    ]


def expiry_constraints(prefix):
    return [
        models.CheckConstraint(condition=Q(expires_at__gt=F("issued_at")), name=f"{prefix}_expiry"),
        models.CheckConstraint(
            condition=Q(revoked_at__isnull=True) | Q(revoked_at__gte=F("issued_at")),
            name=f"{prefix}_revoked",
        ),
    ]


class LifecycleModel(RetainedModel):
    """Explicit mutable fields; identity, snapshots and recorded outcomes stay fixed.

    PostgreSQL migrations also enforce these rules against bulk/SQL bypasses.
    Refs: BUSINESS_RULES.md R-28, R-30; DECISIONS.md D-27.
    """

    mutable_fields = ()
    write_once_fields = ()
    initial_status = None
    transitions = {}

    class Meta(RetainedModel.Meta):
        abstract = True

    def clean(self):
        super().clean()
        if self._state.adding and self.initial_status and self.status != self.initial_status:
            raise ValidationError({"status": "New records must start in their initial state."})

    def validate_existing_identity(self, previous):
        super().validate_existing_identity(previous)
        mutable = {*self.mutable_fields, "updated_at"}
        for field in self._meta.concrete_fields:
            name = field.attname
            if name not in mutable and getattr(self, name) != getattr(previous, name):
                raise ValidationError({field.name: "Recorded identity or evidence is immutable."})
        for name in self.write_once_fields:
            old = getattr(previous, name)
            if old is not None and old != "" and old != {} and getattr(self, name) != old:
                raise ValidationError({name: "Recorded outcomes cannot be rewritten."})
        if self.transitions and self.status != previous.status:
            if self.status not in self.transitions.get(previous.status, ()):
                raise ValidationError({"status": "This lifecycle transition is not permitted."})
