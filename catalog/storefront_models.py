"""Stable public page identity, separate from offering activation and private assets."""

from contextlib import contextmanager
from contextvars import ContextVar

from django.core.exceptions import ValidationError
from django.core.validators import RegexValidator
from django.db import connections, models, router, transaction
from django.db.models.functions import Lower

from common.reference import ValidatedReferenceModel, ValidatedReferenceQuerySet

_publication_write = ContextVar("storefront_publication_write", default=False)


def lock_page_tree(database="default"):
    # Serialize hierarchy edits before taking page row locks, preventing concurrent cycles.
    with connections[database].cursor() as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock(%s)", [674805010404])


@contextmanager
def publication_write():
    token = _publication_write.set(True)
    try:
        yield
    finally:
        _publication_write.reset(token)


class StorefrontQuerySet(ValidatedReferenceQuerySet):
    def delete(self):
        raise TypeError("Withdraw storefront pages; preserve their canonical identity.")


class StorefrontPage(ValidatedReferenceModel):
    """Publishing content never approves prices, media delivery, eligibility or booking.

    Stable routes survive label changes and future market/material expansion.
    Refs: BUSINESS_RULES.md R-07, R-09, R-11, R-30; DECISIONS.md D-20.
    """

    class Kind(models.TextChoices):
        MATERIAL = "MATERIAL", "Material collection"
        CATEGORY = "CATEGORY", "Category collection"
        PRODUCT = "PRODUCT", "Product"

    class Status(models.TextChoices):
        DRAFT = "DRAFT", "Draft"
        PUBLISHED = "PUBLISHED", "Published"
        WITHDRAWN = "WITHDRAWN", "Withdrawn"

    kind = models.CharField(max_length=16, choices=Kind.choices)
    material = models.OneToOneField(
        "catalog.Material",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="storefront_page",
    )
    category = models.OneToOneField(
        "catalog.Category",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="storefront_page",
    )
    product = models.OneToOneField(
        "catalog.Product",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="storefront_page",
    )
    parent = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.PROTECT, related_name="children"
    )
    slug = models.SlugField(
        max_length=140,
        unique=True,
        validators=[
            RegexValidator(
                r"\A[a-z0-9]+(?:-[a-z0-9]+)*\Z", "Use lowercase words separated by hyphens."
            )
        ],
    )
    title = models.CharField(max_length=200)
    description = models.TextField()
    seo_title = models.CharField(max_length=200, blank=True)
    seo_description = models.TextField(blank=True)
    sort_order = models.PositiveIntegerField(default=0)
    is_featured = models.BooleanField(default=False)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.DRAFT)

    objects = StorefrontQuerySet.as_manager()
    immutable_fields = ("public_id", "slug", "kind", "material_id", "category_id", "product_id")

    class Meta(ValidatedReferenceModel.Meta):
        app_label = "catalog"
        ordering = ["sort_order", "slug"]
        permissions = [
            ("publish_storefrontpage", "Can publish reviewed storefront content"),
            ("withdraw_storefrontpage", "Can withdraw storefront content"),
        ]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(status__in=["DRAFT", "PUBLISHED", "WITHDRAWN"]),
                name="storefront_status_valid",
            ),
            models.CheckConstraint(
                condition=models.Q(slug=Lower("slug")), name="storefront_slug_lowercase"
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(
                        kind="MATERIAL",
                        material__isnull=False,
                        category__isnull=True,
                        product__isnull=True,
                    )
                    | models.Q(
                        kind="CATEGORY",
                        category__isnull=False,
                        material__isnull=True,
                        product__isnull=True,
                    )
                    | models.Q(
                        kind="PRODUCT",
                        product__isnull=False,
                        material__isnull=True,
                        category__isnull=True,
                    )
                ),
                name="storefront_exactly_one_subject",
            ),
        ]

    @property
    def path(self):
        prefix = "products" if self.kind == self.Kind.PRODUCT else "collections"
        return f"/{prefix}/{self.slug}"

    def __str__(self):
        return self.title

    def clean(self):
        super().clean()
        expected = {
            self.Kind.MATERIAL: (True, False, False),
            self.Kind.CATEGORY: (False, True, False),
            self.Kind.PRODUCT: (False, False, True),
        }.get(self.kind)
        actual = (bool(self.material_id), bool(self.category_id), bool(self.product_id))
        if actual != expected:
            raise ValidationError("Link exactly the subject appropriate to this page kind.")
        for field in ("title", "description"):
            if not isinstance(getattr(self, field), str) or not getattr(self, field).strip():
                raise ValidationError({field: "Supply reviewed, nonblank page content."})
        visited = {self.pk} if self.pk is not None else set()
        current = self.parent_id
        while current is not None:
            if current in visited:
                raise ValidationError({"parent": "Page hierarchy cannot contain a cycle."})
            visited.add(current)
            ancestor = type(self).objects.filter(pk=current).only("parent_id", "kind").first()
            if ancestor is None:
                break
            if ancestor.kind == self.Kind.PRODUCT:
                raise ValidationError({"parent": "Only collection pages may be parents."})
            current = ancestor.parent_id

    def validate_existing_identity(self, previous):
        super().validate_existing_identity(previous)
        if not _publication_write.get():
            if self.status != previous.status:
                raise ValidationError({"status": "Use the publish or withdraw workflow."})
            if previous.status == self.Status.PUBLISHED:
                raise ValidationError("Withdraw published content before editing it.")

    def save(self, *args, **kwargs):
        if self._state.adding and self.status != self.Status.DRAFT:
            raise ValidationError({"status": "New storefront content must start as a draft."})
        database = kwargs.get("using") or router.db_for_write(type(self), instance=self)
        with transaction.atomic(using=database):
            lock_page_tree(database)
            return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise TypeError("Withdraw storefront pages; preserve their canonical identity.")
