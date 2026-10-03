from django.db import migrations
from django.db.migrations.exceptions import IrreversibleError


def require_empty(apps, schema_editor):
    if apps.get_model("accounts", "CustomerAccount").objects.using(
        schema_editor.connection.alias
    ).exists():
        raise IrreversibleError("Customer identities exist; use reviewed forward recovery.")


class Migration(migrations.Migration):
    dependencies = [("accounts", "0002_customeraccount")]

    # Refs: BUSINESS_RULES.md R-33, R-34, R-35; DECISIONS.md D-24.
    operations = [
        migrations.RunSQL(
            """
            CREATE FUNCTION accounts_guard_customer_identity() RETURNS trigger
            LANGUAGE plpgsql AS $$
            BEGIN
                IF TG_OP = 'DELETE' THEN
                    RAISE EXCEPTION 'Customer identity must be retained.' USING ERRCODE = '55000';
                END IF;
                IF (to_jsonb(OLD) - ARRAY['display_name','status','closed_at','updated_at'])
                    IS DISTINCT FROM
                   (to_jsonb(NEW) - ARRAY['display_name','status','closed_at','updated_at']) THEN
                    RAISE EXCEPTION 'Customer identity is immutable.' USING ERRCODE = '55000';
                END IF;
                IF OLD.closed_at IS NOT NULL AND
                    ROW(NEW.status, NEW.closed_at) IS DISTINCT FROM ROW(OLD.status, OLD.closed_at) THEN
                    RAISE EXCEPTION 'Closed customer identity cannot be reopened.' USING ERRCODE = '55000';
                END IF;
                RETURN NEW;
            END;
            $$;
            CREATE TRIGGER customer_identity_guard BEFORE UPDATE OR DELETE
            ON accounts_customeraccount FOR EACH ROW
            EXECUTE FUNCTION accounts_guard_customer_identity();
            """,
            """
            DROP TRIGGER customer_identity_guard ON accounts_customeraccount;
            DROP FUNCTION accounts_guard_customer_identity();
            """,
        ),
        migrations.RunPython(migrations.RunPython.noop, require_empty),
    ]
