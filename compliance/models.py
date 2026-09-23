import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q

from common.models import TimeStampedModel
from compliance.configuration import configuration_scope_key, validate_configuration_value


class ImmutableQuerySet(models.QuerySet):
    def update(self, **kwargs):
        raise TypeError("Recorded revisions and audit events are append-only.")

    def delete(self):
        raise TypeError("Recorded revisions and audit events are append-only.")

    def bulk_create(self, *args, **kwargs):
        raise TypeError("Use individually validated append-only writes; bulk inserts are disabled.")

    def bulk_update(self, *args, **kwargs):
        raise TypeError("Recorded revisions and audit events are append-only.")


class LegalEntity(TimeStampedModel):
    class Status(models.TextChoices):
        DRAFT = "DRAFT", "Draft"
        ACTIVE = "ACTIVE", "Active"
        INACTIVE = "INACTIVE", "Inactive"

    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    code = models.SlugField(max_length=40, unique=True)
    registered_name = models.CharField(max_length=255)
    display_name = models.CharField(max_length=255, blank=True)
    country_code = models.CharField(max_length=2)
    tax_registration_number = models.CharField(max_length=64, blank=True)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.DRAFT)
    effective_from = models.DateField(null=True, blank=True)
    effective_to = models.DateField(null=True, blank=True)

    class Meta:
        ordering = ["code"]
        constraints = [
            models.CheckConstraint(
                condition=Q(effective_to__isnull=True)
                | Q(effective_from__isnull=True)
                | Q(effective_to__gte=models.F("effective_from")),
                name="legal_entity_effective_range_valid",
            )
        ]

    def __str__(self) -> str:
        return self.registered_name


class BusinessConfiguration(TimeStampedModel):
    class Status(models.TextChoices):
        DRAFT = "DRAFT", "Draft"
        ACTIVE = "ACTIVE", "Active"
        RETIRED = "RETIRED", "Retired"

    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    namespace = models.SlugField(max_length=80)
    key = models.SlugField(max_length=120)
    scope_key = models.CharField(max_length=200, default="global")
    scope = models.JSONField(default=dict, blank=True)
    value = models.JSONField()
    version = models.PositiveIntegerField(default=1)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.DRAFT)
    effective_from = models.DateTimeField(null=True, blank=True)
    effective_to = models.DateTimeField(null=True, blank=True)
    approval_reference = models.CharField(max_length=500, blank=True)
    approval_recorded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="recorded_configuration_approvals",
    )
    approved_at = models.DateTimeField(null=True, blank=True)

    objects = ImmutableQuerySet.as_manager()

    class Meta:
        ordering = ["namespace", "key", "scope_key", "-version"]
        base_manager_name = "objects"
        constraints = [
            models.UniqueConstraint(
                fields=["namespace", "key", "scope_key", "version"],
                name="business_configuration_version_unique",
            ),
            models.CheckConstraint(
                condition=Q(version__gte=1),
                name="business_configuration_version_positive",
            ),
            models.CheckConstraint(
                condition=Q(effective_to__isnull=True)
                | Q(effective_from__isnull=True)
                | Q(effective_to__gt=models.F("effective_from")),
                name="business_configuration_effective_range_valid",
            ),
        ]
        indexes = [
            models.Index(
                fields=["namespace", "key", "scope_key", "status"],
                name="business_config_lookup_idx",
            )
        ]

    def clean(self) -> None:
        super().clean()
        self.scope_key = configuration_scope_key(self.scope)
        validate_configuration_value(self.namespace, self.key, self.value)
        if self.status != self.Status.DRAFT and (
            not self.approval_reference.strip()
            or not self.approval_recorded_by_id
            or not self.approved_at
            or not self.effective_from
        ):
            raise ValidationError(
                {"status": "Approved revisions require approval reference, actor and timestamps."}
            )
        if self.status == self.Status.DRAFT and (
            self.approval_reference or self.approval_recorded_by_id or self.approved_at
        ):
            raise ValidationError({"status": "Draft revisions cannot claim commercial approval."})

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise TypeError("Configuration revisions are append-only; record a new version.")
        self.scope_key = configuration_scope_key(self.scope)
        self.full_clean()
        # Never update an existing primary key passed into a newly constructed instance.
        kwargs["force_insert"] = True
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise TypeError("Configuration revisions are append-only; preserve their history.")

    def __str__(self) -> str:
        return f"{self.namespace}.{self.key}:{self.scope_key}@{self.version}"


class AuditEvent(models.Model):
    event_id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="audit_events",
    )
    actor_label = models.CharField(max_length=255, blank=True)
    action = models.CharField(max_length=120)
    resource_type = models.CharField(max_length=120)
    resource_identifier = models.CharField(max_length=255)
    occurred_at = models.DateTimeField(auto_now_add=True, editable=False)
    reason = models.TextField(blank=True)
    changes = models.JSONField(default=dict, blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    correlation_id = models.UUIDField(null=True, blank=True)

    objects = ImmutableQuerySet.as_manager()

    class Meta:
        ordering = ["-occurred_at"]
        base_manager_name = "objects"
        indexes = [
            models.Index(
                fields=["resource_type", "resource_identifier", "occurred_at"],
                name="audit_resource_time_idx",
            ),
            models.Index(fields=["actor", "occurred_at"], name="audit_actor_time_idx"),
        ]

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise TypeError("Audit events are append-only.")
        if self.actor_id:
            self.actor_label = f"staff:{self.actor.public_id}"
        elif not self.actor_label.startswith("system:") or not self.actor_label[7:].strip():
            raise ValidationError(
                {"actor_label": "Identify a staff actor or a named system actor."}
            )
        for field_name in ("changes", "metadata"):
            if not isinstance(getattr(self, field_name), dict):
                raise ValidationError({field_name: "Audit evidence must be a JSON object."})
        self.full_clean()
        kwargs["force_insert"] = True
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise TypeError("Audit events are append-only.")
