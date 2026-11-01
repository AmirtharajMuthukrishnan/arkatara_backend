"""Audited draft authoring shared by reference-data Admin screens."""

from django import forms
from django.contrib import admin
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import router, transaction

from compliance.audit import model_facts, record_model_change


class ReferenceAdminForm(forms.ModelForm):
    revision = forms.DateTimeField(required=False, widget=forms.HiddenInput)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance.pk:
            self.initial["revision"] = self.instance.updated_at

    def clean(self):
        cleaned = super().clean()
        if self.instance.pk and cleaned.get("revision") != self.instance.updated_at:
            raise ValidationError("This record changed. Reload it before submitting your edits.")
        return cleaned

    def _post_clean(self):
        super()._post_clean()
        if self.instance.pk and not self.instance._state.adding:
            previous = type(self.instance).objects.get(pk=self.instance.pk)
            try:
                self.instance.validate_existing_identity(previous)
            except ValidationError as error:
                self._update_errors(error)


class AuditedReferenceAdmin(admin.ModelAdmin):
    """Separate catalogue maintenance from approval or physical state transitions.

    Refs: BUSINESS_RULES.md R-18, R-28, R-30; DECISIONS.md BD-12, BD-13.
    """

    form = ReferenceAdminForm
    actions = None
    readonly_fields = ("public_id", "created_at", "updated_at", "status")

    def get_readonly_fields(self, request, obj=None):
        fields = list(super().get_readonly_fields(request, obj))
        if obj is not None:
            fields.extend(obj._meta.get_field(field).name for field in obj.immutable_fields)
        return tuple(dict.fromkeys(fields))

    def has_delete_permission(self, request, obj=None):
        return False

    def get_object(self, request, object_id, from_field=None):
        obj = super().get_object(request, object_id, from_field)
        if obj is not None and request.method == "POST":
            # Django wraps change-form POSTs in an atomic transaction. Refresh
            # under its row lock before comparing the submitted revision, so an
            # older form cannot overwrite another administrator's newer edits.
            database = router.db_for_write(type(obj), instance=obj)
            if transaction.get_connection(database).in_atomic_block:
                from common.serialization import lock_integrity_writes

                # Checkout guards acquire this latch before catalogue row locks.
                # Admin must use that order during its initial POST refresh too.
                # Refs: BUSINESS_RULES.md R-14, R-40; DECISIONS.md D-27.
                lock_integrity_writes(database)
                return type(obj).objects.using(database).select_for_update().get(pk=obj.pk)
        return obj

    def delete_model(self, request, obj):
        raise PermissionDenied("Reference records must be retained.")

    def delete_queryset(self, request, queryset):
        raise PermissionDenied("Reference records must be retained.")

    def save_model(self, request, obj, form, change):
        permitted = (
            self.has_change_permission(request, obj) if change else self.has_add_permission(request)
        )
        if not request.user.is_active or not request.user.is_staff or not permitted:
            raise PermissionDenied("Catalogue maintenance permission is required.")
        database = router.db_for_write(type(obj), instance=obj)
        fields = tuple(
            field.attname
            for field in obj._meta.concrete_fields
            if not field.primary_key and field.name not in {"created_at", "updated_at"}
        )
        with transaction.atomic(using=database):
            from common.serialization import lock_integrity_writes

            lock_integrity_writes(database)
            before = {}
            if change:
                previous = type(obj).objects.using(database).select_for_update().get(pk=obj.pk)
                if obj.status != previous.status:
                    raise PermissionDenied("Activation requires a separately reviewed workflow.")
                before = model_facts(previous, fields)
            elif obj.status != "DRAFT":
                raise PermissionDenied("New reference records must remain drafts.")
            super().save_model(request, obj, form, change)
            record_model_change(
                actor=request.user,
                instance=obj,
                before=before,
                after=model_facts(obj, fields),
                reason="Audited reference maintenance; operating approval is unchanged.",
            )
