"""Versioned policy content; storing a revision does not make it legally approved."""

import hashlib

from django.core.exceptions import ValidationError
from django.db import models

from common.evidence import AppendOnlyModel


class PolicyDocumentRevision(AppendOnlyModel):
    """Keep the exact content against which later acceptance is recorded.

    No commercial/legal approval, retention period or production wording is seeded.
    Refs: BUSINESS_RULES.md R-28, R-32, R-35; DECISIONS.md LR-04.
    """

    document_code = models.SlugField(max_length=80)
    revision = models.PositiveIntegerField()
    language_code = models.CharField(max_length=20)
    content = models.TextField()
    content_sha256 = models.CharField(max_length=64, editable=False)

    class Meta(AppendOnlyModel.Meta):
        constraints = [
            models.UniqueConstraint(
                fields=["document_code", "language_code", "revision"],
                name="policy_document_revision_unique",
            ),
            models.CheckConstraint(
                condition=models.Q(revision__gt=0), name="policy_revision_positive"
            ),
            models.CheckConstraint(condition=~models.Q(content=""), name="policy_content_required"),
        ]

    def clean(self):
        super().clean()
        if not self.content.strip() or not self.language_code.strip():
            raise ValidationError("Policy content and language must be nonblank.")

    def save(self, *args, **kwargs):
        if not isinstance(self.content, str):
            raise ValidationError({"content": "Policy content must be text."})
        self.content_sha256 = hashlib.sha256(self.content.encode("utf-8")).hexdigest()
        return super().save(*args, **kwargs)
