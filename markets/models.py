"""Market identity and reviewed postal coverage, independent of hub routing."""

from datetime import datetime

from django.core.exceptions import ValidationError
from django.core.validators import RegexValidator
from django.db import models
from django.utils import timezone

from common.reference import CodedReferenceModel, ValidatedReferenceModel


class Market(CodedReferenceModel):
    country_code = models.CharField(
        max_length=2,
        validators=[RegexValidator(r"\A[A-Z]{2}\Z", "Use a two-letter uppercase country code.")],
    )

    immutable_fields = (*CodedReferenceModel.immutable_fields, "country_code")


class Hub(CodedReferenceModel):
    market = models.ForeignKey(Market, on_delete=models.PROTECT, related_name="hubs")

    immutable_fields = (*CodedReferenceModel.immutable_fields, "market_id")


class ServiceArea(ValidatedReferenceModel):
    class Status(models.TextChoices):
        DRAFT = "DRAFT", "Draft"
        ACTIVE = "ACTIVE", "Active"
        INACTIVE = "INACTIVE", "Inactive"

    market = models.ForeignKey(Market, on_delete=models.PROTECT, related_name="service_areas")
    country_code = models.CharField(
        max_length=2,
        validators=[RegexValidator(r"\A[A-Z]{2}\Z", "Use a two-letter uppercase country code.")],
    )
    postal_code = models.CharField(max_length=20)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.DRAFT)
    effective_from = models.DateTimeField(null=True, blank=True)
    effective_to = models.DateTimeField(null=True, blank=True)
    approval_reference = models.CharField(max_length=500, blank=True)

    immutable_fields = ("public_id", "market_id", "country_code", "postal_code")

    class Meta(ValidatedReferenceModel.Meta):
        ordering = ["market__code", "postal_code", "effective_from"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(status__in=["DRAFT", "ACTIVE", "INACTIVE"]),
                name="service_area_status_valid",
            ),
            models.CheckConstraint(
                condition=models.Q(effective_to__isnull=True)
                | models.Q(effective_from__isnull=True)
                | models.Q(effective_to__gt=models.F("effective_from")),
                name="service_area_effective_range_valid",
            ),
            models.CheckConstraint(
                condition=~models.Q(status="ACTIVE")
                | (models.Q(effective_from__isnull=False) & ~models.Q(approval_reference="")),
                name="service_area_active_evidence_required",
            ),
        ]
        indexes = [
            models.Index(
                fields=["market", "country_code", "postal_code", "status"],
                name="service_area_lookup_idx",
            )
        ]

    def clean(self) -> None:
        super().clean()
        errors = {}
        if (
            not isinstance(self.postal_code, str)
            or self.postal_code != self.postal_code.strip()
            or not self.postal_code.strip()
        ):
            errors["postal_code"] = "Use a nonempty postal identifier without outer whitespace."
        if self.country_code == "IN" and (
            not isinstance(self.postal_code, str)
            or len(self.postal_code) != 6
            or not self.postal_code.isascii()
            or not self.postal_code.isdigit()
            or self.postal_code.startswith("0")
        ):
            errors["postal_code"] = (
                "An Indian PIN code must contain six digits and not start with zero."
            )
        if self.market_id:
            country = (
                Market.objects.filter(pk=self.market_id)
                .values_list("country_code", flat=True)
                .first()
            )
            if country is not None and country != self.country_code:
                errors["country_code"] = "The service area must use its market's country."
        for field in ("effective_from", "effective_to"):
            value = getattr(self, field)
            if isinstance(value, datetime) and timezone.is_naive(value):
                errors[field] = "Use a timezone-aware effective timestamp."
        if self.status == self.Status.ACTIVE and (
            not isinstance(self.approval_reference, str)
            or not self.approval_reference.strip()
            or self.effective_from is None
        ):
            errors["status"] = "Active coverage requires an approval reference and effective start."
        if errors:
            raise ValidationError(errors)

    def __str__(self) -> str:
        return f"{self.market.code}: {self.country_code} {self.postal_code}"
