"""Check upgrading existing evidence without inventing historical approvals."""

import pytest
from django.db import DatabaseError, connection, transaction
from django.db.migrations.executor import MigrationExecutor


@pytest.mark.django_db(transaction=True)
def test_foundation_upgrade_preserves_legacy_evidence_and_adds_audit_guard():
    old_target = ("compliance", "0001_initial")
    new_target = ("compliance", "0002_foundation_integrity")
    executor = MigrationExecutor(connection)
    restore_targets = executor.loader.graph.leaf_nodes()
    try:
        # Reversing the new migration also exercises its reversible trigger DDL.
        executor.migrate([old_target])
        old_apps = executor.loader.project_state([old_target]).apps
        old_config = old_apps.get_model("compliance", "BusinessConfiguration")
        old_audit = old_apps.get_model("compliance", "AuditEvent")
        legacy = old_config.objects.create(
            namespace="trials",
            key="maximum-boxes",
            scope={},
            value=4,
            status="ACTIVE",
        )
        evidence = old_audit.objects.create(
            action="fictional.legacy.action",
            resource_type="fixture",
            resource_identifier="fictional-legacy-record",
            changes={"fixture": "original evidence"},
        )

        executor = MigrationExecutor(connection)
        executor.migrate([new_target])
        new_apps = executor.loader.project_state([new_target]).apps
        upgraded = new_apps.get_model("compliance", "BusinessConfiguration").objects.get(
            pk=legacy.pk
        )
        retained = new_apps.get_model("compliance", "AuditEvent").objects.get(pk=evidence.pk)
        assert (upgraded.public_id, upgraded.status, upgraded.value) == (
            legacy.public_id,
            "ACTIVE",
            4,
        )
        assert upgraded.approval_reference == ""
        assert upgraded.approved_at is None
        assert upgraded.approval_recorded_by_id is None
        assert retained.actor_label == ""
        assert retained.actor_id is None
        assert retained.changes == {"fixture": "original evidence"}

        with pytest.raises(DatabaseError, match="append-only"), transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE compliance_auditevent SET reason = %s WHERE event_id = %s",
                    ["rewritten", evidence.pk],
                )
    finally:
        # Leave the test database at the full current schema even after an assertion failure.
        MigrationExecutor(connection).migrate(restore_targets)
