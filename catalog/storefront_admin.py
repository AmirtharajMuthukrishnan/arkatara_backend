"""Draft content authoring and explicit attributed publication actions."""

from django import forms
from django.contrib import admin, messages
from django.contrib.admin.helpers import ActionForm
from django.core.exceptions import ValidationError
from django.db import router, transaction

from catalog.storefront import change_publication
from catalog.storefront_models import StorefrontPage, lock_page_tree
from common.admin import AuditedReferenceAdmin


class PublicationActionForm(ActionForm):
    publication_reason = forms.CharField(label="Publication/withdrawal reason", required=True)


@admin.register(StorefrontPage)
class StorefrontPageAdmin(AuditedReferenceAdmin):
    list_display = ("title", "slug", "kind", "status", "is_featured", "sort_order")
    list_filter = ("kind", "status", "is_featured")
    search_fields = ("slug", "title", "description")
    list_select_related = ("product", "material", "category", "parent")
    autocomplete_fields = ("product", "material", "category", "parent")
    actions = ("publish_content", "withdraw_content")
    action_form = PublicationActionForm

    def get_readonly_fields(self, request, obj=None):
        fields = super().get_readonly_fields(request, obj)
        if obj and obj.status == StorefrontPage.Status.PUBLISHED:
            fields += tuple(field.name for field in obj._meta.concrete_fields)
        return tuple(dict.fromkeys(fields))

    def has_change_permission(self, request, obj=None):
        return super().has_change_permission(request, obj) and (
            obj is None or obj.status != StorefrontPage.Status.PUBLISHED
        )

    def has_publish_permission(self, request):
        return request.user.has_perm("catalog.publish_storefrontpage")

    def has_withdraw_permission(self, request):
        return request.user.has_perm("catalog.withdraw_storefrontpage")

    def get_object(self, request, object_id, from_field=None):
        database = router.db_for_write(self.model)
        if request.method == "POST" and transaction.get_connection(database).in_atomic_block:
            lock_page_tree(database)
        return super().get_object(request, object_id, from_field)

    def save_model(self, request, obj, form, change):
        with transaction.atomic():
            lock_page_tree()
            return super().save_model(request, obj, form, change)

    def _publication_action(self, request, queryset, publish):
        try:
            with transaction.atomic():
                lock_page_tree()
                for page in queryset.order_by("pk"):
                    change_publication(
                        actor=request.user,
                        page=page,
                        publish=publish,
                        reason=request.POST.get("publication_reason", ""),
                    )
        except ValidationError as error:
            self.message_user(request, "; ".join(error.messages), messages.ERROR)
        else:
            self.message_user(
                request, "The content publication decision was recorded.", messages.SUCCESS
            )

    @admin.action(description="Publish reviewed content", permissions=["publish"])
    def publish_content(self, request, queryset):
        self._publication_action(request, queryset, True)

    @admin.action(description="Withdraw published content", permissions=["withdraw"])
    def withdraw_content(self, request, queryset):
        self._publication_action(request, queryset, False)
