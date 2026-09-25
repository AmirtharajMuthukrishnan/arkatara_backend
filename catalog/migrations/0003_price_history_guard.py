from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [("catalog", "0002_initial_materials")]
    operations = [
        migrations.RunSQL(
            sql="""
                CREATE FUNCTION catalog_reject_price_mutation()
                RETURNS trigger LANGUAGE plpgsql AS $$
                BEGIN
                    RAISE EXCEPTION 'Price revisions are append-only.' USING ERRCODE = '55000';
                END;
                $$;
                CREATE TRIGGER catalog_price_append_only
                BEFORE UPDATE OR DELETE ON catalog_pricerevision
                FOR EACH ROW EXECUTE FUNCTION catalog_reject_price_mutation();
            """,
            reverse_sql="""
                DROP TRIGGER catalog_price_append_only ON catalog_pricerevision;
                DROP FUNCTION catalog_reject_price_mutation();
            """,
        )
    ]
