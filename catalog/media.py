"""Product-owned media identity, independent of storage hosts and publication."""

from django.core.exceptions import ValidationError
from django.db import models

from catalog.storage import KEY_PATTERN, validate_object_key
from common.reference import ValidatedReferenceModel, ValidatedReferenceQuerySet


class MediaQuerySet(ValidatedReferenceQuerySet):
    def delete(self):
        raise TypeError("Retire media through the attributed workflow; do not erase asset history.")


class ProductMedia(ValidatedReferenceModel):
    """Upload metadata grants no public access or live offering permission.

    Refs: BUSINESS_RULES.md R-11, R-30; DECISIONS.md BD-15.
    """

    class Role(models.TextChoices):
        MAIN = "MAIN", "Main image"
        ADDITIONAL = "ADDITIONAL", "Additional image"
        VIDEO = "VIDEO", "Video"
        THUMBNAIL = "THUMBNAIL", "Thumbnail"

    class Status(models.TextChoices):
        DRAFT = "DRAFT", "Draft"
        RETIRED = "RETIRED", "Retired"

    product = models.ForeignKey("catalog.Product", on_delete=models.PROTECT, related_name="media")
    source_media = models.ForeignKey(
        "self", on_delete=models.PROTECT, null=True, blank=True, related_name="thumbnails"
    )
    role = models.CharField(max_length=16, choices=Role.choices)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.DRAFT)
    sort_order = models.PositiveIntegerField(default=0)
    alt_text = models.CharField(max_length=500)
    storage_backend = models.CharField(max_length=50, editable=False)
    object_key = models.CharField(max_length=255, unique=True, editable=False)
    content_type = models.CharField(max_length=50, editable=False)
    byte_size = models.PositiveBigIntegerField(editable=False)
    sha256 = models.CharField(max_length=64, editable=False)

    objects = MediaQuerySet.as_manager()
    immutable_fields = (
        "public_id",
        "product_id",
        "source_media_id",
        "storage_backend",
        "object_key",
        "content_type",
        "byte_size",
        "sha256",
        "role",
    )

    class Meta(ValidatedReferenceModel.Meta):
        app_label = "catalog"
        ordering = ["product", "sort_order", "pk"]
        constraints = [
            models.UniqueConstraint(
                fields=["product"],
                condition=models.Q(role="MAIN", status="DRAFT"),
                name="product_one_current_main_media",
            ),
            models.CheckConstraint(
                condition=models.Q(status__in=["DRAFT", "RETIRED"]),
                name="product_media_status_valid",
            ),
            models.CheckConstraint(
                condition=models.Q(role__in=["MAIN", "ADDITIONAL", "VIDEO", "THUMBNAIL"]),
                name="product_media_role_valid",
            ),
            models.CheckConstraint(
                condition=models.Q(byte_size__gt=0), name="product_media_has_bytes"
            ),
            models.CheckConstraint(
                condition=(models.Q(role="THUMBNAIL", source_media__isnull=False))
                | (~models.Q(role="THUMBNAIL") & models.Q(source_media__isnull=True)),
                name="product_thumbnail_has_source",
            ),
        ]

    def __str__(self):
        return f"{self.product_id}: {self.role} ({self.public_id})"

    def save(self, *args, **kwargs):
        if not getattr(self, "_service_write", False):
            raise TypeError("Use the attributed media upload/metadata workflow.")
        try:
            return super().save(*args, **kwargs)
        finally:
            self._service_write = False

    def clean(self):
        super().clean()
        if not isinstance(self.alt_text, str) or not self.alt_text.strip():
            raise ValidationError({"alt_text": "Describe the product media."})
        if type(self.sort_order) is not int or self.sort_order < 0:
            raise ValidationError({"sort_order": "Use a nonnegative integer for ordering."})
        if self.content_type:
            expected = (
                {"video/mp4"} if self.role == self.Role.VIDEO else {"image/png", "image/jpeg"}
            )
            if self.content_type not in expected:
                raise ValidationError(
                    {"role": "Media role does not match the validated file type."}
                )
        if self.object_key:
            validate_object_key(self.object_key)
            match = KEY_PATTERN.fullmatch(self.object_key)
            if self.product_id and match.group("product") != str(self.product.public_id):
                raise ValidationError(
                    {"product": "Media assets cannot be reassigned between designs."}
                )
            if match.group("asset") != str(self.public_id):
                raise ValidationError(
                    {"object_key": "Media identity must match its object reference."}
                )
        if self.role == self.Role.THUMBNAIL:
            if not self.source_media_id:
                raise ValidationError({"source_media": "A thumbnail requires its source media."})
            if self.source_media_id == self.pk:
                raise ValidationError({"source_media": "A thumbnail cannot reference itself."})
            if self.source_media.product_id != self.product_id:
                raise ValidationError(
                    {"source_media": "Thumbnail source must belong to this design."}
                )
            if self.source_media.role == self.Role.THUMBNAIL:
                raise ValidationError(
                    {"source_media": "Use the original image or video as source."}
                )
        elif self.source_media_id:
            raise ValidationError(
                {"source_media": "Only thumbnails have a source media reference."}
            )

    def validate_existing_identity(self, previous):
        super().validate_existing_identity(previous)
        if previous.status == self.Status.RETIRED and self.status != previous.status:
            raise ValidationError({"status": "Retired media cannot be restored silently."})

    def delete(self, *args, **kwargs):
        raise TypeError("Retire media through the attributed workflow; do not erase asset history.")
