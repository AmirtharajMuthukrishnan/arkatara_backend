from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("inventory", "0002_inventorymovement_alter_inventoryunit_options_and_more"),
    ]
    operations = [
        migrations.RunSQL(
            sql="""
                CREATE FUNCTION inventory_reject_movement_mutation()
                RETURNS trigger LANGUAGE plpgsql AS $$
                BEGIN
                    RAISE EXCEPTION 'Inventory movements are append-only.' USING ERRCODE = '55000';
                END;
                $$;
                CREATE TRIGGER inventory_movement_append_only
                BEFORE UPDATE OR DELETE ON inventory_inventorymovement
                FOR EACH ROW EXECUTE FUNCTION inventory_reject_movement_mutation();
            """,
            reverse_sql="""
                DROP TRIGGER inventory_movement_append_only ON inventory_inventorymovement;
                DROP FUNCTION inventory_reject_movement_mutation();
            """,
        )
    ]
