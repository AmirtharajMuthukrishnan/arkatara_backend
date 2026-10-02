from django.db import migrations
from django.db.migrations.exceptions import IrreversibleError


def reject_evidence_downgrade(apps, schema_editor):
    database = schema_editor.connection.alias
    unit = apps.get_model("inventory", "InventoryUnit")
    movement = apps.get_model("inventory", "InventoryMovement")
    if (
        movement.objects.using(database).exists()
        or unit.objects.using(database).exclude(status="DRAFT", custody="UNKNOWN").exists()
    ):
        raise IrreversibleError(
            "Recorded stock operations require forward recovery; preserve movement history."
        )


class Migration(migrations.Migration):
    dependencies = [("inventory", "0003_movement_history_guard")]
    operations = [migrations.RunPython(migrations.RunPython.noop, reject_evidence_downgrade)]
