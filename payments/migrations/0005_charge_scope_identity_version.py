"""Preserve code-scoped history while new charge bindings use public UUIDs."""

from django.db import migrations, models
from django.db.migrations.exceptions import IrreversibleError


def preserve_versioned_history(apps, schema_editor):
    Charge = apps.get_model("payments", "BookingCharge")
    if Charge.objects.using(schema_editor.connection.alias).filter(policy_scope_version=2).exists():
        raise IrreversibleError("UUID-scoped charge evidence exists; use reviewed forward recovery.")


class Migration(migrations.Migration):
    dependencies = [("payments", "0004_paymentresolutionreceipt_and_more")]
    operations = [
        migrations.AddField(
            model_name="bookingcharge", name="policy_scope_version",
            field=models.PositiveSmallIntegerField(default=1),
        ),
        migrations.AlterField(
            model_name="bookingcharge", name="policy_scope_version",
            field=models.PositiveSmallIntegerField(default=2),
        ),
        migrations.AddConstraint(
            model_name="bookingcharge",
            constraint=models.CheckConstraint(
                condition=models.Q(policy_scope_version__in=[1, 2]), name="charge_policy_scope_version"
            ),
        ),
        migrations.RunPython(migrations.RunPython.noop, preserve_versioned_history),
    ]
