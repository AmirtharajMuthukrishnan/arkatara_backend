"""Frozen checkout guards and a lossless per-grant browser credential migration."""

import json

from django.db import migrations, models
from django.db.migrations.exceptions import IrreversibleError
import django.db.models.deletion


# All specifications are frozen here; never import live model classes in a migration.
SPECS = {
    "trials_trialbooking": (
        "status verified_phone_challenge_id verification_valid_until sealed_at confirmed_at acceptance_snapshot",
        "verified_phone_challenge_id verification_valid_until sealed_at confirmed_at acceptance_snapshot",
        "DRAFT", {"DRAFT": ["VERIFIED", "CANCELLED"], "VERIFIED": ["PAYMENT_PENDING", "CANCELLED"],
                  "PAYMENT_PENDING": ["CONFIRMED", "PAYMENT_EXPIRED", "CANCELLED"]}),
    "trials_guestbrowsersession": ("token_digest revoked_at rotated_at", "revoked_at", "", {}),
    "trials_guestbookingaccessgrant": ("revoked_at", "revoked_at", "", {}),
    "trials_guestbookingrecoverygrant": ("revoked_at redeemed_at redeemed_session_id verified_challenge_id",
        "revoked_at redeemed_at redeemed_session_id verified_challenge_id", "", {}),
    "trials_trialitemallocation": ("released_at", "released_at", "", {}),
    "trials_idempotencyrecord": ("status booking_id payment_attempt_id response_status completed_at",
        "booking_id payment_attempt_id response_status completed_at", "IN_PROGRESS",
        {"IN_PROGRESS": ["COMPLETED", "FAILED"]}),
    "payments_bookingcharge": ("status accepted_at", "accepted_at", "QUOTED",
        {"QUOTED": ["DUE", "VOID"], "DUE": ["COLLECTED", "VOID"]}),
    "payments_paymentcontext": ("status capture_started_at paid_at closed_at resolution_reference",
        "capture_started_at paid_at closed_at resolution_reference", "OPEN",
        {"OPEN": ["CAPTURING", "EXPIRED", "CANCELLED", "REVIEW_REQUIRED"],
         "CAPTURING": ["PAID", "REVIEW_REQUIRED"], "REVIEW_REQUIRED": ["PAID", "CANCELLED"]}),
    "payments_paymentattempt": (
        "provider_order_id status capture_requested_at last_verified_at succeeded_at exception_code",
        "provider_order_id capture_requested_at succeeded_at", "CREATING",
        {"CREATING": ["PENDING", "FAILED", "EXPIRED", "REVIEW_REQUIRED"],
         "PENDING": ["AUTHORIZED", "FAILED", "EXPIRED", "REVIEW_REQUIRED"],
         "AUTHORIZED": ["CAPTURE_PENDING", "FAILED", "EXPIRED", "REVIEW_REQUIRED"],
         "CAPTURE_PENDING": ["SUCCEEDED", "REVIEW_REQUIRED"],
         "REVIEW_REQUIRED": ["PENDING", "AUTHORIZED", "CAPTURE_PENDING", "SUCCEEDED", "FAILED"]}),
    "payments_gatewayevent": ("status processed_at", "processed_at", "RECEIVED",
        {"RECEIVED": ["PROCESSED", "REVIEW_REQUIRED"], "REVIEW_REQUIRED": ["PROCESSED"]}),
    "inventory_inventoryreservation": (
        "status capture_started_at committed_at released_at release_reason resolution_reference",
        "capture_started_at committed_at released_at release_reason resolution_reference", "HELD",
        {"HELD": ["CAPTURE_PENDING", "EXPIRED", "RELEASED"], "CAPTURE_PENDING": ["COMMITTED", "RELEASED"]}),
    "notifications_notificationevent": ("status sent_at", "sent_at", "PENDING",
        {"PENDING": ["SENT", "FAILED", "EXPIRED"]}),
    "notifications_notificationattempt": ("provider_message_id status accepted_at delivered_at error_code",
        "provider_message_id accepted_at delivered_at", "PENDING",
        {"PENDING": ["ACCEPTED", "FAILED", "UNKNOWN"], "ACCEPTED": ["DELIVERED", "FAILED", "UNKNOWN"],
         "UNKNOWN": ["ACCEPTED", "DELIVERED", "FAILED"]}),
    "notifications_outboxcommand": (
        "status attempts available_at lease_token lease_until completed_at last_error_code", "completed_at", "PENDING",
        {"PENDING": ["PROCESSING", "FAILED"], "PROCESSING": ["PENDING", "SUCCEEDED", "FAILED", "REVIEW_REQUIRED"],
         "REVIEW_REQUIRED": ["PENDING", "SUCCEEDED", "FAILED"]}),
}
APPEND_ONLY = ["trials_trialplanrevision", "trials_trialboxitem", "payments_gatewaytransaction", "payments_paymentallocation"]


def preserve_credentials(apps, schema_editor):
    alias = schema_editor.connection.alias
    Session = apps.get_model("trials", "GuestBrowserSession")
    Grant = apps.get_model("trials", "GuestBookingAccessGrant")
    for grant in Grant.objects.using(alias).order_by("pk").iterator():
        session = Session.objects.using(alias).create(
            token_digest=grant.token_digest, issued_at=grant.issued_at,
            expires_at=grant.expires_at, revoked_at=grant.revoked_at,
        )
        Grant.objects.using(alias).filter(pk=grant.pk).update(browser_session_id=session.pk)


def require_empty(apps, schema_editor):
    # Do not downgrade away new evidence or restore a draft-only schema with live records.
    for table in [*SPECS, *APPEND_ONLY, "trials_guestrecoveryphonechallenge", "trials_trialbox"]:
        with schema_editor.connection.cursor() as cursor:
            cursor.execute(f'SELECT EXISTS (SELECT 1 FROM "{table}")')
            if cursor.fetchone()[0]:
                raise IrreversibleError("Checkout evidence exists; use reviewed forward recovery.")


SQL = r"""
CREATE FUNCTION checkout_guard_record() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE before_row jsonb; after_row jsonb; mutable text[]; field text; transitions jsonb;
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'Checkout history must be retained.' USING ERRCODE = '55000';
    END IF;
    after_row := to_jsonb(NEW);
    FOREACH field IN ARRAY ARRAY['token_digest','request_fingerprint','payload_digest'] LOOP
        IF after_row ? field AND after_row->>field !~ '^[0-9a-f]{64}$' THEN
            RAISE EXCEPTION 'Valid digest required.' USING ERRCODE='23514';
        END IF;
    END LOOP;
    IF TG_OP = 'INSERT' THEN
        IF TG_ARGV[2] <> '' AND after_row->>'status' IS DISTINCT FROM TG_ARGV[2] THEN
            RAISE EXCEPTION 'New records must start in their initial state.' USING ERRCODE = '23514';
        END IF;
    ELSE
        before_row := to_jsonb(OLD);
        mutable := string_to_array(TG_ARGV[0], ' ') || ARRAY['updated_at'];
        IF (before_row - mutable) IS DISTINCT FROM (after_row - mutable) THEN
            RAISE EXCEPTION 'Checkout identity and snapshots are immutable.' USING ERRCODE = '55000';
        END IF;
        FOREACH field IN ARRAY string_to_array(TG_ARGV[1], ' ') LOOP
            IF before_row->field NOT IN ('null'::jsonb, '""'::jsonb, '{}'::jsonb)
               AND before_row->field IS DISTINCT FROM after_row->field THEN
                RAISE EXCEPTION 'Recorded checkout outcomes are immutable.' USING ERRCODE = '55000';
            END IF;
        END LOOP;
        transitions := TG_ARGV[3]::jsonb;
        IF transitions <> '{}'::jsonb AND before_row->>'status' IS DISTINCT FROM after_row->>'status'
            AND NOT COALESCE((transitions->(before_row->>'status')) ? (after_row->>'status'), false) THEN
            RAISE EXCEPTION 'Invalid checkout transition.' USING ERRCODE = '23514';
        END IF;
    END IF;
    RETURN NEW;
END;
$$;

CREATE FUNCTION checkout_booking_binding() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE proof trials_bookingphonechallenge%ROWTYPE; country text;
BEGIN
    SELECT country_code INTO country FROM markets_market WHERE id=NEW.market_id FOR SHARE;
    IF country IS DISTINCT FROM NEW.country_code OR btrim(NEW.contact_name) = '' OR btrim(NEW.address) = ''
        OR btrim(NEW.postal_code) = '' OR NEW.contact_phone !~ '^[+][1-9][0-9]{1,14}$'
        OR NEW.request_fingerprint !~ '^[0-9a-f]{64}$'
        OR (NEW.country_code='IN' AND NEW.postal_code !~ '^[1-9][0-9]{5}$') THEN
        RAISE EXCEPTION 'Valid immutable booking contact required.' USING ERRCODE = '23514';
    END IF;
    IF NEW.verified_phone_challenge_id IS NOT NULL THEN
        SELECT * INTO proof FROM trials_bookingphonechallenge WHERE id=NEW.verified_phone_challenge_id FOR SHARE;
        IF proof.booking_id IS DISTINCT FROM NEW.id OR proof.consumed_at IS NULL
            OR proof.invalidated_at IS NOT NULL OR NEW.verification_valid_until IS NULL
            OR NEW.verification_valid_until <= proof.consumed_at THEN
            RAISE EXCEPTION 'Booking requires its own consumed phone proof.' USING ERRCODE = '23514';
        END IF;
    END IF;
    IF NEW.sealed_at IS NOT NULL THEN
        IF jsonb_typeof(NEW.acceptance_snapshot) <> 'object' OR NEW.acceptance_snapshot = '{}'
            OR NOT EXISTS (SELECT 1 FROM trials_bookingpolicyacceptance WHERE booking_id=NEW.id)
            OR NOT EXISTS (SELECT 1 FROM trials_trialbox WHERE booking_id=NEW.id)
            OR EXISTS (SELECT 1 FROM trials_trialbox b WHERE b.booking_id=NEW.id AND
                (b.plan_revision_id IS NULL OR b.plan_snapshot = '{}' OR
                 NOT EXISTS (SELECT 1 FROM trials_trialboxitem i WHERE i.box_id=b.id))) THEN
            RAISE EXCEPTION 'Seal requires accepted terms and complete plan selections.' USING ERRCODE = '23514';
        END IF;
    END IF;
    IF TG_OP='UPDATE' AND OLD.status IS DISTINCT FROM NEW.status THEN
        IF NEW.status IN ('VERIFIED','PAYMENT_PENDING') AND NEW.verification_valid_until <= statement_timestamp() THEN
            RAISE EXCEPTION 'Phone verification has expired.' USING ERRCODE = '23514';
        END IF;
        IF NEW.status='PAYMENT_PENDING' AND NOT EXISTS (
            SELECT 1 FROM payments_paymentcontext c JOIN payments_bookingcharge q ON c.booking_charge_id=q.id
            WHERE q.booking_id=NEW.id AND c.status='OPEN' AND checkout_context_complete(c.id,'HELD')) THEN
            RAISE EXCEPTION 'Payment pending requires an open context.' USING ERRCODE = '23514';
        END IF;
        IF NEW.status='CONFIRMED' AND NOT EXISTS (
            SELECT 1 FROM payments_paymentcontext c JOIN payments_bookingcharge q ON c.booking_charge_id=q.id
            WHERE q.booking_id=NEW.id AND c.status='PAID' AND c.paid_at<=NEW.confirmed_at) THEN
            RAISE EXCEPTION 'Confirmation requires a paid context.' USING ERRCODE = '23514';
        END IF;
    END IF;
    RETURN NEW;
END;
$$;

CREATE FUNCTION checkout_selection_binding() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE booking trials_trialbooking%ROWTYPE; box trials_trialbox%ROWTYPE; plan trials_trialplanrevision%ROWTYPE;
DECLARE category bigint;
BEGIN
    IF TG_TABLE_NAME='trials_trialbox' THEN
        SELECT * INTO booking FROM trials_trialbooking WHERE id=NEW.booking_id FOR UPDATE;
        IF NEW.plan_revision_id IS NOT NULL THEN
            SELECT * INTO plan FROM trials_trialplanrevision WHERE id=NEW.plan_revision_id;
            IF plan.market_id IS DISTINCT FROM booking.market_id OR plan.category_id IS DISTINCT FROM NEW.category_id
                OR jsonb_typeof(NEW.plan_snapshot)<>'object' OR NEW.plan_snapshot='{}' THEN
                RAISE EXCEPTION 'Box plan scope or snapshot mismatch.' USING ERRCODE='23514';
            END IF;
        END IF;
    ELSE
        SELECT * INTO box FROM trials_trialbox WHERE id=NEW.box_id;
        SELECT * INTO booking FROM trials_trialbooking WHERE id=box.booking_id FOR UPDATE;
        SELECT p.category_id INTO category FROM catalog_productvariant v JOIN catalog_product p ON p.id=v.product_id
            WHERE v.id=NEW.variant_id FOR SHARE OF p, v;
        IF category IS DISTINCT FROM box.category_id OR jsonb_typeof(NEW.selection_snapshot)<>'object'
            OR NEW.selection_snapshot='{}' THEN
            RAISE EXCEPTION 'Selected variant category or snapshot mismatch.' USING ERRCODE='23514';
        END IF;
    END IF;
    IF booking.status IS DISTINCT FROM 'DRAFT' OR booking.sealed_at IS NOT NULL THEN
        RAISE EXCEPTION 'Sealed booking selections cannot change.' USING ERRCODE='23514';
    END IF;
    RETURN NEW;
END;
$$;

CREATE FUNCTION checkout_guest_binding() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE booking trials_trialbooking%ROWTYPE; session trials_guestbrowsersession%ROWTYPE;
DECLARE proof trials_guestrecoveryphonechallenge%ROWTYPE;
BEGIN
    IF TG_TABLE_NAME='trials_guestbrowsersession' THEN
        IF TG_OP='UPDATE' AND (
            ((OLD.token_digest IS DISTINCT FROM NEW.token_digest) <> (OLD.rotated_at IS DISTINCT FROM NEW.rotated_at))
            OR (OLD.token_digest IS DISTINCT FROM NEW.token_digest AND
                (OLD.revoked_at IS NOT NULL OR NEW.rotated_at IS NULL OR
                 NEW.rotated_at<=COALESCE(OLD.rotated_at,OLD.issued_at)))) THEN
            RAISE EXCEPTION 'Invalid browser credential rotation.' USING ERRCODE='23514';
        END IF;
        RETURN NEW;
    END IF;
    SELECT * INTO booking FROM trials_trialbooking WHERE id=NEW.booking_id FOR SHARE;
    IF booking.checkout_mode IS DISTINCT FROM 'GUEST' THEN
        RAISE EXCEPTION 'Guest access requires a guest booking.' USING ERRCODE='23514';
    END IF;
    IF TG_TABLE_NAME='trials_guestbookingaccessgrant' THEN
        SELECT * INTO session FROM trials_guestbrowsersession WHERE id=NEW.browser_session_id FOR SHARE;
        IF session.id IS NULL OR NEW.issued_at<session.issued_at OR NEW.expires_at>session.expires_at THEN
            RAISE EXCEPTION 'Grant must be bounded by its browser session.' USING ERRCODE='23514';
        END IF;
    ELSE
        IF NEW.token_digest !~ '^[0-9a-f]{64}$' THEN
            RAISE EXCEPTION 'Recovery requires a secret digest.' USING ERRCODE='23514';
        END IF;
        IF NEW.redeemed_at IS NOT NULL THEN
            SELECT * INTO proof FROM trials_guestrecoveryphonechallenge WHERE id=NEW.verified_challenge_id FOR SHARE;
            SELECT * INTO session FROM trials_guestbrowsersession WHERE id=NEW.redeemed_session_id FOR SHARE;
            IF proof.recovery_grant_id IS DISTINCT FROM NEW.id OR proof.consumed_at IS NULL
                OR proof.invalidated_at IS NOT NULL OR NEW.redeemed_at < proof.consumed_at
                OR NEW.revoked_at IS NOT NULL OR session.revoked_at IS NOT NULL
                OR NEW.redeemed_at >= session.expires_at THEN
                RAISE EXCEPTION 'Recovery needs fresh scoped proof and a valid session.' USING ERRCODE='23514';
            END IF;
        END IF;
    END IF;
    RETURN NEW;
END;
$$;

CREATE FUNCTION checkout_recovery_challenge() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE phone text;
BEGIN
    IF TG_OP='DELETE' THEN RAISE EXCEPTION 'Challenge evidence retained.' USING ERRCODE='55000'; END IF;
    SELECT b.contact_phone INTO phone FROM trials_trialbooking b JOIN trials_guestbookingrecoverygrant g
        ON g.booking_id=b.id WHERE g.id=NEW.recovery_grant_id FOR SHARE OF b,g;
    IF NEW.phone IS DISTINCT FROM phone THEN
        RAISE EXCEPTION 'Recovery phone mismatch.' USING ERRCODE='23514';
    END IF;
    IF TG_OP='INSERT' THEN
        IF NEW.attempts_used<>0 OR NEW.verified_at IS NOT NULL OR NEW.consumed_at IS NOT NULL OR NEW.invalidated_at IS NOT NULL THEN
            RAISE EXCEPTION 'New challenge must be unused.' USING ERRCODE='23514';
        END IF;
    ELSE
        IF (to_jsonb(OLD)-ARRAY['updated_at','attempts_used','verified_at','consumed_at','invalidated_at'])
            IS DISTINCT FROM (to_jsonb(NEW)-ARRAY['updated_at','attempts_used','verified_at','consumed_at','invalidated_at'])
            OR NEW.attempts_used<OLD.attempts_used
            OR (OLD.verified_at IS NOT NULL AND ROW(NEW.verified_at,NEW.attempts_used) IS DISTINCT FROM ROW(OLD.verified_at,OLD.attempts_used))
            OR (OLD.verified_at IS NULL AND NEW.verified_at IS NOT NULL AND NEW.attempts_used<=OLD.attempts_used)
            OR (OLD.consumed_at IS NOT NULL AND NEW.consumed_at IS DISTINCT FROM OLD.consumed_at)
            OR (OLD.invalidated_at IS NOT NULL AND NEW.invalidated_at IS DISTINCT FROM OLD.invalidated_at)
            OR ((OLD.consumed_at IS NOT NULL OR OLD.invalidated_at IS NOT NULL) AND
                ROW(NEW.attempts_used,NEW.verified_at,NEW.consumed_at,NEW.invalidated_at)
                IS DISTINCT FROM ROW(OLD.attempts_used,OLD.verified_at,OLD.consumed_at,OLD.invalidated_at)) THEN
            RAISE EXCEPTION 'Challenge binding or outcomes cannot change.' USING ERRCODE='55000';
        END IF;
    END IF;
    RETURN NEW;
END;
$$;

CREATE FUNCTION checkout_charge_binding() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE booking trials_trialbooking%ROWTYPE; collected numeric;
BEGIN
    SELECT * INTO booking FROM trials_trialbooking WHERE id=NEW.booking_id FOR UPDATE;
    IF jsonb_typeof(NEW.payee_snapshot)<>'object' OR NEW.payee_snapshot='{}'
        OR jsonb_typeof(NEW.policy_snapshot)<>'object' OR NEW.policy_snapshot='{}' THEN
        RAISE EXCEPTION 'Charge requires party and policy snapshots.' USING ERRCODE='23514';
    END IF;
    IF TG_OP='UPDATE' AND OLD.status<>NEW.status THEN
        IF NEW.status='DUE' AND (booking.status<>'VERIFIED' OR booking.verification_valid_until<=statement_timestamp()
            OR NEW.quote_expires_at<=statement_timestamp()) THEN
            RAISE EXCEPTION 'Due obligation requires a verified booking and valid quote.' USING ERRCODE='23514';
        END IF;
        SELECT COALESCE(SUM(amount_minor),0) INTO collected FROM payments_paymentallocation WHERE booking_charge_id=NEW.id;
        IF (NEW.status='COLLECTED' AND collected<>NEW.amount_minor) OR (NEW.status='VOID' AND collected<>0) THEN
            RAISE EXCEPTION 'Charge state disagrees with captured allocations.' USING ERRCODE='23514';
        END IF;
    END IF;
    RETURN NEW;
END;
$$;

CREATE FUNCTION checkout_context_binding() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE charge payments_bookingcharge%ROWTYPE; booking trials_trialbooking%ROWTYPE;
BEGIN
    SELECT * INTO charge FROM payments_bookingcharge WHERE id=NEW.booking_charge_id FOR UPDATE;
    SELECT * INTO booking FROM trials_trialbooking WHERE id=charge.booking_id FOR UPDATE;
    IF TG_OP='INSERT' AND (charge.status<>'DUE' OR booking.status<>'VERIFIED'
        OR booking.verification_valid_until<=NEW.opened_at OR charge.quote_expires_at<=NEW.opened_at
        OR NEW.opened_at>statement_timestamp() OR NEW.expires_at<=statement_timestamp()
        OR NEW.policy_snapshot='{}' OR jsonb_typeof(NEW.policy_snapshot)<>'object') THEN
        RAISE EXCEPTION 'Context requires verified intent and an accepted current obligation.' USING ERRCODE='23514';
    END IF;
    IF TG_OP='UPDATE' AND OLD.status<>NEW.status THEN
        IF NEW.status='CAPTURING' THEN
            IF OLD.expires_at<=statement_timestamp() OR NEW.capture_started_at IS NULL
                OR NEW.capture_started_at>=OLD.expires_at OR booking.status<>'PAYMENT_PENDING' THEN
                RAISE EXCEPTION 'Capture cannot begin after expiry or without payment intent.' USING ERRCODE='23514';
            END IF;
            IF NOT checkout_context_complete(NEW.id,'HELD') THEN
                RAISE EXCEPTION 'Capture needs all requested pieces reserved.' USING ERRCODE='23514';
            END IF;
        END IF;
        IF NEW.status IN ('EXPIRED','CANCELLED') THEN
            IF EXISTS (SELECT 1 FROM payments_gatewaytransaction t JOIN payments_paymentattempt a ON a.id=t.attempt_id
                WHERE a.context_id=NEW.id AND t.transaction_kind='CAPTURE')
                OR (OLD.status='REVIEW_REQUIRED' AND btrim(NEW.resolution_reference)='')
                OR EXISTS (SELECT 1 FROM payments_paymentattempt a WHERE a.context_id=NEW.id
                    AND a.provider_order_id IS NOT NULL AND btrim(NEW.resolution_reference)='') THEN
                RAISE EXCEPTION 'Reconcile provider outcomes before releasing payment context.' USING ERRCODE='23514';
            END IF;
            IF NEW.status='EXPIRED' AND NEW.closed_at<NEW.expires_at THEN
                RAISE EXCEPTION 'Context cannot expire before its deadline.' USING ERRCODE='23514';
            END IF;
        END IF;
        IF NEW.status='PAID' THEN
            IF charge.status<>'COLLECTED' OR NOT EXISTS (SELECT 1 FROM payments_paymentattempt
                WHERE context_id=NEW.id AND status='SUCCEEDED')
                OR NOT checkout_context_complete(NEW.id,'COMMITTED') THEN
                RAISE EXCEPTION 'Paid context requires collection and complete committed inventory.' USING ERRCODE='23514';
            END IF;
        END IF;
    END IF;
    RETURN NEW;
END;
$$;

CREATE FUNCTION checkout_reservation_binding() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE unit inventory_inventoryunit%ROWTYPE; context payments_paymentcontext%ROWTYPE; market bigint; city bigint;
BEGIN
    SELECT * INTO context FROM payments_paymentcontext WHERE id=NEW.payment_context_id FOR UPDATE;
    SELECT b.market_id INTO market FROM trials_trialbooking b JOIN payments_bookingcharge q ON q.booking_id=b.id
        WHERE q.id=context.booking_charge_id;
    SELECT * INTO unit FROM inventory_inventoryunit WHERE id=NEW.unit_id FOR UPDATE;
    SELECT market_id INTO city FROM markets_hub WHERE id=unit.hub_id;
    IF city IS DISTINCT FROM market THEN RAISE EXCEPTION 'Reservation city mismatch.' USING ERRCODE='23514'; END IF;
    IF TG_OP='INSERT' AND (unit.status<>'AVAILABLE' OR unit.custody<>'HUB' OR context.status<>'OPEN'
        OR NEW.held_at>=context.expires_at OR context.expires_at<=statement_timestamp()) THEN
        RAISE EXCEPTION 'Reservation requires available stock and a live context.' USING ERRCODE='23514';
    END IF;
    IF TG_OP='UPDATE' AND OLD.status<>NEW.status THEN
        IF NEW.status='CAPTURE_PENDING' AND (context.status<>'CAPTURING' OR NEW.capture_started_at IS DISTINCT FROM context.capture_started_at) THEN
            RAISE EXCEPTION 'Reservation capture must match its context.' USING ERRCODE='23514';
        END IF;
        IF NEW.status='COMMITTED' AND NOT EXISTS (SELECT 1 FROM payments_paymentattempt
            WHERE context_id=context.id AND status='SUCCEEDED') THEN
            RAISE EXCEPTION 'Commitment requires verified successful payment.' USING ERRCODE='23514';
        END IF;
        IF NEW.status IN ('EXPIRED','RELEASED') AND context.status NOT IN ('EXPIRED','CANCELLED') THEN
            RAISE EXCEPTION 'Close/reconcile context before releasing inventory.' USING ERRCODE='23514';
        END IF;
        IF OLD.status='CAPTURE_PENDING' AND NEW.status='RELEASED' AND btrim(NEW.resolution_reference)='' THEN
            RAISE EXCEPTION 'Capture uncertainty requires reconciliation evidence.' USING ERRCODE='23514';
        END IF;
    END IF;
    RETURN NEW;
END;
$$;

CREATE FUNCTION checkout_allocation_binding() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE item trials_trialboxitem%ROWTYPE; hold inventory_inventoryreservation%ROWTYPE;
DECLARE booking bigint; target bigint; variant bigint; used bigint;
BEGIN
    SELECT * INTO item FROM trials_trialboxitem WHERE id=NEW.trial_box_item_id FOR UPDATE;
    SELECT * INTO hold FROM inventory_inventoryreservation WHERE id=NEW.reservation_id FOR UPDATE;
    SELECT booking_id INTO booking FROM trials_trialbox WHERE id=item.box_id;
    SELECT q.booking_id INTO target FROM payments_bookingcharge q JOIN payments_paymentcontext c
        ON c.booking_charge_id=q.id WHERE c.id=hold.payment_context_id;
    SELECT variant_id INTO variant FROM inventory_inventoryunit WHERE id=hold.unit_id;
    IF booking IS DISTINCT FROM target OR item.variant_id IS DISTINCT FROM variant THEN
        RAISE EXCEPTION 'Physical allocation does not match requested booking/variant.' USING ERRCODE='23514';
    END IF;
    IF TG_OP='INSERT' THEN
        SELECT COUNT(*) INTO used FROM trials_trialitemallocation a JOIN inventory_inventoryreservation r
            ON r.id=a.reservation_id WHERE a.trial_box_item_id=item.id AND a.released_at IS NULL
            AND r.status IN ('HELD','CAPTURE_PENDING','COMMITTED');
        IF hold.status<>'HELD' OR used>=item.quantity OR NEW.released_at IS NOT NULL THEN
            RAISE EXCEPTION 'Allocation exceeds requested quantity or targets closed stock.' USING ERRCODE='23514';
        END IF;
    ELSIF NEW.released_at IS NOT NULL AND hold.status NOT IN ('EXPIRED','RELEASED') THEN
        RAISE EXCEPTION 'Release reservation before allocation.' USING ERRCODE='23514';
    END IF;
    RETURN NEW;
END;
$$;

CREATE FUNCTION checkout_attempt_binding() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE context payments_paymentcontext%ROWTYPE; charge payments_bookingcharge%ROWTYPE;
BEGIN
    SELECT * INTO context FROM payments_paymentcontext WHERE id=NEW.context_id FOR UPDATE;
    SELECT * INTO charge FROM payments_bookingcharge WHERE id=context.booking_charge_id FOR UPDATE;
    IF ROW(NEW.amount_minor,NEW.currency) IS DISTINCT FROM ROW(charge.amount_minor,charge.currency)
        OR btrim(NEW.provider)='' OR btrim(NEW.provider_account)='' OR NEW.request_fingerprint !~ '^[0-9a-f]{64}$' THEN
        RAISE EXCEPTION 'Provider attempt must bind the exact obligation.' USING ERRCODE='23514';
    END IF;
    IF TG_OP='INSERT' AND (context.status<>'OPEN' OR NOT checkout_context_complete(context.id,'HELD')) THEN
        RAISE EXCEPTION 'Attempt requires an open context.' USING ERRCODE='23514';
    END IF;
    IF NEW.status IN ('PENDING','AUTHORIZED','CAPTURE_PENDING','SUCCEEDED') AND
        (NEW.provider_order_id IS NULL OR btrim(NEW.provider_order_id)='') THEN
        RAISE EXCEPTION 'Provider order identity required.' USING ERRCODE='23514';
    END IF;
    IF TG_OP='UPDATE' THEN
        IF OLD.last_verified_at IS NOT NULL AND (NEW.last_verified_at IS NULL OR NEW.last_verified_at<OLD.last_verified_at) THEN
            RAISE EXCEPTION 'Provider observation cannot move backwards.' USING ERRCODE='23514';
        END IF;
        IF OLD.status<>NEW.status AND NEW.status='AUTHORIZED' AND NOT EXISTS (
            SELECT 1 FROM payments_gatewaytransaction WHERE attempt_id=NEW.id AND transaction_kind='AUTHORISATION'
                AND amount_minor=NEW.amount_minor AND currency=NEW.currency) THEN
            RAISE EXCEPTION 'Authorization requires verified provider evidence.' USING ERRCODE='23514';
        END IF;
        IF OLD.status<>NEW.status AND NEW.status='CAPTURE_PENDING' AND context.status<>'CAPTURING' THEN
            RAISE EXCEPTION 'Capture requires a protected context.' USING ERRCODE='23514';
        END IF;
        IF OLD.status<>NEW.status AND NEW.status='SUCCEEDED' AND (
            context.status NOT IN ('CAPTURING','REVIEW_REQUIRED') OR NEW.last_verified_at IS NULL OR NOT EXISTS (
                SELECT 1 FROM payments_gatewaytransaction t JOIN payments_paymentallocation a ON a.gateway_transaction_id=t.id
                WHERE t.attempt_id=NEW.id AND t.transaction_kind='CAPTURE' AND a.booking_charge_id=charge.id
                    AND a.amount_minor=charge.amount_minor AND a.currency=charge.currency)) THEN
            RAISE EXCEPTION 'Success requires captured and allocated evidence.' USING ERRCODE='23514';
        END IF;
    END IF;
    RETURN NEW;
END;
$$;

CREATE FUNCTION checkout_receipt_binding() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE attempt payments_paymentattempt%ROWTYPE; event payments_gatewayevent%ROWTYPE;
BEGIN
    SELECT * INTO attempt FROM payments_paymentattempt WHERE id=NEW.attempt_id FOR SHARE;
    IF ROW(NEW.provider,NEW.provider_account,NEW.environment) IS DISTINCT FROM
        ROW(attempt.provider,attempt.provider_account,attempt.environment) OR btrim(NEW.provider_payment_id)=''
        OR btrim(NEW.verification_reference)='' OR NEW.verified_at<NEW.occurred_at THEN
        RAISE EXCEPTION 'Receipt provider or verification mismatch.' USING ERRCODE='23514';
    END IF;
    IF NEW.source_event_id IS NOT NULL THEN
        SELECT * INTO event FROM payments_gatewayevent WHERE id=NEW.source_event_id;
        IF ROW(event.provider,event.provider_account,event.environment) IS DISTINCT FROM
            ROW(NEW.provider,NEW.provider_account,NEW.environment) THEN
            RAISE EXCEPTION 'Receipt source event mismatch.' USING ERRCODE='23514';
        END IF;
    END IF;
    RETURN NEW;
END;
$$;

CREATE FUNCTION checkout_payment_allocation() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE receipt payments_gatewaytransaction%ROWTYPE; charge payments_bookingcharge%ROWTYPE;
DECLARE context payments_paymentcontext%ROWTYPE; allocated numeric; applied numeric;
BEGIN
    -- Lock the obligation and receipt before summing; distinct callbacks cannot overspend either.
    SELECT * INTO charge FROM payments_bookingcharge WHERE id=NEW.booking_charge_id FOR UPDATE;
    SELECT * INTO receipt FROM payments_gatewaytransaction WHERE id=NEW.gateway_transaction_id FOR UPDATE;
    SELECT c.* INTO context FROM payments_paymentcontext c JOIN payments_paymentattempt a ON a.context_id=c.id
        WHERE a.id=receipt.attempt_id FOR UPDATE OF c;
    IF receipt.transaction_kind<>'CAPTURE' OR receipt.currency<>NEW.currency OR charge.currency<>NEW.currency
        OR context.booking_charge_id IS DISTINCT FROM charge.id OR charge.status<>'DUE'
        OR context.status NOT IN ('CAPTURING','REVIEW_REQUIRED') THEN
        RAISE EXCEPTION 'Only captured funds for this live obligation can be allocated.' USING ERRCODE='23514';
    END IF;
    SELECT COALESCE(SUM(amount_minor),0) INTO allocated FROM payments_paymentallocation WHERE gateway_transaction_id=receipt.id;
    SELECT COALESCE(SUM(amount_minor),0) INTO applied FROM payments_paymentallocation WHERE booking_charge_id=charge.id;
    IF allocated+NEW.amount_minor>receipt.amount_minor OR applied+NEW.amount_minor>charge.amount_minor THEN
        RAISE EXCEPTION 'Allocation exceeds captured receipt or obligation.' USING ERRCODE='23514';
    END IF;
    RETURN NEW;
END;
$$;

CREATE FUNCTION checkout_request_binding() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE booking trials_trialbooking%ROWTYPE; target bigint;
BEGIN
    IF NEW.request_fingerprint !~ '^[0-9a-f]{64}$' OR btrim(NEW.scope)='' THEN
        RAISE EXCEPTION 'Scoped request fingerprint required.' USING ERRCODE='23514';
    END IF;
    IF NEW.booking_id IS NOT NULL THEN
        SELECT * INTO booking FROM trials_trialbooking WHERE id=NEW.booking_id;
        IF (NEW.guest_session_id IS NOT NULL AND (booking.checkout_mode<>'GUEST' OR NOT EXISTS (
            SELECT 1 FROM trials_guestbookingaccessgrant WHERE booking_id=booking.id AND browser_session_id=NEW.guest_session_id)))
            OR (NEW.customer_account_id IS NOT NULL AND
                (booking.checkout_mode<>'ACCOUNT' OR booking.customer_account_id IS DISTINCT FROM NEW.customer_account_id)) THEN
            RAISE EXCEPTION 'Request result cannot cross guest/account ownership.' USING ERRCODE='23514';
        END IF;
    END IF;
    IF NEW.payment_attempt_id IS NOT NULL THEN
        SELECT q.booking_id INTO target FROM payments_bookingcharge q JOIN payments_paymentcontext c ON c.booking_charge_id=q.id
            JOIN payments_paymentattempt a ON a.context_id=c.id WHERE a.id=NEW.payment_attempt_id;
        IF target IS DISTINCT FROM NEW.booking_id THEN
            RAISE EXCEPTION 'Request result payment does not belong to booking.' USING ERRCODE='23514';
        END IF;
    END IF;
    RETURN NEW;
END;
$$;

CREATE FUNCTION checkout_notification_binding() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE booking trials_trialbooking%ROWTYPE; target bigint;
BEGIN
    SELECT * INTO booking FROM trials_trialbooking WHERE id=NEW.booking_id;
    IF NEW.recipient_phone IS DISTINCT FROM booking.contact_phone THEN
        RAISE EXCEPTION 'Notification phone must match booking snapshot.' USING ERRCODE='23514';
    END IF;
    IF NEW.purpose='BOOKING_PHONE_OTP' THEN
        SELECT booking_id INTO target FROM trials_bookingphonechallenge WHERE id=NEW.booking_challenge_id;
    ELSIF NEW.purpose='GUEST_RECOVERY_OTP' THEN
        SELECT g.booking_id INTO target FROM trials_guestbookingrecoverygrant g JOIN trials_guestrecoveryphonechallenge c
            ON c.recovery_grant_id=g.id WHERE c.id=NEW.recovery_challenge_id;
    ELSIF NEW.purpose='ARRIVAL_OTP' THEN
        SELECT a.booking_id INTO target FROM delivery_deliveryassignment a JOIN delivery_arrivalotp c ON c.assignment_id=a.id
            WHERE c.id=NEW.arrival_challenge_id;
    ELSE
        target := NEW.booking_id;
        IF booking.status<>'CONFIRMED' THEN
            RAISE EXCEPTION 'Confirmation message requires a confirmed booking.' USING ERRCODE='23514';
        END IF;
    END IF;
    IF target IS DISTINCT FROM NEW.booking_id OR btrim(NEW.template_reference)='' THEN
        RAISE EXCEPTION 'Notification purpose binding invalid.' USING ERRCODE='23514';
    END IF;
    RETURN NEW;
END;
$$;

CREATE FUNCTION checkout_outbox_binding() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP='UPDATE' AND NEW.attempts<OLD.attempts THEN
        RAISE EXCEPTION 'Outbox attempts cannot decrease.' USING ERRCODE='23514';
    END IF;
    IF NEW.operation='CAPTURE_PAYMENT' AND TG_OP='INSERT' AND NOT EXISTS (
        SELECT 1 FROM payments_paymentattempt WHERE id=NEW.payment_attempt_id AND status='CAPTURE_PENDING') THEN
        RAISE EXCEPTION 'Capture command requires a capture-pending attempt.' USING ERRCODE='23514';
    END IF;
    RETURN NEW;
END;
$$;
"""

COMPLETENESS_SQL = r"""
CREATE FUNCTION checkout_context_complete(context_id bigint, required_status text) RETURNS boolean
LANGUAGE sql STABLE AS $$
    SELECT EXISTS (SELECT 1 FROM inventory_inventoryreservation WHERE payment_context_id=context_id)
    AND NOT EXISTS (
        SELECT 1 FROM trials_trialboxitem i JOIN trials_trialbox b ON b.id=i.box_id
        JOIN payments_bookingcharge q ON q.booking_id=b.booking_id
        JOIN payments_paymentcontext c ON c.booking_charge_id=q.id
        WHERE c.id=context_id AND i.quantity<>(
            SELECT COUNT(*) FROM trials_trialitemallocation a JOIN inventory_inventoryreservation r
            ON r.id=a.reservation_id WHERE a.trial_box_item_id=i.id AND a.released_at IS NULL
                AND r.payment_context_id=context_id AND r.status=required_status))
    AND NOT EXISTS (
        SELECT 1 FROM inventory_inventoryreservation r WHERE r.payment_context_id=context_id AND (
            r.status<>required_status OR NOT EXISTS (
                SELECT 1 FROM trials_trialitemallocation a WHERE a.reservation_id=r.id AND a.released_at IS NULL)))
$$;
CREATE FUNCTION checkout_protect_reserved_unit() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF ROW(OLD.variant_id,OLD.hub_id,OLD.status,OLD.custody) IS DISTINCT FROM
        ROW(NEW.variant_id,NEW.hub_id,NEW.status,NEW.custody) AND EXISTS (
            SELECT 1 FROM inventory_inventoryreservation WHERE unit_id=NEW.id
                AND status IN ('HELD','CAPTURE_PENDING','COMMITTED')) THEN
        RAISE EXCEPTION 'Reserved stock cannot change physical identity or disposition.' USING ERRCODE='23514';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER checkout_reserved_unit BEFORE UPDATE ON inventory_inventoryunit
    FOR EACH ROW EXECUTE FUNCTION checkout_protect_reserved_unit();
"""

BINDINGS = {
    "trials_trialbooking": "checkout_booking_binding",
    "trials_trialbox": "checkout_selection_binding",
    "trials_trialboxitem": "checkout_selection_binding",
    "trials_guestbrowsersession": "checkout_guest_binding",
    "trials_guestbookingaccessgrant": "checkout_guest_binding",
    "trials_guestbookingrecoverygrant": "checkout_guest_binding",
    "trials_guestrecoveryphonechallenge": "checkout_recovery_challenge",
    "payments_bookingcharge": "checkout_charge_binding",
    "payments_paymentcontext": "checkout_context_binding",
    "inventory_inventoryreservation": "checkout_reservation_binding",
    "trials_trialitemallocation": "checkout_allocation_binding",
    "payments_paymentattempt": "checkout_attempt_binding",
    "payments_gatewaytransaction": "checkout_receipt_binding",
    "payments_paymentallocation": "checkout_payment_allocation",
    "trials_idempotencyrecord": "checkout_request_binding",
    "notifications_notificationevent": "checkout_notification_binding",
    "notifications_outboxcommand": "checkout_outbox_binding",
}


def install_guards(apps, schema_editor):
    schema_editor.execute(COMPLETENESS_SQL, params=None)
    schema_editor.execute(SQL, params=None)
    for table, (mutable, once, initial, transitions) in SPECS.items():
        args = [mutable, once, initial, json.dumps(transitions)]
        arguments = ",".join("'" + value.replace("'", "''") + "'" for value in args)
        schema_editor.execute(f'CREATE TRIGGER checkout_identity BEFORE INSERT OR UPDATE OR DELETE ON "{table}" '
                              f'FOR EACH ROW EXECUTE FUNCTION checkout_guard_record({arguments})')
    for table in APPEND_ONLY:
        schema_editor.execute(f'CREATE TRIGGER checkout_history BEFORE UPDATE OR DELETE ON "{table}" '
                              'FOR EACH ROW EXECUTE FUNCTION trials_reject_evidence_mutation()')
    for table, function in BINDINGS.items():
        operation = "INSERT" if table in APPEND_ONLY or table == "trials_trialbox" else "INSERT OR UPDATE"
        if table == "trials_guestrecoveryphonechallenge":
            operation += " OR DELETE"
        schema_editor.execute(f'CREATE TRIGGER checkout_binding BEFORE {operation} ON "{table}" '
                              f'FOR EACH ROW EXECUTE FUNCTION {function}()')


def remove_guards(apps, schema_editor):
    for table in SPECS:
        schema_editor.execute(f'DROP TRIGGER checkout_identity ON "{table}"')
    for table in APPEND_ONLY:
        schema_editor.execute(f'DROP TRIGGER checkout_history ON "{table}"')
    for table in BINDINGS:
        schema_editor.execute(f'DROP TRIGGER checkout_binding ON "{table}"')
    for function in {*BINDINGS.values(), "checkout_guard_record"}:
        schema_editor.execute(f'DROP FUNCTION {function}()')
    schema_editor.execute('DROP TRIGGER checkout_reserved_unit ON inventory_inventoryunit')
    schema_editor.execute('DROP FUNCTION checkout_protect_reserved_unit()')
    schema_editor.execute('DROP FUNCTION checkout_context_complete(bigint,text)')


class Migration(migrations.Migration):
    dependencies = [
        ("trials", "0003_guestbookingrecoverygrant_guestbrowsersession_and_more"),
        ("inventory", "0006_inventoryreservation_payment_context_and_more"),
        ("notifications", "0002_initial"),
    ]
    operations = [
        migrations.RunSQL(
            "DROP TRIGGER guest_access_grant_guard ON trials_guestbookingaccessgrant; "
            "DROP TRIGGER booking_identity_guard ON trials_trialbooking;",
            "CREATE TRIGGER guest_access_grant_guard BEFORE INSERT OR UPDATE OR DELETE ON trials_guestbookingaccessgrant "
            "FOR EACH ROW EXECUTE FUNCTION trials_guard_guest_grant(); "
            "CREATE TRIGGER booking_identity_guard BEFORE INSERT OR UPDATE OR DELETE ON trials_trialbooking "
            "FOR EACH ROW EXECUTE FUNCTION trials_guard_booking();",
        ),
        migrations.RunPython(preserve_credentials, migrations.RunPython.noop),
        migrations.AlterField(model_name="guestbookingaccessgrant", name="browser_session",
            field=models.ForeignKey(on_delete=django.db.models.deletion.PROTECT,
                                    related_name="booking_grants", to="trials.guestbrowsersession")),
        migrations.RemoveField(model_name="guestbookingaccessgrant", name="token_digest"),
        migrations.RunPython(install_guards, remove_guards),
        migrations.RunPython(migrations.RunPython.noop, require_empty),
    ]
