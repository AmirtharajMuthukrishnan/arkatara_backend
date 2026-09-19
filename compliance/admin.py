from django.contrib import admin

from compliance.models import AuditEvent, BusinessConfiguration, LegalEntity


@admin.register(LegalEntity)
class LegalEntityAdmin(admin.ModelAdmin):
    list_display = ("code", "registered_name", "status", "effective_from", "effective_to")
    list_filter = ("status", "country_code")
    search_fields = ("code", "registered_name", "tax_registration_number")
    readonly_fields = ("public_id", "created_at", "updated_at")


@admin.register(BusinessConfiguration)
class BusinessConfigurationAdmin(admin.ModelAdmin):
    list_display = ("namespace", "key", "scope_key", "version", "status")
    list_filter = ("namespace", "status")
    search_fields = ("namespace", "key", "scope_key")
    readonly_fields = ("public_id", "created_at", "updated_at")


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
