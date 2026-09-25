"""Price proposals and exact inputs; commercial calculation remains BD-04/CA-02."""

import re
import uuid
from decimal import Decimal, InvalidOperation

from django.core.exceptions import ValidationError
from django.core.validators import DecimalValidator, RegexValidator
from django.db import models, router, transaction
from django.db.models import Q

from compliance.models import ImmutableQuerySet

# Storage capacity, not a currency exponent or a transaction rounding policy.
MAX_DIGITS = 24
DECIMAL_PLACES = 6


def exact_decimal(value, *, nonnegative=True) -> Decimal:
    """Reject binary floats, nonfinite numbers and precision loss; never round."""
    if isinstance(value, bool) or not isinstance(value, Decimal | str | int):
        raise ValidationError("Use an exact decimal string or Decimal, never a float.")
    try:
        result = Decimal(value)
    except InvalidOperation as error:
        raise ValidationError("Use a valid exact decimal.") from error
    if not result.is_finite():
        raise ValidationError("Decimal values must be finite.")
    DecimalValidator(MAX_DIGITS, DECIMAL_PLACES)(result)
    if nonnegative and result < 0:
        raise ValidationError("Decimal values must be nonnegative.")
    return result.copy_abs() if result.is_zero() else result


def decimal_text(value, *, nonnegative=True) -> str:
    return format(exact_decimal(value, nonnegative=nonnegative), "f")


def validate_currency(value) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Z]{3}", value):
        raise ValidationError("Currency must be an explicit three-letter uppercase code.")
    return value


def text_value(value, *, required=True) -> str:
    if not isinstance(value, str) or (required and not value.strip()):
        raise ValidationError("A nonblank text value is required.")
    if value != value.strip():
        raise ValidationError("Text values must not contain surrounding whitespace.")
    return value


def validate_charges(value) -> list[dict]:
    """Typed monetary inputs in the revision currency, without a sum/formula."""
    if not isinstance(value, list):
        raise ValidationError("Charges must be a list of named exact monetary inputs.")
    normalized = []
    codes = set()
    for charge in value:
        if not isinstance(charge, dict) or not {"code", "amount"} <= charge.keys():
            raise ValidationError("Each charge requires code and amount.")
        if charge.keys() - {"code", "amount", "source_reference"}:
            raise ValidationError("Unknown charge fields are not accepted.")
        code = text_value(charge["code"])
        if code in codes:
            raise ValidationError("Charge codes must be unique within a revision.")
        codes.add(code)
        normalized.append(
            {
                "code": code,
                "amount": decimal_text(charge["amount"]),
                "source_reference": text_value(charge.get("source_reference", ""), required=False),
            }
        )
    return normalized


def validate_pricing_inputs(value) -> dict:
    """Accept partial draft evidence without inventing units, a source or a formula."""
    if not isinstance(value, dict):
        raise ValidationError("Pricing inputs must be an object.")
    allowed = {
        "weight_value",
        "weight_unit",
        "rate_amount",
        "rate_unit",
        "rate_source_reference",
        "formula_reference",
        "charges",
    }
    if value.keys() - allowed:
        raise ValidationError("Unknown pricing input fields are not accepted.")
    normalized = {}
    for amount_key, unit_key in (("weight_value", "weight_unit"), ("rate_amount", "rate_unit")):
        amount = value.get(amount_key)
        unit = value.get(unit_key)
        if (amount is None) != (unit in (None, "")):
            raise ValidationError("Each weight or rate value requires its explicit unit.")
        if amount is not None:
            number = exact_decimal(amount)
            if amount_key == "weight_value" and number <= 0:
                raise ValidationError("A supplied weight must be positive.")
            normalized[amount_key] = format(number, "f")
            normalized[unit_key] = text_value(unit)
    for name in ("rate_source_reference", "formula_reference"):
        if name in value:
            normalized[name] = text_value(value[name], required=False)
    if normalized.get("rate_source_reference") and "rate_amount" not in normalized:
        raise ValidationError("A rate source must identify a supplied rate.")
    if "charges" in value:
        normalized["charges"] = validate_charges(value["charges"])
    return normalized


class ExactDecimalField(models.DecimalField):
    def to_python(self, value):
        if value is None:
            return None
        return exact_decimal(value)


class PriceRevision(models.Model):
    """Immutable draft evidence; no current-price selector or activation workflow."""

    class Mode(models.TextChoices):
        FIXED = "FIXED", "Fixed"
        WEIGHT_BASED = "WEIGHT_BASED", "Weight based"

    class Status(models.TextChoices):
        DRAFT = "DRAFT", "Draft"

    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    variant = models.ForeignKey(
        "catalog.ProductVariant", on_delete=models.PROTECT, related_name="price_revisions"
    )
    revision = models.PositiveIntegerField()
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.DRAFT)
    mode = models.CharField(max_length=20, choices=Mode.choices)
    currency = models.CharField(
        max_length=3,
        validators=[RegexValidator(r"\A[A-Z]{3}\Z", "Use an uppercase currency code.")],
    )
    fixed_amount = ExactDecimalField(
        max_digits=MAX_DIGITS, decimal_places=DECIMAL_PLACES, null=True, blank=True
    )
    weight_value = ExactDecimalField(
        max_digits=MAX_DIGITS, decimal_places=DECIMAL_PLACES, null=True, blank=True
    )
    weight_unit = models.CharField(max_length=40, blank=True)
    rate_amount = ExactDecimalField(
        max_digits=MAX_DIGITS, decimal_places=DECIMAL_PLACES, null=True, blank=True
    )
    rate_unit = models.CharField(max_length=40, blank=True)
    rate_source_reference = models.CharField(max_length=500, blank=True)
    formula_reference = models.CharField(max_length=500, blank=True)
    charges = models.JSONField(default=list, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, editable=False)

    objects = ImmutableQuerySet.as_manager()

    class Meta:
        app_label = "catalog"
        base_manager_name = "objects"
        ordering = ["variant", "-revision"]
        constraints = [
            models.UniqueConstraint(fields=["variant", "revision"], name="price_revision_unique"),
            models.CheckConstraint(condition=Q(revision__gte=1), name="price_revision_positive"),
            models.CheckConstraint(condition=Q(status="DRAFT"), name="price_revision_draft_only"),
            models.CheckConstraint(
                condition=Q(mode__in=["FIXED", "WEIGHT_BASED"]), name="price_revision_mode_valid"
            ),
            models.CheckConstraint(
                condition=Q(fixed_amount__isnull=True) | Q(fixed_amount__gte=0),
                name="price_fixed_amount_nonnegative",
            ),
            models.CheckConstraint(
                condition=Q(weight_value__isnull=True) | Q(weight_value__gt=0),
                name="price_weight_positive",
            ),
            models.CheckConstraint(
                condition=Q(rate_amount__isnull=True) | Q(rate_amount__gte=0),
                name="price_rate_nonnegative",
            ),
            models.CheckConstraint(
                condition=Q(mode="FIXED") | Q(fixed_amount__isnull=True),
                name="price_weight_has_no_fixed_amount",
            ),
            models.CheckConstraint(
                condition=(Q(weight_value__isnull=True) & Q(weight_unit=""))
                | (Q(weight_value__isnull=False) & ~Q(weight_unit="")),
                name="price_weight_has_unit",
            ),
            models.CheckConstraint(
                condition=(Q(rate_amount__isnull=True) & Q(rate_unit=""))
                | (Q(rate_amount__isnull=False) & ~Q(rate_unit="")),
                name="price_rate_has_unit",
            ),
        ]

    def __str__(self):
        return f"{self.variant_id}@{self.revision} ({self.mode}, {self.status})"

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise TypeError("Price revisions are append-only; record a new revision.")
        from catalog.models import Product, ProductVariant

        database = kwargs.get("using") or router.db_for_write(type(self), instance=self)
        with transaction.atomic(using=database):
            if self.variant_id:
                variant = (
                    ProductVariant.objects.using(database)
                    .select_for_update()
                    .filter(pk=self.variant_id)
                    .first()
                )
                if variant is not None:
                    Product.objects.using(database).select_for_update().get(pk=variant.product_id)
            # Normalize Decimal charge inputs before JSONField's JSON validation.
            self.charges = validate_charges(self.charges)
            self.full_clean()
            kwargs["force_insert"] = True
            kwargs["using"] = database
            return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise TypeError("Price revisions are append-only; preserve recorded evidence.")

    def clean(self):
        super().clean()
        validate_currency(self.currency)
        if self.mode == self.Mode.WEIGHT_BASED and self.fixed_amount is not None:
            raise ValidationError({"fixed_amount": "A weight-based draft has no fixed price."})
        self.pricing_inputs()
        self.charges = validate_charges(self.charges)

    def pricing_inputs(self) -> dict:
        return validate_pricing_inputs(
            {
                "weight_value": self.weight_value,
                "weight_unit": self.weight_unit,
                "rate_amount": self.rate_amount,
                "rate_unit": self.rate_unit,
                "rate_source_reference": self.rate_source_reference,
                "formula_reference": self.formula_reference,
                "charges": self.charges,
            }
        )
