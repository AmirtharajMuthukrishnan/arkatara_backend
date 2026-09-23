from django.contrib import admin
from django.core.exceptions import PermissionDenied
from django.db import router, transaction
from django.db.models import Max

from compliance.audit import model_facts, record_model_change
from compliance.models import AuditEvent, BusinessConfiguration, LegalEntity


@admin.register(LegalEntity)
class LegalEntityAdmin(admin.ModelAdmin):
    list_display = ("code", "registered_name", "status", "effective_from", "effective_to")
    list_filter = ("status", "country_code")
    search_fields = ("code", "registered_name", "tax_registration_number")
    readonly_fields = ("public_id", "created_at", "updated_at")
    audited_fields = (
        "code",
        "registered_name",
        "display_name",
        "country_code",
        "tax_registration_number",
        "status",
        "effective_from",
        "effective_to",
    )

    def has_delete_permission(self, request, obj=None):
        return False

    def save_model(self, request, obj, form, change):
        database = router.db_for_write(LegalEntity)
        with transaction.atomic(using=database):
            before = {}
            if change:
                previous = LegalEntity.objects.using(database).select_for_update().get(pk=obj.pk)
                before = model_facts(previous, self.audited_fields)
            super().save_model(request, obj, form, change)
            record_model_change(
                actor=request.user,
                instance=obj,
                before=before,
                after=model_facts(obj, self.audited_fields),
            )


@admin.register(BusinessConfiguration)
class BusinessConfigurationAdmin(admin.ModelAdmin):
    list_display = ("namespace", "key", "scope_key", "version", "status")
    list_filter = ("namespace", "status")
    search_fields = ("namespace", "key", "scope_key")
    readonly_fields = (
        "public_id",
        "created_at",
        "updated_at",
        "scope_key",
        "version",
        "status",
        "approval_reference",
        "approval_recorded_by",
        "approved_at",
    )
    audited_fields = (
        "namespace",
        "key",
        "scope",
        "scope_key",
        "value",
        "version",
        "status",
        "effective_from",
        "effective_to",
    )

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def save_model(self, request, obj, form, change):
        if change or obj.status != BusinessConfiguration.Status.DRAFT:
            raise PermissionDenied("Admin can only create draft configuration revisions.")
        database = router.db_for_write(BusinessConfiguration)
        with transaction.atomic(using=database):
            # Validation canonicalizes scope before assigning its next revision number.
            obj.clean()
            revisions = BusinessConfiguration.objects.using(database).filter(
                namespace=obj.namespace, key=obj.key, scope_key=obj.scope_key
            )
            # Lock existing revisions; the unique constraint handles concurrent first inserts.
            list(revisions.select_for_update().order_by("pk").values_list("pk", flat=True))
            latest = revisions.aggregate(latest=Max("version"))["latest"] or 0
            obj.version = latest + 1
            super().save_model(request, obj, form, change)
            record_model_change(
                actor=request.user,
                instance=obj,
                before={},
                after=model_facts(obj, self.audited_fields),
                reason="Draft proposal only; commercial approval is not granted by this action.",
            )


@admin.register(AuditEvent)
class AuditEventAdmin(admin.ModelAdmin):
    list_display = ("occurred_at", "action", "resource_type", "resource_identifier", "actor")
    list_filter = ("action", "resource_type")
    search_fields = ("resource_identifier", "actor_label")
    readonly_fields = (
        "event_id",
        "actor",
        "actor_label",
        "action",
        "resource_type",
        "resource_identifier",
        "occurred_at",
        "reason",
        "changes",
        "metadata",
        "correlation_id",
    )

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
