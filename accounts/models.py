import uuid

from django.contrib.auth.models import AbstractUser
from django.db import models


class StaffUser(AbstractUser):
    """Authenticated workforce identity; this is not a customer account."""

    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)

    class Meta:
        verbose_name = "staff user"
        verbose_name_plural = "staff users"
