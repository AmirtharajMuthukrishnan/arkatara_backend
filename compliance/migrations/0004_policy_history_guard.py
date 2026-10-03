from django.db import migrations
from django.db.migrations.exceptions import IrreversibleError


def require_empty(apps, schema_editor):
    if apps.get_model("compliance", "PolicyDocumentRevision").objects.using(
        schema_editor.connection.alias
    ).exists():
        raise IrreversibleError("Policy evidence exists; use reviewed forward recovery.")


class Migration(migrations.Migration):
    dependencies = [("compliance", "0003_policydocumentrevision")]

    # Refs: BUSINESS_RULES.md R-28, R-35; DECISIONS.md LR-04.
    operations = [
        migrations.RunSQL(
            """
            CREATE FUNCTION compliance_reject_policy_mutation() RETURNS trigger
            LANGUAGE plpgsql AS $$
            BEGIN
                RAISE EXCEPTION 'Policy evidence is append-only.' USING ERRCODE = '55000';
            END;
            $$;
            CREATE TRIGGER policy_history_guard BEFORE UPDATE OR DELETE
            ON compliance_policydocumentrevision FOR EACH ROW
            EXECUTE FUNCTION compliance_reject_policy_mutation();
            """,
            """
            DROP TRIGGER policy_history_guard ON compliance_policydocumentrevision;
            DROP FUNCTION compliance_reject_policy_mutation();
            """,
        ),
        migrations.RunPython(migrations.RunPython.noop, require_empty),
    ]
