from django.db import migrations
from django.db.migrations.exceptions import IrreversibleError


def require_empty(apps, schema_editor):
    for name in ("DeliveryAssignment", "ArrivalOTP"):
        if apps.get_model("delivery", name).objects.using(schema_editor.connection.alias).exists():
            raise IrreversibleError("Delivery evidence exists; use reviewed forward recovery.")


class Migration(migrations.Migration):
    dependencies = [
        ("delivery", "0002_initial"),
        ("trials", "0002_booking_evidence_guards"),
    ]

    # Refs: BUSINESS_RULES.md R-18, R-21, R-30, R-36; DECISIONS.md D-24.
    operations = [
        migrations.RunSQL(
            """
            CREATE FUNCTION delivery_guard_assignment() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
                IF TG_OP = 'DELETE' THEN
                    RAISE EXCEPTION 'Assignment history must be retained.' USING ERRCODE = '55000';
                END IF;
                IF (to_jsonb(OLD) - ARRAY['updated_at','ended_at','end_reason']) IS DISTINCT FROM
                   (to_jsonb(NEW) - ARRAY['updated_at','ended_at','end_reason']) THEN
                    RAISE EXCEPTION 'Assignment identity is immutable.' USING ERRCODE = '55000';
                END IF;
                IF OLD.ended_at IS NOT NULL AND ROW(NEW.ended_at, NEW.end_reason)
                    IS DISTINCT FROM ROW(OLD.ended_at, OLD.end_reason) THEN
                    RAISE EXCEPTION 'Ended assignment cannot be reopened.' USING ERRCODE = '55000';
                END IF;
                RETURN NEW;
            END;
            $$;
            CREATE TRIGGER assignment_identity_guard BEFORE UPDATE OR DELETE ON delivery_deliveryassignment
                FOR EACH ROW EXECUTE FUNCTION delivery_guard_assignment();
            CREATE TRIGGER arrival_challenge_guard BEFORE INSERT OR UPDATE OR DELETE ON delivery_arrivalotp
                FOR EACH ROW EXECUTE FUNCTION trials_guard_challenge();
            """,
            """
            DROP TRIGGER arrival_challenge_guard ON delivery_arrivalotp;
            DROP TRIGGER assignment_identity_guard ON delivery_deliveryassignment;
            DROP FUNCTION delivery_guard_assignment();
            """,
        ),
        migrations.RunPython(migrations.RunPython.noop, require_empty),
    ]
