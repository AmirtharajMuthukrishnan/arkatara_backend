"""Validated, retained records; business authorization belongs to explicit workflows."""

import uuid

from django.core.exceptions import ValidationError
from django.db import models, router, transaction

from common.reference import ValidatedReferenceModel, ValidatedReferenceQuerySet


class RetainedQuerySet(ValidatedReferenceQuerySet):
    def delete(self):
        raise TypeError("Recorded evidence must be retained.")


class RetainedModel(ValidatedReferenceModel):
    objects = RetainedQuerySet.as_manager()

    class Meta(ValidatedReferenceModel.Meta):
        abstract = True

    def delete(self, *args, **kwargs):
        raise TypeError("Recorded evidence must be retained.")


class AppendOnlyQuerySet(RetainedQuerySet):
    def update(self, **kwargs):
        raise TypeError("Recorded evidence is append-only.")


class AppendOnlyModel(models.Model):
    """Shared persistence guard for new evidence, with corresponding SQL migrations.

    A record is evidence, not permission or proof that an external event occurred.
    Refs: BUSINESS_RULES.md R-28, R-30; DECISIONS.md D-24.
    """

    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    recorded_at = models.DateTimeField(auto_now_add=True, editable=False)

    objects = AppendOnlyQuerySet.as_manager()

    class Meta:
        abstract = True
        base_manager_name = "objects"

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError("Recorded evidence is append-only.")
        database = kwargs.get("using") or router.db_for_write(type(self), instance=self)
        with transaction.atomic(using=database):
            from common.serialization import lock_integrity_writes

            lock_integrity_writes(database)
            self.lock_related_records(database)
            self.full_clean()
            kwargs["using"] = database
            kwargs["force_insert"] = True
            return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise TypeError("Recorded evidence is append-only.")

    def lock_related_records(self, database):
        """Concrete records can serialize validation of their referenced identities."""
