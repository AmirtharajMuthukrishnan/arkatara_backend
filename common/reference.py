"""Validated reference writes; these primitives grant no operating approval."""

import uuid

from django.core.exceptions import ValidationError
from django.db import models, router, transaction

from common.models import TimeStampedModel


class ReferenceStatus(models.TextChoices):
    DRAFT = "DRAFT", "Draft"
    ACTIVE = "ACTIVE", "Active"
    COMING_SOON = "COMING_SOON", "Coming soon"
    INACTIVE = "INACTIVE", "Inactive"


class ValidatedReferenceQuerySet(models.QuerySet):
    def update(self, **kwargs):
        raise TypeError("Use validated individual saves for reference and identity changes.")

    def bulk_create(self, *args, **kwargs):
        raise TypeError("Bulk writes bypass reference validation and are not supported.")

    def bulk_update(self, *args, **kwargs):
        raise TypeError("Bulk writes bypass reference validation and are not supported.")


class ValidatedReferenceModel(TimeStampedModel):
    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)

    objects = ValidatedReferenceQuerySet.as_manager()

    # Concrete classes include relation attnames (for example, market_id) here.
    immutable_fields = ("public_id",)

    class Meta:
        abstract = True
        base_manager_name = "objects"

    def validate_existing_identity(self, previous) -> None:
        changed = [
            field
            for field in self.immutable_fields
            if getattr(self, field) != getattr(previous, field)
        ]
        if changed:
            raise ValidationError(
                {
                    self._meta.get_field(field).name: "Recorded identity cannot be reassigned."
                    for field in changed
                }
            )

    def lock_related_identity(self, database: str) -> None:
        """Concrete physical-identity writes may lock their parent definitions."""

    def save(self, *args, **kwargs):
        database = kwargs.get("using") or router.db_for_write(type(self), instance=self)
        with transaction.atomic(using=database):
            if not self._state.adding:
                previous = type(self).objects.using(database).select_for_update().get(pk=self.pk)
                self.validate_existing_identity(previous)
            self.lock_related_identity(database)
            self.full_clean()
            if self._state.adding:
                # An explicitly supplied existing primary key must not overwrite a row.
                kwargs["force_insert"] = True
            kwargs["using"] = database
            return super().save(*args, **kwargs)


class CodedReferenceModel(ValidatedReferenceModel):
    code = models.SlugField(max_length=64, unique=True)
    name = models.CharField(max_length=200)
    status = models.CharField(
        max_length=16, choices=ReferenceStatus.choices, default=ReferenceStatus.DRAFT
    )

    immutable_fields = ("public_id", "code")

    class Meta(ValidatedReferenceModel.Meta):
        abstract = True
        ordering = ["code"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(status__in=ReferenceStatus.values),
                name="%(app_label)s_%(class)s_status_valid",
            )
        ]

    def clean(self) -> None:
        super().clean()
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValidationError({"name": "Provide a nonempty display name."})

    def __str__(self) -> str:
        return self.name
