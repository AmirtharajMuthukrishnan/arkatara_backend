from django.contrib import admin

from common.admin import AuditedReferenceAdmin
from markets.models import Hub, Market, ServiceArea


@admin.register(Market)
class MarketAdmin(AuditedReferenceAdmin):
    list_display = ("code", "name", "country_code", "status")
    list_filter = ("country_code", "status")
    search_fields = ("code", "name")


@admin.register(Hub)
class HubAdmin(AuditedReferenceAdmin):
    list_display = ("code", "name", "market", "status")
    list_filter = ("market", "status")
    search_fields = ("code", "name", "market__name")
    autocomplete_fields = ("market",)
    list_select_related = ("market",)


@admin.register(ServiceArea)
class ServiceAreaAdmin(AuditedReferenceAdmin):
    list_display = (
        "market",
        "country_code",
        "postal_code",
        "status",
        "effective_from",
        "effective_to",
    )
    list_filter = ("market", "country_code", "status")
    search_fields = ("postal_code", "market__name", "approval_reference")
    autocomplete_fields = ("market",)
    list_select_related = ("market",)

    def get_readonly_fields(self, request, obj=None):
        fields = super().get_readonly_fields(request, obj)
        # Existing approved coverage is historical operational evidence, not a draft.
        # Refs: BUSINESS_RULES.md R-28; DECISIONS.md BD-13.
        if obj is not None and obj.status != ServiceArea.Status.DRAFT:
            fields += tuple(field.name for field in obj._meta.concrete_fields)
        return tuple(dict.fromkeys(fields))

    def has_change_permission(self, request, obj=None):
        return super().has_change_permission(request, obj) and (
            obj is None or obj.status == ServiceArea.Status.DRAFT
        )
