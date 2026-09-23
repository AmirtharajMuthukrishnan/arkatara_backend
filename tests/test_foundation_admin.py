from unittest.mock import patch

import pytest
from django.contrib.admin.sites import AdminSite
from django.core.exceptions import PermissionDenied
from django.test import RequestFactory
from django.urls import reverse

from accounts.models import StaffUser
from compliance.admin import AuditEventAdmin, BusinessConfigurationAdmin, LegalEntityAdmin
from compliance.models import AuditEvent, BusinessConfiguration, LegalEntity


@pytest.fixture
def admin_request():
    request = RequestFactory().post("/admin/")
    request.user = StaffUser.objects.create_superuser(username="fixture-administrator")
    return request


def entity(**changes):
    facts = {
        "code": "fictional-entity",
        "registered_name": "Fictional test entity",
        "country_code": "IN",
    }
    return LegalEntity(**(facts | changes))


@pytest.mark.django_db
def test_legal_entity_admin_records_atomic_attributed_before_and_after(admin_request):
    model_admin = LegalEntityAdmin(LegalEntity, AdminSite())
    record = entity()
    model_admin.save_model(admin_request, record, None, False)
    first = AuditEvent.objects.get(resource_identifier=str(record.public_id))
    assert first.actor_id == admin_request.user.pk
    assert first.actor_label == f"staff:{admin_request.user.public_id}"
    assert first.changes["registered_name"] == {"before": None, "after": "Fictional test entity"}

    record.display_name = "Changed fixture name"
    model_admin.save_model(admin_request, record, None, True)
    changed = AuditEvent.objects.get(
        resource_identifier=str(record.public_id), action="admin.change"
    )
    assert changed.changes == {"display_name": {"before": "", "after": "Changed fixture name"}}
    first.refresh_from_db()
    assert first.changes["registered_name"] == {"before": None, "after": "Fictional test entity"}
    assert first.changes["display_name"] == {"before": None, "after": ""}
    assert not model_admin.has_delete_permission(admin_request, record)


@pytest.mark.django_db
def test_failed_audit_write_rolls_back_the_privileged_mutation(admin_request):
    model_admin = LegalEntityAdmin(LegalEntity, AdminSite())
    record = entity()
    with (
        patch("compliance.admin.record_model_change", side_effect=RuntimeError("fixture failure")),
        pytest.raises(RuntimeError, match="fixture failure"),
    ):
        model_admin.save_model(admin_request, record, None, False)
    assert not LegalEntity.objects.filter(code=record.code).exists()
    assert not AuditEvent.objects.exists()


@pytest.mark.django_db
def test_configuration_admin_can_only_add_audited_draft_revisions(admin_request):
    model_admin = BusinessConfigurationAdmin(BusinessConfiguration, AdminSite())
    for expected_version, value in enumerate((4, 8), start=1):
        record = BusinessConfiguration(
            namespace="trials", key="maximum-boxes", scope={}, value=value
        )
        model_admin.save_model(admin_request, record, None, False)
        assert record.version == expected_version
        assert record.status == BusinessConfiguration.Status.DRAFT
        assert not record.approval_reference
        audit = AuditEvent.objects.get(resource_identifier=str(record.public_id))
        assert audit.changes["value"]["after"] == value
        assert audit.actor_id == admin_request.user.pk
    assert not model_admin.has_change_permission(admin_request, record)
    assert not model_admin.has_delete_permission(admin_request, record)
    with pytest.raises(PermissionDenied):
        model_admin.save_model(admin_request, record, None, True)
    record.status = BusinessConfiguration.Status.ACTIVE
    with pytest.raises(PermissionDenied):
        model_admin.save_model(admin_request, record, None, False)


@pytest.mark.django_db
def test_audit_admin_is_read_only_even_for_administrators(admin_request):
    model_admin = AuditEventAdmin(AuditEvent, AdminSite())
    assert model_admin.has_view_permission(admin_request)
    assert not model_admin.has_add_permission(admin_request)
    assert not model_admin.has_change_permission(admin_request)
    assert not model_admin.has_delete_permission(admin_request)


@pytest.mark.django_db
def test_staff_without_model_permissions_cannot_read_or_change_foundation_admin(client):
    user = StaffUser.objects.create_user(username="fixture-unprivileged-staff", is_staff=True)
    client.force_login(user)
    for model in ("legalentity", "businessconfiguration", "auditevent"):
        assert client.get(reverse(f"admin:compliance_{model}_changelist")).status_code == 403
        assert client.post(reverse(f"admin:compliance_{model}_add"), {}).status_code == 403


@pytest.mark.django_db
def test_configuration_admin_form_accepts_global_scope_and_false_without_approval(client):
    administrator = StaffUser.objects.create_superuser(username="fixture-form-administrator")
    client.force_login(administrator)
    for expected_version, value in enumerate(("false", "true"), start=1):
        response = client.post(
            reverse("admin:compliance_businessconfiguration_add"),
            {
                "namespace": "trials",
                "key": "category-eligibility",
                "scope": "{}",
                "value": value,
                "_save": "Save",
                # Crafted fields cannot activate a policy through the draft-only form.
                "status": "ACTIVE",
                "approval_reference": "not-an-owner-approval",
            },
        )
        assert response.status_code == 302
        revision = BusinessConfiguration.objects.get(version=expected_version)
        assert revision.scope == {}
        assert revision.value is (value == "true")
        assert revision.status == BusinessConfiguration.Status.DRAFT
        assert revision.approval_reference == ""
        assert AuditEvent.objects.filter(
            resource_identifier=str(revision.public_id), actor=administrator
        ).exists()
