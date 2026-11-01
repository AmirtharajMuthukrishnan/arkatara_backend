"""Physical stock identity and append-only evidence for reviewed hub operations."""

import uuid
from contextlib import contextmanager
from contextvars import ContextVar

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone

from common.reference import ValidatedReferenceModel, ValidatedReferenceQuerySet
from compliance.models import ImmutableQuerySet

from .reservations import InventoryReservation  # noqa: F401

_operational_write = ContextVar("inventory_operational_write", default=False)


@contextmanager
def _recording_operation():
    """Internal service boundary; never expose this as an Admin/API bypass flag."""
    token = _operational_write.set(True)
    try:
        yield
    finally:
        _operational_write.reset(token)


class InventoryUnitQuerySet(ValidatedReferenceQuerySet):
    def delete(self):
        raise TypeError("Physical identities are retained; deletion is not a stock operation.")


class InventoryUnit(ValidatedReferenceModel):
    """Keep physical identity distinct from receipt, inspection and availability.

    Reviewed receiving/QC services own changes; reservation, dispatch, sale and
    customer-return workflows remain later dependencies.
    Refs: BUSINESS_RULES.md R-08, R-11, R-17; DECISIONS.md BD-09.
    """

    class Status(models.TextChoices):
        DRAFT = "DRAFT", "Draft / not operationally available"
        QC_PENDING = "QC_PENDING", "Awaiting inspection"
        AVAILABLE = "AVAILABLE", "Available at hub"
        OUT_FOR_DELIVERY = "OUT_FOR_DELIVERY", "With assigned agent"
        RETURNING_TO_HUB = "RETURNING_TO_HUB", "Returning to hub"
        SOLD = "SOLD", "Sold"
        QUARANTINED = "QUARANTINED", "Quarantined"
        RETIRED = "RETIRED", "Retired"

    class Custody(models.TextChoices):
        UNKNOWN = "UNKNOWN", "Not physically verified"
        HUB = "HUB", "Verified at hub"
        AGENT = "AGENT", "Assigned agent custody"
        CUSTOMER = "CUSTOMER", "Purchased customer custody"

    variant = models.ForeignKey(
        "catalog.ProductVariant", on_delete=models.PROTECT, related_name="inventory_units"
    )
    hub = models.ForeignKey("markets.Hub", on_delete=models.PROTECT, related_name="inventory_units")
    status = models.CharField(max_length=24, choices=Status.choices, default=Status.DRAFT)
    custody = models.CharField(max_length=16, choices=Custody.choices, default=Custody.UNKNOWN)

    objects = InventoryUnitQuerySet.as_manager()

    immutable_fields = ("public_id", "variant_id", "hub_id")

    class Meta(ValidatedReferenceModel.Meta):
        ordering = ["public_id"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(
                    status__in=[
                        "DRAFT",
                        "QC_PENDING",
                        "AVAILABLE",
                        "QUARANTINED",
                        "RETIRED",
                        "OUT_FOR_DELIVERY",
                        "RETURNING_TO_HUB",
                        "SOLD",
                    ]
                ),
                name="inventory_unit_status_valid",
            ),
            models.CheckConstraint(
                condition=models.Q(custody__in=["UNKNOWN", "HUB", "AGENT", "CUSTOMER"]),
                name="inventory_unit_custody_valid",
            ),
            models.CheckConstraint(
                condition=~models.Q(status="AVAILABLE") | models.Q(custody="HUB"),
                name="available_unit_at_hub",
            ),
        ]
        indexes = [
            models.Index(fields=["hub", "variant", "status"], name="inventory_unit_lookup_idx")
        ]
        permissions = [
            ("receive_inventoryunit", "Can record physical hub receipt"),
            ("inspect_inventoryunit", "Can record hub quality inspection"),
        ]

    def validate_existing_identity(self, previous) -> None:
        super().validate_existing_identity(previous)
        if not _operational_write.get() and (
            self.status != previous.status or self.custody != previous.custody
        ):
            raise ValidationError("Use the receiving or inspection workflow to change stock state.")

    def save(self, *args, **kwargs):
        if self._state.adding and (
            self.status != self.Status.DRAFT or self.custody != self.Custody.UNKNOWN
        ):
            raise ValidationError("New physical identities start as unverified drafts.")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise TypeError("Physical identities are retained; deletion is not a stock operation.")

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


class InventoryMovement(models.Model):
    """Preserve what was observed and which reviewed procedure authorized the action.

    Hub location is not stock ownership; corrections require new reviewed actions.
    Refs: BUSINESS_RULES.md R-15, R-28, R-30; DECISIONS.md BD-09, BD-11.
    """

    class Action(models.TextChoices):
        REGISTER = "REGISTER", "Draft registration"
        RECEIVE = "RECEIVE", "Physical hub receipt"
        QC_PASS = "QC_PASS", "Inspection passed"
        QC_FAIL = "QC_FAIL", "Inspection failed"

    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    unit = models.ForeignKey(InventoryUnit, on_delete=models.PROTECT, related_name="movements")
    action = models.CharField(max_length=16, choices=Action.choices)
    from_status = models.CharField(max_length=16, blank=True)
    to_status = models.CharField(max_length=16, choices=InventoryUnit.Status.choices)
    from_hub = models.ForeignKey(
        "markets.Hub",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="outgoing_inventory_movements",
    )
    to_hub = models.ForeignKey(
        "markets.Hub", on_delete=models.PROTECT, related_name="incoming_inventory_movements"
    )
    from_custody = models.CharField(max_length=16, choices=InventoryUnit.Custody.choices)
    to_custody = models.CharField(max_length=16, choices=InventoryUnit.Custody.choices)
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="inventory_movements"
    )
    actor_label = models.CharField(max_length=255, editable=False)
    occurred_at = models.DateTimeField()
    recorded_at = models.DateTimeField(auto_now_add=True, editable=False)
    reason = models.TextField()
    source_reference = models.CharField(max_length=500)
    idempotency_key = models.UUIDField(unique=True)
    request_fingerprint = models.CharField(max_length=64, editable=False)
    procedure_revision = models.ForeignKey(
        "compliance.BusinessConfiguration",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="inventory_movements",
    )
    procedure_snapshot = models.JSONField(default=dict, blank=True)

    objects = ImmutableQuerySet.as_manager()

    class Meta:
        ordering = ["recorded_at", "pk"]
        base_manager_name = "objects"
        indexes = [models.Index(fields=["unit", "occurred_at"], name="unit_movement_time_idx")]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(action__in=["REGISTER", "RECEIVE", "QC_PASS", "QC_FAIL"]),
                name="inventory_movement_action_valid",
            ),
        ]

    def __str__(self):
        return f"{self.unit.public_id}: {self.action}"

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise TypeError("Inventory movement evidence is append-only.")
        if not _operational_write.get():
            raise ValidationError("Inventory movements are written by reviewed stock workflows.")
        self.actor_label = f"staff:{self.actor.public_id}"
        self.full_clean()
        kwargs["force_insert"] = True
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise TypeError("Inventory movement evidence is append-only.")

    def clean(self):
        super().clean()
        if self.occurred_at is not None and timezone.is_naive(self.occurred_at):
            raise ValidationError({"occurred_at": "Use a timezone-aware observation time."})
        if not self.reason.strip() or not self.source_reference.strip():
            raise ValidationError("Record both a reason and supporting source reference.")
        if self.action != self.Action.REGISTER and not self.procedure_revision_id:
            raise ValidationError("Physical operations require their reviewed procedure revision.")
        if not isinstance(self.procedure_snapshot, dict):
            raise ValidationError({"procedure_snapshot": "Procedure evidence must be an object."})
