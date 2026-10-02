"""Action evidence forms; operational state is never a writable form field."""

import uuid

from django import forms
from django.utils import timezone

from catalog.models import ProductVariant
from markets.models import Hub


class InventoryActionForm(forms.Form):
    idempotency_key = forms.UUIDField(initial=uuid.uuid4, widget=forms.HiddenInput)
    reason = forms.CharField(max_length=4000, widget=forms.Textarea(attrs={"rows": 3}))
    source_reference = forms.CharField(max_length=500, label="Observation / source reference")
    occurred_at = forms.DateTimeField(initial=timezone.now, label="Observed at")


class RegisterUnitForm(InventoryActionForm):
    variant = forms.ModelChoiceField(queryset=ProductVariant.objects.all())
    hub = forms.ModelChoiceField(queryset=Hub.objects.all())


class InspectUnitForm(InventoryActionForm):
    outcome = forms.ChoiceField(choices=[("PASS", "Passed"), ("FAIL", "Failed / quarantine")])
