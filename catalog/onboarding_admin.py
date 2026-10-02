"""Restricted onboarding screens; all domain mutations go through audited services."""

import uuid

from django import forms
from django.contrib import admin, messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError
from django.http import Http404, HttpResponse, HttpResponseRedirect
from django.shortcuts import get_object_or_404
from django.template.response import TemplateResponse
from django.urls import path, reverse
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import csrf_protect

from catalog.imports import HEADERS, MAX_CSV_BYTES, CSVImportBatch, apply_import, preview_import
from catalog.media import ProductMedia
from catalog.media_forms import ProductMediaUploadForm
from catalog.media_services import media_download_response, update_product_media_metadata


class CSVImportForm(forms.Form):
    kind = forms.ChoiceField(choices=[(key, key.title()) for key in HEADERS])
    file = forms.FileField(help_text="UTF-8 CSV, at most 500 rows and 1,000,000 bytes.")
    idempotency_key = forms.UUIDField(
        initial=uuid.uuid4, help_text="Retain this ID for retries of the same import."
    )
    reason = forms.CharField(widget=forms.Textarea)

    def clean_file(self):
        uploaded = self.cleaned_data["file"]
        if uploaded.size > MAX_CSV_BYTES:
            raise forms.ValidationError("CSV exceeds the import size limit.")
        return uploaded


@admin.register(CSVImportBatch)
class CSVImportBatchAdmin(admin.ModelAdmin):
    list_display = ("public_id", "kind", "row_count", "actor", "created_at")
    list_filter = ("kind", "created_at")
    search_fields = ("public_id", "idempotency_key", "actor__username")
    list_select_related = ("actor",)
    actions = None

    def get_readonly_fields(self, request, obj=None):
        return tuple(field.name for field in self.model._meta.concrete_fields)

    def has_add_permission(self, request):
        return request.user.has_perm("catalog.import_catalogue")

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    @method_decorator(csrf_protect)
    def add_view(self, request, form_url="", extra_context=None):
        if not self.has_add_permission(request):
            raise PermissionDenied
        form = CSVImportForm(request.POST or None, request.FILES or None)
        preview = None
        if request.method == "POST" and form.is_valid():
            values = form.cleaned_data
            content = values["file"].read(MAX_CSV_BYTES + 1)
            try:
                if request.POST.get("operation") == "apply":
                    batch = apply_import(
                        actor=request.user,
                        kind=values["kind"],
                        content=content,
                        idempotency_key=values["idempotency_key"],
                        reason=values["reason"],
                    )
                    self.message_user(
                        request,
                        f"Recorded draft import of {batch.row_count} rows.",
                        messages.SUCCESS,
                    )
                    destination = (
                        "admin:catalog_csvimportbatch_changelist"
                        if self.has_view_permission(request)
                        else "admin:index"
                    )
                    return HttpResponseRedirect(reverse(destination))
                preview = preview_import(actor=request.user, kind=values["kind"], content=content)
            except ValidationError as error:
                form.add_error(None, error)
            except IntegrityError:
                form.add_error(
                    None,
                    "A reference changed during import. No rows were committed; preview again.",
                )
        return TemplateResponse(
            request,
            "admin/catalog/csv_import.html",
            {
                **self.admin_site.each_context(request),
                "opts": self.model._meta,
                "title": "Onboard draft catalogue or inventory",
                "form": form,
                "preview": preview,
                "headers": HEADERS,
            },
        )


class MediaMetadataForm(forms.Form):
    alt_text = forms.CharField(max_length=500)
    sort_order = forms.IntegerField(min_value=0)
    status = forms.ChoiceField(choices=ProductMedia.Status.choices)
    reason = forms.CharField(widget=forms.Textarea)


@admin.register(ProductMedia)
class ProductMediaAdmin(admin.ModelAdmin):
    list_display = ("product", "role", "status", "sort_order", "content_type", "byte_size")
    list_filter = ("role", "status", "content_type", "product__category")
    search_fields = ("product__code", "product__name", "alt_text")
    list_select_related = ("product",)
    change_form_template = "admin/catalog/product_media_change.html"
    actions = None

    def get_readonly_fields(self, request, obj=None):
        return tuple(field.name for field in self.model._meta.concrete_fields)

    def has_add_permission(self, request):
        return super().has_add_permission(request) and request.user.has_perm("catalog.view_product")

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def get_urls(self):
        return [
            path(
                "<int:object_id>/download/",
                self.admin_site.admin_view(self.download_view),
                name="catalog_productmedia_download",
            ),
            path(
                "<int:object_id>/metadata/",
                self.admin_site.admin_view(self.metadata_view),
                name="catalog_productmedia_metadata",
            ),
        ] + super().get_urls()

    def download_view(self, request, object_id):
        try:
            return media_download_response(
                actor=request.user, media=get_object_or_404(ProductMedia, pk=object_id)
            )
        except FileNotFoundError as error:
            raise Http404("The recorded asset is unavailable.") from error
        except ValidationError:
            return HttpResponse(
                "Asset storage is unavailable.", status=503, content_type="text/plain"
            )

    @method_decorator(csrf_protect)
    def add_view(self, request, form_url="", extra_context=None):
        if not self.has_add_permission(request):
            raise PermissionDenied
        form = ProductMediaUploadForm(request.POST or None, request.FILES or None)
        if request.method == "POST" and form.is_valid():
            try:
                media = form.save_for_actor(request.user)
                self.message_user(request, "Private draft media uploaded.", messages.SUCCESS)
                destination = (
                    reverse("admin:catalog_productmedia_change", args=[media.pk])
                    if self.has_view_permission(request, media)
                    else reverse("admin:index")
                )
                return HttpResponseRedirect(destination)
            except ValidationError as error:
                form.add_error(None, error)
            except IntegrityError:
                form.add_error(
                    None, "A media reference changed during upload. Please refresh and retry."
                )
        return TemplateResponse(
            request,
            "admin/catalog/media_form.html",
            {
                **self.admin_site.each_context(request),
                "opts": self.model._meta,
                "title": "Upload private product media",
                "form": form,
                "submit_label": "Upload draft media",
            },
        )

    @method_decorator(csrf_protect)
    def metadata_view(self, request, object_id):
        if not request.user.has_perms(["catalog.change_productmedia", "catalog.view_product"]):
            raise PermissionDenied
        media = get_object_or_404(ProductMedia, pk=object_id)
        form = MediaMetadataForm(
            request.POST or None,
            initial={
                "alt_text": media.alt_text,
                "sort_order": media.sort_order,
                "status": media.status,
            },
        )
        if request.method == "POST" and form.is_valid():
            try:
                update_product_media_metadata(actor=request.user, media=media, **form.cleaned_data)
                return HttpResponseRedirect(
                    reverse("admin:catalog_productmedia_change", args=[media.pk])
                )
            except ValidationError as error:
                form.add_error(None, error)
        return TemplateResponse(
            request,
            "admin/catalog/media_form.html",
            {
                **self.admin_site.each_context(request),
                "opts": self.model._meta,
                "title": "Update media description",
                "form": form,
                "submit_label": "Save metadata",
            },
        )
