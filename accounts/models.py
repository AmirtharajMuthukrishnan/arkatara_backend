import uuid

from django.contrib.auth.models import AbstractUser
from django.db import models


class StaffUser(AbstractUser):
    """Staff identity leaves guest browsing and booking free of a login requirement.

    Assignment permissions belong to the later staff workflow, not this identity alone.
    Refs: BUSINESS_RULES.md R-04, R-18.
    """

    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)

    class Meta:
        verbose_name = "staff user"
        verbose_name_plural = "staff users"
