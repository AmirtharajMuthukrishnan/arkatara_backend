"""Catalogue designs and selectable specifications are not physical inventory."""

from django.core.exceptions import ValidationError
from django.db import models

from common.reference import CodedReferenceModel, ReferenceStatus, ValidatedReferenceModel


class Material(CodedReferenceModel):
    pass


class Purity(ValidatedReferenceModel):
    material = models.ForeignKey(Material, on_delete=models.PROTECT, related_name="purities")
    code = models.SlugField(max_length=64)
    name = models.CharField(max_length=200)
    status = models.CharField(
        max_length=16, choices=ReferenceStatus.choices, default=ReferenceStatus.DRAFT
    )

    immutable_fields = ("public_id", "material_id", "code")

    class Meta(ValidatedReferenceModel.Meta):
        ordering = ["material__code", "code"]
        constraints = [
            models.UniqueConstraint(
                fields=["material", "code"], name="purity_material_code_unique"
            ),
            models.CheckConstraint(
                condition=models.Q(status__in=ReferenceStatus.values),
                name="catalog_purity_status_valid",
            ),
        ]

    def clean(self) -> None:
        super().clean()
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValidationError({"name": "Provide a nonempty display name."})

    def __str__(self) -> str:
        return self.name


class Category(CodedReferenceModel):
    """Trial eligibility is an approved scoped BusinessConfiguration, not a default."""


class Product(CodedReferenceModel):
    category = models.ForeignKey(Category, on_delete=models.PROTECT, related_name="products")
    description = models.TextField(blank=True)

    def validate_existing_identity(self, previous) -> None:
        super().validate_existing_identity(previous)
        if (
            self.category_id != previous.category_id
            and self.variants.filter(
                models.Q(inventory_units__isnull=False) | models.Q(price_revisions__isnull=False)
            ).exists()
        ):
            raise ValidationError(
                {"category": "A design with units or price records cannot be recategorized."}
            )


class ProductVariant(ValidatedReferenceModel):
    product = models.ForeignKey(Product, on_delete=models.PROTECT, related_name="variants")
    material = models.ForeignKey(Material, on_delete=models.PROTECT, related_name="variants")
    purity = models.ForeignKey(
        Purity, null=True, blank=True, on_delete=models.PROTECT, related_name="variants"
    )
    sku = models.CharField(max_length=100, unique=True)
    size_label = models.CharField(max_length=100, blank=True)
    specification = models.JSONField(default=dict, blank=True)
    status = models.CharField(
        max_length=16, choices=ReferenceStatus.choices, default=ReferenceStatus.DRAFT
    )

    immutable_fields = ("public_id", "sku")
    definition_fields = ("product_id", "material_id", "purity_id", "size_label", "specification")

    class Meta(ValidatedReferenceModel.Meta):
        ordering = ["sku"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(status__in=ReferenceStatus.values),
                name="catalog_variant_status_valid",
            ),
        ]

    def clean(self) -> None:
        super().clean()
        errors = {}
        if not isinstance(self.sku, str) or not self.sku.strip() or self.sku != self.sku.strip():
            errors["sku"] = "Use a nonempty SKU without outer whitespace."
        if not isinstance(self.specification, dict):
            errors["specification"] = "Variant specification must be a JSON object."
        if self.purity_id and self.material_id:
            material = (
                Purity.objects.filter(pk=self.purity_id)
                .values_list("material_id", flat=True)
                .first()
            )
            if material is not None and material != self.material_id:
                errors["purity"] = "Purity must belong to the variant material."
        if errors:
            raise ValidationError(errors)

    def validate_existing_identity(self, previous) -> None:
        super().validate_existing_identity(previous)
        changed = [
            field
            for field in self.definition_fields
            if getattr(self, field) != getattr(previous, field)
        ]
        if changed and (self.inventory_units.exists() or self.price_revisions.exists()):
            raise ValidationError(
                {
                    self._meta.get_field(field).name: (
                        "A specification with recorded units or prices cannot be redefined."
                    )
                    for field in changed
                }
            )

    def __str__(self) -> str:
        return self.sku


# Keep the pricing implementation separate while registering its Django model.
from catalog.pricing import PriceRevision  # noqa: E402,F401
