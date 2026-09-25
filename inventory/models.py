"""Physical piece identity only; stock operations are introduced in Task 3."""

from django.db import models

from common.reference import ValidatedReferenceModel


class InventoryUnit(ValidatedReferenceModel):
    class Status(models.TextChoices):
        DRAFT = "DRAFT", "Draft / not operationally available"

    variant = models.ForeignKey(
        "catalog.ProductVariant", on_delete=models.PROTECT, related_name="inventory_units"
    )
    hub = models.ForeignKey("markets.Hub", on_delete=models.PROTECT, related_name="inventory_units")
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.DRAFT)

    immutable_fields = ("public_id", "variant_id", "hub_id")

    class Meta(ValidatedReferenceModel.Meta):
        ordering = ["public_id"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(status="DRAFT"), name="inventory_unit_foundation_status_valid"
            ),
        ]
        indexes = [
            models.Index(fields=["hub", "variant", "status"], name="inventory_unit_lookup_idx")
        ]

    def lock_related_identity(self, database: str) -> None:
        # Share the variant/product locks used by definition edits, so a concurrent
        # first unit cannot be inserted after an edit checked for existing units.
        from catalog.models import Product, ProductVariant

        if self.variant_id:
            variant = (
                ProductVariant.objects.using(database)
                .select_for_update()
                .filter(pk=self.variant_id)
                .first()
            )
            if variant is not None:
                Product.objects.using(database).select_for_update().get(pk=variant.product_id)

    def __str__(self) -> str:
        return str(self.public_id)
