"""Back-office stock observations through reviewed services, never status dropdowns."""

from django.contrib import admin, messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import HttpResponseNotAllowed
from django.shortcuts import get_object_or_404, redirect
from django.template.response import TemplateResponse
from django.urls import path, reverse

from compliance.configuration import ConfigurationConflict, ConfigurationUnavailable
from inventory.forms import InspectUnitForm, InventoryActionForm, RegisterUnitForm
from inventory.models import InventoryMovement, InventoryUnit
from inventory.services import receive_unit, record_qc, register_unit


class InventoryMovementInline(admin.TabularInline):
    model = InventoryMovement
    extra = 0
    can_delete = False
    fields = (
        "action",
        "from_status",
        "to_status",
        "to_custody",
        "actor_label",
        "occurred_at",
        "source_reference",
    )
    readonly_fields = fields
    show_change_link = True

    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(InventoryUnit)
class InventoryUnitAdmin(admin.ModelAdmin):
    """Keep read access separate from receiving/QC permissions and physical evidence.

    Refs: BUSINESS_RULES.md R-18, R-17, R-30; DECISIONS.md BD-09.
    """

    change_form_template = "admin/inventory/inventoryunit/change_form.html"
    change_list_template = "admin/inventory/inventoryunit/change_list.html"
    list_display = ("public_id", "variant", "hub", "status", "custody", "updated_at")
    list_filter = (
        "hub__market",
        "hub",
        "variant__material",
        "variant__product__category",
        "status",
        "custody",
        "variant__size_label",
    )
    search_fields = ("public_id", "variant__sku", "variant__product__code", "hub__code")
    list_select_related = ("variant", "hub")
    readonly_fields = (
        "public_id",
        "variant",
        "hub",
        "status",
        "custody",
        "created_at",
        "updated_at",
    )
    inlines = (InventoryMovementInline,)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def get_urls(self):
        return [
            path(
                "register/",
                self.admin_site.admin_view(self.register_view),
                name="inventory_inventoryunit_register",
            ),
            path(
                "<uuid:public_id>/receive/",
                self.admin_site.admin_view(self.receive_view),
                name="inventory_inventoryunit_receive",
            ),
            path(
                "<uuid:public_id>/inspect/",
                self.admin_site.admin_view(self.inspect_view),
                name="inventory_inventoryunit_inspect",
            ),
            *super().get_urls(),
        ]

    def changelist_view(self, request, extra_context=None):
        context = {
            **(extra_context or {}),
            "can_register_unit": request.user.has_perm("inventory.add_inventoryunit"),
        }
        return super().changelist_view(request, extra_context=context)

    def change_view(self, request, object_id, form_url="", extra_context=None):
        context = {
            **(extra_context or {}),
            "can_receive_unit": request.user.has_perm("inventory.receive_inventoryunit"),
            "can_inspect_unit": request.user.has_perm("inventory.inspect_inventoryunit"),
        }
        return super().change_view(request, object_id, form_url, extra_context=context)

    def _action_view(self, request, *, action, public_id=None):
        if request.method not in ("GET", "POST"):
            return HttpResponseNotAllowed(["GET", "POST"])
        permissions = {
            "register": "add_inventoryunit",
            "receive": "receive_inventoryunit",
            "inspect": "inspect_inventoryunit",
        }
        if not request.user.has_perm(f"inventory.{permissions[action]}"):
            raise PermissionDenied("This staff account cannot perform the inventory action.")
        unit = get_object_or_404(InventoryUnit, public_id=public_id) if public_id else None
        form_class = {
            "register": RegisterUnitForm,
            "receive": InventoryActionForm,
            "inspect": InspectUnitForm,
        }[action]
        form = form_class(request.POST if request.method == "POST" else None)
        if request.method == "POST" and form.is_valid():
            values = dict(form.cleaned_data)
            try:
                if action == "register":
                    unit = register_unit(actor=request.user, **values)
                elif action == "receive":
                    receive_unit(unit=unit, actor=request.user, **values)
                else:
                    passed = values.pop("outcome") == "PASS"
                    record_qc(unit=unit, passed=passed, actor=request.user, **values)
            except ValidationError as error:
                form.add_error(None, " ".join(error.messages))
            except (ConfigurationUnavailable, ConfigurationConflict) as error:
                form.add_error(None, str(error))
            else:
                self.message_user(request, "Inventory observation recorded.", messages.SUCCESS)
                destination = (
                    reverse("admin:inventory_inventoryunit_change", args=[unit.pk])
                    if self.has_view_permission(request, unit)
                    else reverse("admin:index")
                )
                return redirect(destination)
        titles = {
            "register": "Register draft physical unit",
            "receive": "Record physical hub receipt",
            "inspect": "Record quality inspection",
        }
        return TemplateResponse(
            request,
            "admin/inventory/inventoryunit/action.html",
            {
                **self.admin_site.each_context(request),
                "opts": self.model._meta,
                "title": titles[action],
                "form": form,
                "unit": unit,
                "action": action,
            },
        )

    def register_view(self, request):
        return self._action_view(request, action="register")

    def receive_view(self, request, public_id):
        return self._action_view(request, action="receive", public_id=public_id)

    def inspect_view(self, request, public_id):
        return self._action_view(request, action="inspect", public_id=public_id)


@admin.register(InventoryMovement)
class InventoryMovementAdmin(admin.ModelAdmin):
    list_display = (
        "public_id",
        "unit",
        "action",
        "from_status",
        "to_status",
        "actor_label",
        "occurred_at",
    )
    list_filter = ("action", "to_hub__market", "to_hub", "to_status")
    search_fields = (
        "public_id",
        "unit__public_id",
        "unit__variant__sku",
        "source_reference",
        "actor_label",
    )
    readonly_fields = tuple(field.name for field in InventoryMovement._meta.fields)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
