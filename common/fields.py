"""Exact integer minor-unit amounts; no implicit binary-float rounding."""

from django.core.exceptions import ValidationError
from django.db import models


class MinorUnitField(models.PositiveBigIntegerField):
    def to_python(self, value):
        if value is not None and type(value) is not int:
            raise ValidationError("Supply an integer amount in the currency's minor unit.")
        return super().to_python(value)

    def get_prep_value(self, value):
        # Bulk/queryset writes also pass here, even when full_clean is bypassed.
        return super().get_prep_value(self.to_python(value))


class SignedMinorUnitField(models.BigIntegerField):
    """Exact signed adjustment component; policy determines which adjustments apply."""

    def to_python(self, value):
        if value is not None and type(value) is not int:
            raise ValidationError("Supply an integer amount in the currency's minor unit.")
        return super().to_python(value)

    def get_prep_value(self, value):
        return super().get_prep_value(self.to_python(value))
