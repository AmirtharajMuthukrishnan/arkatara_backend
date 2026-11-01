from django.contrib import admin
from django.contrib.auth.admin import GroupAdmin, UserAdmin
from django.contrib.auth.models import Group
from django.core.exceptions import PermissionDenied
from django.db import router, transaction

from accounts.models import StaffUser
from common.serialization import lock_integrity_writes
from compliance.models import AuditEvent


def authority_facts(obj):
    if isinstance(obj, StaffUser):
        return {
            "active": obj.is_active,
            "staff": obj.is_staff,
            "superuser": obj.is_superuser,
            "groups": list(obj.groups.order_by("pk").values_list("pk", flat=True)),
            "permissions": list(obj.user_permissions.order_by("pk").values_list("pk", flat=True)),
        }
    return {
        "name": obj.name,
        "permissions": list(obj.permissions.order_by("pk").values_list("pk", flat=True)),
    }


class TrustedAdministratorOnly:
    """Delegated user/group permissions cannot grant administrative authority.

    Refs: BUSINESS_RULES.md R-30, R-33, R-38. MFA remains a separate live gate.
    """

    def has_module_permission(self, request):
        return request.user.is_active and request.user.is_staff and request.user.is_superuser

    def has_view_permission(self, request, obj=None):
        return self.has_module_permission(request)

    has_add_permission = has_view_permission
    has_change_permission = has_view_permission
    has_delete_permission = has_view_permission

    def save_model(self, request, obj, form, change):
        if not self.has_change_permission(request):
            raise PermissionDenied("Trusted administrator access required.")
        database = router.db_for_write(type(obj))
        with transaction.atomic(using=database):
            lock_integrity_writes(database)
            previous = (
                type(obj).objects.using(database).select_for_update().get(pk=obj.pk)
                if change
                else None
            )
            before = authority_facts(previous) if previous else {}
            super().save_model(request, obj, form, change)
            AuditEvent.objects.using(database).create(
                actor=request.user,
                action="staff_authority.saved",
                resource_type=obj._meta.label,
                resource_identifier=str(obj.pk),
                changes={"before": before, "after": authority_facts(obj)},
                reason="Trusted administrator changed staff authority",
            )

    def save_related(self, request, form, formsets, change):
        if not self.has_change_permission(request):
            raise PermissionDenied("Trusted administrator access required.")
        database = router.db_for_write(type(form.instance))
        with transaction.atomic(using=database):
            lock_integrity_writes(database)
            before = authority_facts(form.instance)
            super().save_related(request, form, formsets, change)
            AuditEvent.objects.using(database).create(
                actor=request.user,
                action="staff_authority.relations_saved",
                resource_type=form.instance._meta.label,
                resource_identifier=str(form.instance.pk),
                changes={"before": before, "after": authority_facts(form.instance)},
                reason="Trusted administrator saved permission/group relationships",
            )


@admin.register(StaffUser)
class StaffUserAdmin(TrustedAdministratorOnly, UserAdmin):
    readonly_fields = (*UserAdmin.readonly_fields, "public_id")

    # Preserve identities referenced by financial and assignment history.
    def has_delete_permission(self, request, obj=None):
        return False

    def user_change_password(self, request, id, form_url=""):
        database = router.db_for_write(self.model)
        with transaction.atomic(using=database):
            lock_integrity_writes(database)
            response = super().user_change_password(request, id, form_url)
            if request.method == "POST" and response.status_code == 302:
                AuditEvent.objects.using(database).create(
                    actor=request.user,
                    action="staff_authority.password_changed",
                    resource_type=self.model._meta.label,
                    resource_identifier=str(id),
                    reason="Trusted administrator changed staff credential",
                )
            return response


admin.site.unregister(Group)


@admin.register(Group)
class TrustedGroupAdmin(TrustedAdministratorOnly, GroupAdmin):
    def has_delete_permission(self, request, obj=None):
        return False
