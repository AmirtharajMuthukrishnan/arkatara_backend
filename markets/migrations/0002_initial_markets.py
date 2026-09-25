"""Only approved city identities and launch labels; no geographic coverage."""

from django.db import migrations


def seed_markets(apps, schema_editor):
    market = apps.get_model("markets", "Market")
    for code, name, status in (
        ("BLR", "Bengaluru", "ACTIVE"),
        ("HYD", "Hyderabad", "COMING_SOON"),
    ):
        market.objects.using(schema_editor.connection.alias).get_or_create(
            code=code, defaults={"name": name, "status": status, "country_code": "IN"}
        )


class Migration(migrations.Migration):
    dependencies = [("markets", "0001_initial")]
    # Reversing this seed preserves reference identities and any later references.
    operations = [migrations.RunPython(seed_markets, migrations.RunPython.noop)]
