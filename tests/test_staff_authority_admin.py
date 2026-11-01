from types import SimpleNamespace

import pytest
from django.contrib import admin
from django.contrib.auth.models import Group, Permission
from django.core.exceptions import ValidationError
from django.db import DatabaseError, transaction
from django.utils import timezone

from accounts.models import StaffScopeGrant, StaffUser
from compliance.models import AuditEvent
from markets.models import Hub, Market
from tests.test_checkout_models import raw_insert, rejected

pytestmark = pytest.mark.django_db


def test_delegated_user_and_group_permissions_cannot_escalate_authority():
    delegated = StaffUser.objects.create_user(username="delegated", is_staff=True)
    delegated.user_permissions.add(
        *Permission.objects.filter(content_type__model__in=["staffuser", "group"])
    )
    request = SimpleNamespace(user=delegated)
    for model in (StaffUser, Group):
        screen = admin.site._registry[model]
        for check in (
            screen.has_module_permission,
            screen.has_view_permission,
            screen.has_add_permission,
            screen.has_change_permission,
            screen.has_delete_permission,
        ):
            assert not check(request)
    trusted = StaffUser.objects.create_superuser(username="trusted", password="fictional-test-only")
    request.user = trusted
    assert admin.site._registry[StaffUser].has_change_permission(request)
    assert admin.site._registry[Group].has_change_permission(request)
    trusted.is_active = False
    assert not admin.site._registry[StaffUser].has_change_permission(request)


def test_staff_authority_change_rolls_back_when_project_audit_fails(monkeypatch):
    trusted = StaffUser.objects.create_superuser(username="trusted", password="fictional-test-only")
    managed = StaffUser.objects.create_user(username="managed", is_staff=True)
    managed.is_superuser = True

    def fail(*args, **kwargs):
        raise ValidationError("Fictional failed audit write")

    monkeypatch.setattr(AuditEvent, "save", fail)
    with pytest.raises(ValidationError):
        admin.site._registry[StaffUser].save_model(
            SimpleNamespace(user=trusted),
            managed,
            SimpleNamespace(changed_data=["is_superuser"]),
            True,
        )
    managed.refresh_from_db()
    assert not managed.is_superuser
    assert not AuditEvent.objects.exists()


def test_staff_scope_cannot_cross_city_or_rewrite_revocation():
    actor = StaffUser.objects.create_user(username="actor", is_staff=True)
    first = Market.objects.create(code="FIRST", name="Fictional city", country_code="IN")
    second = Market.objects.create(code="SECOND", name="Other fictional city", country_code="IN")
    hub = Hub.objects.create(market=second, code="OTHER", name="Other fictional hub")
    permission = Permission.objects.get(
        content_type__app_label="inventory", codename="receive_inventoryunit"
    )
    values = dict(
        staff=actor,
        permission=permission,
        market=first,
        hub=hub,
        granted_by=actor,
        granted_at=timezone.now(),
        reason="Fictional scoped operation",
    )
    with pytest.raises(DatabaseError), transaction.atomic():
        raw_insert(StaffScopeGrant, **values)
    values["market"] = second
    grant = StaffScopeGrant.objects.create(**values)
    grant.revoked_by = actor
    grant.revoked_at = timezone.now()
    grant.revocation_reason = "Fictional revocation"
    grant.save()
    rejected(
        "UPDATE accounts_staffscopegrant SET revoked_at=NULL,revoked_by_id=NULL,"
        "revocation_reason='' WHERE id=%s",
        [grant.pk],
    )
