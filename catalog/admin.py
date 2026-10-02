from django.contrib import admin

# Register the service-backed upload/import screens alongside catalogue browsing.
from catalog import onboarding_admin, storefront_admin  # noqa: F401
from catalog.models import Category, Material, PriceRevision, Product, ProductVariant, Purity
from common.admin import AuditedReferenceAdmin


@admin.register(Material)
class MaterialAdmin(AuditedReferenceAdmin):
    list_display = ("code", "name", "status")
    list_filter = ("status",)
    search_fields = ("code", "name")


@admin.register(Purity)
class PurityAdmin(AuditedReferenceAdmin):
    list_display = ("code", "name", "material", "status")
    list_filter = ("material", "status")
    search_fields = ("code", "name", "material__name")
    autocomplete_fields = ("material",)
    list_select_related = ("material",)


@admin.register(Category)
class CategoryAdmin(AuditedReferenceAdmin):
    list_display = ("code", "name", "status")
    list_filter = ("status",)
    search_fields = ("code", "name")


@admin.register(Product)
class ProductAdmin(AuditedReferenceAdmin):
    list_display = ("code", "name", "category", "status")
    list_filter = ("category", "status")
    search_fields = ("code", "name", "description")
    autocomplete_fields = ("category",)
    list_select_related = ("category",)


@admin.register(ProductVariant)
class ProductVariantAdmin(AuditedReferenceAdmin):
    list_display = ("sku", "product", "material", "purity", "size_label", "status")
    list_filter = ("product__category", "material", "purity", "status", "size_label")
    search_fields = ("sku", "product__code", "product__name")
    autocomplete_fields = ("product", "material", "purity")
    list_select_related = ("product", "material", "purity")


@admin.register(PriceRevision)
class PriceRevisionAdmin(admin.ModelAdmin):
    """Expose prior proposals for review without a price override or activation UI.

    Refs: BUSINESS_RULES.md R-19, R-28; DECISIONS.md BD-04, CA-02.
    """

    list_display = ("variant", "revision", "mode", "currency", "status", "created_at")
    list_filter = ("mode", "currency", "status", "variant__material")
    search_fields = ("variant__sku",)
    list_select_related = ("variant",)
    actions = None

    def get_readonly_fields(self, request, obj=None):
        return tuple(field.name for field in self.model._meta.concrete_fields)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
