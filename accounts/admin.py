from django.contrib import admin
from django.contrib.auth.admin import UserAdmin

from accounts.models import StaffUser


@admin.register(StaffUser)
class StaffUserAdmin(UserAdmin):
    readonly_fields = (*UserAdmin.readonly_fields, "public_id")
