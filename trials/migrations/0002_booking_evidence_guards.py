from django.db import migrations
from django.db.migrations.exceptions import IrreversibleError


def require_empty(apps, schema_editor):
    for name in (
        "TrialBooking", "TrialBox", "BookingPolicyAcceptance",
        "BookingPhoneChallenge", "GuestBookingAccessGrant",
    ):
        if apps.get_model("trials", name).objects.using(schema_editor.connection.alias).exists():
            raise IrreversibleError("Booking evidence exists; use reviewed forward recovery.")


class Migration(migrations.Migration):
    dependencies = [
        ("trials", "0001_initial"),
        ("accounts", "0003_customer_identity_guard"),
        ("compliance", "0004_policy_history_guard"),
    ]

    # Frozen database enforcement complements row checks and ordinary ORM guards.
    # Refs: BUSINESS_RULES.md R-34, R-35, R-36; DECISIONS.md D-24, D-25.
    operations = [
        migrations.RunSQL(
            """
            CREATE FUNCTION trials_reject_evidence_mutation() RETURNS trigger
            LANGUAGE plpgsql AS $$
            BEGIN
                RAISE EXCEPTION 'Booking evidence is append-only.' USING ERRCODE = '55000';
            END;
            $$;
            CREATE FUNCTION trials_guard_booking() RETURNS trigger LANGUAGE plpgsql AS $$
            DECLARE market_country text;
            BEGIN
                IF TG_OP = 'DELETE' THEN
                    RAISE EXCEPTION 'Booking history must be retained.' USING ERRCODE = '55000';
                END IF;
                IF TG_OP = 'UPDATE' AND (to_jsonb(OLD) - 'updated_at')
                    IS DISTINCT FROM (to_jsonb(NEW) - 'updated_at') THEN
                    RAISE EXCEPTION 'Booking ownership and snapshots are immutable.' USING ERRCODE = '55000';
                END IF;
                IF btrim(NEW.contact_name) = '' OR btrim(NEW.address) = ''
                    OR btrim(NEW.postal_code) = ''
                    OR NEW.contact_phone !~ '^[+][1-9][0-9]{1,14}$'
                    OR NEW.request_fingerprint !~ '^[0-9a-f]{64}$' THEN
                    RAISE EXCEPTION 'Valid booking contact and request evidence required.' USING ERRCODE = '23514';
                END IF;
                SELECT country_code INTO market_country FROM markets_market
                    WHERE id = NEW.market_id FOR SHARE;
                IF market_country IS DISTINCT FROM NEW.country_code THEN
                    RAISE EXCEPTION 'Booking country must match its market.' USING ERRCODE = '23514';
                END IF;
                IF NEW.country_code = 'IN' AND NEW.postal_code !~ '^[1-9][0-9]{5}$' THEN
                    RAISE EXCEPTION 'Invalid booking postal code.' USING ERRCODE = '23514';
                END IF;
                RETURN NEW;
            END;
            $$;
            CREATE FUNCTION trials_guard_challenge() RETURNS trigger LANGUAGE plpgsql AS $$
            DECLARE expected_phone text;
            BEGIN
                IF TG_OP = 'DELETE' THEN
                    RAISE EXCEPTION 'Challenge evidence must be retained.' USING ERRCODE = '55000';
                END IF;
                IF TG_TABLE_NAME = 'trials_bookingphonechallenge' THEN
                    SELECT contact_phone INTO expected_phone FROM trials_trialbooking
                        WHERE id = NEW.booking_id FOR SHARE;
                ELSE
                    SELECT b.contact_phone INTO expected_phone
                        FROM delivery_deliveryassignment a JOIN trials_trialbooking b ON b.id = a.booking_id
                        WHERE a.id = NEW.assignment_id FOR SHARE OF a, b;
                END IF;
                IF NEW.phone IS DISTINCT FROM expected_phone THEN
                    RAISE EXCEPTION 'Challenge phone must match its subject.' USING ERRCODE = '23514';
                END IF;
                IF TG_OP = 'INSERT' THEN
                    IF NEW.attempts_used <> 0 OR NEW.verified_at IS NOT NULL
                        OR NEW.consumed_at IS NOT NULL OR NEW.invalidated_at IS NOT NULL THEN
                        RAISE EXCEPTION 'New challenges must be unused.' USING ERRCODE = '23514';
                    END IF;
                ELSE
                    IF (to_jsonb(OLD) - ARRAY['updated_at','attempts_used','verified_at','consumed_at','invalidated_at'])
                        IS DISTINCT FROM
                       (to_jsonb(NEW) - ARRAY['updated_at','attempts_used','verified_at','consumed_at','invalidated_at']) THEN
                        RAISE EXCEPTION 'Challenge binding is immutable.' USING ERRCODE = '55000';
                    END IF;
                    IF NEW.attempts_used < OLD.attempts_used
                        OR (OLD.verified_at IS NOT NULL AND (NEW.verified_at IS DISTINCT FROM OLD.verified_at OR NEW.attempts_used <> OLD.attempts_used))
                        OR (OLD.consumed_at IS NOT NULL AND NEW.consumed_at IS DISTINCT FROM OLD.consumed_at)
                        OR (OLD.invalidated_at IS NOT NULL AND NEW.invalidated_at IS DISTINCT FROM OLD.invalidated_at)
                        OR (OLD.verified_at IS NULL AND NEW.verified_at IS NOT NULL AND NEW.attempts_used <= OLD.attempts_used) THEN
                        RAISE EXCEPTION 'Challenge evidence cannot be rewritten.' USING ERRCODE = '55000';
                    END IF;
                    IF (OLD.consumed_at IS NOT NULL OR OLD.invalidated_at IS NOT NULL) AND
                       ROW(NEW.attempts_used, NEW.verified_at, NEW.consumed_at, NEW.invalidated_at)
                        IS DISTINCT FROM ROW(OLD.attempts_used, OLD.verified_at, OLD.consumed_at, OLD.invalidated_at) THEN
                        RAISE EXCEPTION 'Closed challenges cannot be reused.' USING ERRCODE = '55000';
                    END IF;
                END IF;
                RETURN NEW;
            END;
            $$;
            CREATE FUNCTION trials_guard_guest_grant() RETURNS trigger LANGUAGE plpgsql AS $$
            DECLARE mode text;
            BEGIN
                IF TG_OP = 'DELETE' THEN
                    RAISE EXCEPTION 'Guest grant evidence must be retained.' USING ERRCODE = '55000';
                END IF;
                SELECT checkout_mode INTO mode FROM trials_trialbooking WHERE id = NEW.booking_id FOR SHARE;
                IF mode IS DISTINCT FROM 'GUEST' OR NEW.token_digest !~ '^[0-9a-f]{64}$' THEN
                    RAISE EXCEPTION 'Guest grant requires a guest booking and a digest.' USING ERRCODE = '23514';
                END IF;
                IF TG_OP = 'UPDATE' AND (
                    (to_jsonb(OLD) - ARRAY['updated_at','revoked_at']) IS DISTINCT FROM
                    (to_jsonb(NEW) - ARRAY['updated_at','revoked_at']) OR
                    (OLD.revoked_at IS NOT NULL AND NEW.revoked_at IS DISTINCT FROM OLD.revoked_at)) THEN
                    RAISE EXCEPTION 'Guest grant identity and revocation are immutable.' USING ERRCODE = '55000';
                END IF;
                RETURN NEW;
            END;
            $$;
            CREATE TRIGGER booking_identity_guard BEFORE INSERT OR UPDATE OR DELETE ON trials_trialbooking
                FOR EACH ROW EXECUTE FUNCTION trials_guard_booking();
            CREATE TRIGGER booking_box_history BEFORE UPDATE OR DELETE ON trials_trialbox
                FOR EACH ROW EXECUTE FUNCTION trials_reject_evidence_mutation();
            CREATE TRIGGER booking_acceptance_history BEFORE UPDATE OR DELETE ON trials_bookingpolicyacceptance
                FOR EACH ROW EXECUTE FUNCTION trials_reject_evidence_mutation();
            CREATE TRIGGER booking_phone_challenge_guard BEFORE INSERT OR UPDATE OR DELETE ON trials_bookingphonechallenge
                FOR EACH ROW EXECUTE FUNCTION trials_guard_challenge();
            CREATE TRIGGER guest_access_grant_guard BEFORE INSERT OR UPDATE OR DELETE ON trials_guestbookingaccessgrant
                FOR EACH ROW EXECUTE FUNCTION trials_guard_guest_grant();
            """,
            """
            DROP TRIGGER guest_access_grant_guard ON trials_guestbookingaccessgrant;
            DROP TRIGGER booking_phone_challenge_guard ON trials_bookingphonechallenge;
            DROP TRIGGER booking_acceptance_history ON trials_bookingpolicyacceptance;
            DROP TRIGGER booking_box_history ON trials_trialbox;
            DROP TRIGGER booking_identity_guard ON trials_trialbooking;
            DROP FUNCTION trials_guard_guest_grant();
            DROP FUNCTION trials_guard_challenge();
            DROP FUNCTION trials_guard_booking();
            DROP FUNCTION trials_reject_evidence_mutation();
            """,
        ),
        migrations.RunPython(migrations.RunPython.noop, require_empty),
    ]
