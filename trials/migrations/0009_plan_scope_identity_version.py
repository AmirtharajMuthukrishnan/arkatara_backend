"""Keep the recorded identifier contract of legacy plans without rewriting scopes."""

from django.db import migrations, models
from django.db.migrations.exceptions import IrreversibleError


def preserve_versioned_history(apps, schema_editor):
    Plan = apps.get_model("trials", "TrialPlanRevision")
    if Plan.objects.using(schema_editor.connection.alias).filter(policy_scope_version=2).exists():
        raise IrreversibleError("UUID-scoped plan evidence exists; use reviewed forward recovery.")


class Migration(migrations.Migration):
    dependencies = [
        ("trials", "0008_visit_corrections"),
        ("payments", "0005_charge_scope_identity_version"),
    ]
    operations = [
        migrations.AddField(
            model_name="trialplanrevision", name="policy_scope_version",
            field=models.PositiveSmallIntegerField(default=1),
        ),
        migrations.AlterField(
            model_name="trialplanrevision", name="policy_scope_version",
            field=models.PositiveSmallIntegerField(default=2),
        ),
        migrations.AddConstraint(
            model_name="trialplanrevision",
            constraint=models.CheckConstraint(
                condition=models.Q(policy_scope_version__in=[1, 2]), name="plan_policy_scope_version"
            ),
        ),
        migrations.RunPython(migrations.RunPython.noop, preserve_versioned_history),
    ]
