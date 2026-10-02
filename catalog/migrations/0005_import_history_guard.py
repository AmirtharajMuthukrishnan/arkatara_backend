from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [("catalog", "0004_csvimportbatch_productmedia")]
    operations = [
        migrations.RunSQL(
            sql="""
                CREATE FUNCTION catalog_reject_import_mutation()
                RETURNS trigger LANGUAGE plpgsql AS $$
                BEGIN
                    RAISE EXCEPTION 'Import outcomes are append-only.' USING ERRCODE = '55000';
                END;
                $$;
                CREATE TRIGGER catalog_import_append_only
                BEFORE UPDATE OR DELETE ON catalog_csvimportbatch
                FOR EACH ROW EXECUTE FUNCTION catalog_reject_import_mutation();
            """,
            reverse_sql="""
                DROP TRIGGER catalog_import_append_only ON catalog_csvimportbatch;
                DROP FUNCTION catalog_reject_import_mutation();
            """,
        )
    ]
