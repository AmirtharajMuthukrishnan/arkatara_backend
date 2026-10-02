"""Admin upload form; its caller must own the outer upload transaction."""

from django import forms

from catalog.media import ProductMedia
from catalog.media_services import upload_product_media, validate_media_upload
from catalog.models import Product


class ProductMediaUploadForm(forms.Form):
    product = forms.ModelChoiceField(queryset=Product.objects.all())
    role = forms.ChoiceField(choices=ProductMedia.Role.choices)
    source_media = forms.ModelChoiceField(queryset=ProductMedia.objects.all(), required=False)
    file = forms.FileField()
    alt_text = forms.CharField(max_length=500)
    sort_order = forms.IntegerField(min_value=0, initial=0)
    reason = forms.CharField(widget=forms.Textarea)

    def clean_file(self):
        uploaded = self.cleaned_data["file"]
        validate_media_upload(uploaded)
        return uploaded

    def clean(self):
        cleaned = super().clean()
        product = cleaned.get("product")
        source = cleaned.get("source_media")
        role = cleaned.get("role")
        if role == ProductMedia.Role.THUMBNAIL and not source:
            self.add_error("source_media", "A thumbnail requires its source media.")
        elif source and (not product or source.product_id != product.pk):
            self.add_error("source_media", "Thumbnail source must belong to this design.")
        elif source and role != ProductMedia.Role.THUMBNAIL:
            self.add_error("source_media", "Only thumbnails use a source media reference.")
        return cleaned

    def save_for_actor(self, actor):
        if not self.is_valid():
            raise ValueError("Validate the upload form before saving.")
        return upload_product_media(
            actor=actor,
            product=self.cleaned_data["product"],
            uploaded_file=self.cleaned_data["file"],
            role=self.cleaned_data["role"],
            alt_text=self.cleaned_data["alt_text"],
            sort_order=self.cleaned_data["sort_order"],
            source_media=self.cleaned_data["source_media"],
            reason=self.cleaned_data["reason"],
        )
