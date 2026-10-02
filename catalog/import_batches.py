"""Immutable catalogue import outcomes, separate from onboarding orchestration."""

import uuid

from django.conf import settings
from django.db import models

from compliance.models import ImmutableQuerySet


class CSVImportBatch(models.Model):
    """Retain attributable import outcomes so a retry cannot repeat onboarding.

    Refs: BUSINESS_RULES.md R-11, R-28, R-30; DECISIONS.md BD-09.
    """

    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    idempotency_key = models.UUIDField(unique=True)
    kind = models.CharField(max_length=16, choices=[("VARIANTS", "Variants"), ("UNITS", "Units")])
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    payload_sha256 = models.CharField(max_length=64)
    reason = models.TextField()
    row_count = models.PositiveIntegerField()
    result = models.JSONField()
    created_at = models.DateTimeField(auto_now_add=True)

    objects = ImmutableQuerySet.as_manager()

    class Meta:
        ordering = ["-created_at"]
        base_manager_name = "objects"
        permissions = [("import_catalogue", "Can preview and apply draft catalogue CSV imports")]

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise TypeError("Import outcomes are append-only.")
        self.full_clean()
        kwargs["force_insert"] = True
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise TypeError("Import outcomes are append-only.")
