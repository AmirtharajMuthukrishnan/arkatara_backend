"""Approved initial material identities; no Gold purity or commercial defaults."""

from django.db import migrations


def seed_materials(apps, schema_editor):
    database = schema_editor.connection.alias
    material = apps.get_model("catalog", "Material")
    silver, _ = material.objects.using(database).get_or_create(
        code="SILVER", defaults={"name": "Silver", "status": "ACTIVE"}
    )
    material.objects.using(database).get_or_create(
        code="GOLD", defaults={"name": "Gold", "status": "COMING_SOON"}
    )
    apps.get_model("catalog", "Purity").objects.using(database).get_or_create(
        material_id=silver.pk, code="S925", defaults={"name": "S925", "status": "ACTIVE"}
    )


class Migration(migrations.Migration):
    dependencies = [("catalog", "0001_initial")]
    operations = [migrations.RunPython(seed_materials, migrations.RunPython.noop)]
