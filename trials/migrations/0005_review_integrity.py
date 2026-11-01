"""Additive corrections; retained receipts/allocations are never rewritten.

Refs: R-14, R-24, R-26, R-28, R-30, R-39, R-40; D-27.
The coarse transaction latch precedes implicit UPDATE locks. READ COMMITTED is
required so waiting guard queries observe committed reconciliation evidence.
"""
from django.db import migrations
from django.db.migrations.exceptions import IrreversibleError

SQL = r"""

CREATE FUNCTION checkout_serialize_writes() RETURNS void LANGUAGE plpgsql AS $$
BEGIN
    IF current_setting('transaction_isolation') <> 'read committed' THEN
        RAISE EXCEPTION 'Integrity writes require READ COMMITTED isolation.' USING ERRCODE='25000';
    END IF;
    PERFORM pg_advisory_xact_lock(1095912257,40);
END; $$;
CREATE FUNCTION checkout_statement_lock() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN PERFORM checkout_serialize_writes(); RETURN NULL; END; $$;

CREATE FUNCTION checkout_unit_active(unit_id bigint) RETURNS boolean LANGUAGE sql VOLATILE AS $$
    SELECT h.status='ACTIVE' AND m.status='ACTIVE' AND v.status='ACTIVE' AND p.status='ACTIVE'
        AND mat.status='ACTIVE' AND cat.status='ACTIVE' AND (v.purity_id IS NULL OR pur.status='ACTIVE')
    FROM inventory_inventoryunit u JOIN markets_hub h ON h.id=u.hub_id JOIN markets_market m ON m.id=h.market_id
    JOIN catalog_productvariant v ON v.id=u.variant_id JOIN catalog_product p ON p.id=v.product_id
    JOIN catalog_material mat ON mat.id=v.material_id JOIN catalog_category cat ON cat.id=p.category_id
    LEFT JOIN catalog_purity pur ON pur.id=v.purity_id WHERE u.id=unit_id
$$;

CREATE FUNCTION checkout_resolution_current(target_resolution bigint, target_context bigint) RETURNS boolean LANGUAGE sql VOLATILE AS $$
    SELECT EXISTS (SELECT 1 FROM payments_paymentresolution r JOIN payments_paymentcontext c ON c.id=r.context_id
        JOIN payments_bookingcharge q ON q.id=c.booking_charge_id WHERE r.id=target_resolution AND r.context_id=target_context
        AND r.currency=q.currency AND r.captured_minor=(SELECT COALESCE(SUM(t.amount_minor),0)
            FROM payments_gatewaytransaction t JOIN payments_paymentattempt a ON a.id=COALESCE(t.attempt_id,
                (SELECT attempt_id FROM payments_gatewaytransactionmatch WHERE gateway_transaction_id=t.id))
            WHERE a.context_id=target_context AND t.transaction_kind='CAPTURE')
        AND r.allocated_minor=(SELECT COALESCE(SUM(x.amount_minor),0) FROM payments_paymentallocation x
            JOIN payments_gatewaytransaction t ON t.id=x.gateway_transaction_id
            JOIN payments_paymentattempt a ON a.id=COALESCE(t.attempt_id,
                (SELECT attempt_id FROM payments_gatewaytransactionmatch WHERE gateway_transaction_id=t.id))
            WHERE a.context_id=target_context)
        AND NOT EXISTS (SELECT 1 FROM payments_gatewaytransaction t JOIN payments_paymentattempt a
            ON a.id=COALESCE(t.attempt_id,(SELECT attempt_id FROM payments_gatewaytransactionmatch WHERE gateway_transaction_id=t.id))
            WHERE a.context_id=target_context AND t.recorded_at>r.recorded_at)
        AND NOT EXISTS (SELECT 1 FROM payments_paymentattempt a WHERE a.context_id=target_context
            AND (a.last_verified_at IS NULL OR a.last_verified_at>r.reconciled_at OR a.last_verified_at<r.reconciled_at
                OR a.status NOT IN ('FAILED','EXPIRED','REVIEW_REQUIRED'))))
$$;

CREATE FUNCTION checkout_resolution_binding() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF btrim(NEW.reason)='' OR btrim(NEW.evidence_reference)='' OR
        (NEW.staff_actor_id IS NULL AND btrim(NEW.system_actor)='') OR NEW.reconciled_at>statement_timestamp() THEN
        RAISE EXCEPTION 'Reconciliation requires attributable observed evidence.' USING ERRCODE='23514'; END IF;
    RETURN NEW;
END; $$;

CREATE FUNCTION checkout_match_binding() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE receipt payments_gatewaytransaction%ROWTYPE; attempt payments_paymentattempt%ROWTYPE;
BEGIN
    SELECT * INTO receipt FROM payments_gatewaytransaction WHERE id=NEW.gateway_transaction_id FOR UPDATE;
    SELECT * INTO attempt FROM payments_paymentattempt WHERE id=NEW.attempt_id FOR UPDATE;
    IF receipt.attempt_id IS NOT NULL OR ROW(receipt.provider,receipt.provider_account,receipt.environment,receipt.currency)
        IS DISTINCT FROM ROW(attempt.provider,attempt.provider_account,attempt.environment,attempt.currency)
        OR NEW.matched_at<receipt.verified_at OR NEW.matched_at>statement_timestamp()
        OR btrim(NEW.reason)='' OR btrim(NEW.evidence_reference)=''
        OR (NEW.staff_actor_id IS NULL AND btrim(NEW.system_actor)='') THEN
        RAISE EXCEPTION 'Attributable receipt matching must preserve provider/account/currency identity.' USING ERRCODE='23514'; END IF;
    RETURN NEW;
END; $$;

CREATE FUNCTION checkout_policy_binding() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE policy compliance_businessconfiguration%ROWTYPE; market_code text; category_code text; ns text; policy_key text;
BEGIN
    SELECT * INTO policy FROM compliance_businessconfiguration WHERE id=NEW.policy_revision_id FOR SHARE;
    IF TG_TABLE_NAME='trials_trialplanrevision' THEN
        ns:='trials'; policy_key:='plan-policy-reference';
        SELECT code INTO market_code FROM markets_market WHERE id=NEW.market_id;
        SELECT code INTO category_code FROM catalog_category WHERE id=NEW.category_id;
    ELSE
        ns:='payments'; policy_key:='booking-charge-policy-reference';
        SELECT m.code INTO market_code FROM markets_market m JOIN trials_trialbooking b ON b.market_id=m.id WHERE b.id=NEW.booking_id;
    END IF;
    IF policy.namespace IS DISTINCT FROM ns OR policy.key IS DISTINCT FROM policy_key OR jsonb_typeof(policy.scope)<>'object'
        OR EXISTS (SELECT 1 FROM jsonb_each_text(policy.scope) d WHERE
            d.value IS DISTINCT FROM CASE d.key WHEN 'market' THEN market_code WHEN 'category' THEN category_code END) THEN
        RAISE EXCEPTION 'Policy identity or explicit scope mismatch.' USING ERRCODE='23514'; END IF;
    RETURN NEW;
END; $$;
CREATE OR REPLACE FUNCTION checkout_booking_binding() RETURNS trigger LANGUAGE plpgsql AS $$
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
        IF NEW.status IN ('CANCELLED','PAYMENT_EXPIRED') AND (
            EXISTS (SELECT 1 FROM payments_bookingcharge q WHERE q.booking_id=NEW.id AND q.status IN ('DUE','COLLECTED'))
            OR EXISTS (SELECT 1 FROM payments_paymentcontext c JOIN payments_bookingcharge q ON q.id=c.booking_charge_id
                WHERE q.booking_id=NEW.id AND c.status NOT IN ('CANCELLED','EXPIRED'))
            OR EXISTS (SELECT 1 FROM inventory_inventoryreservation r JOIN payments_paymentcontext c ON c.id=r.payment_context_id
                JOIN payments_bookingcharge q ON q.id=c.booking_charge_id WHERE q.booking_id=NEW.id
                AND r.status IN ('HELD','CAPTURE_PENDING','COMMITTED'))) THEN
            RAISE EXCEPTION 'Resolve payment contexts, charges and stock before terminating booking.' USING ERRCODE='23514';
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
CREATE OR REPLACE FUNCTION checkout_guest_binding() RETURNS trigger LANGUAGE plpgsql AS $$
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
        IF session.id IS NULL OR (TG_OP='INSERT' AND (session.revoked_at IS NOT NULL OR session.expires_at<=statement_timestamp() OR NEW.expires_at<=statement_timestamp())) OR NEW.issued_at<session.issued_at OR NEW.expires_at>session.expires_at THEN
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
CREATE OR REPLACE FUNCTION checkout_charge_binding() RETURNS trigger LANGUAGE plpgsql AS $$
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
        IF NEW.status='COLLECTED' AND booking.status<>'PAYMENT_PENDING' THEN
            RAISE EXCEPTION 'Terminal booking cannot accept funds.' USING ERRCODE='23514'; END IF;
        IF NEW.status='VOID' AND EXISTS (SELECT 1 FROM payments_paymentcontext c WHERE c.booking_charge_id=NEW.id
            AND c.status NOT IN ('CANCELLED','EXPIRED')) THEN
            RAISE EXCEPTION 'Close reconciled contexts before voiding obligation.' USING ERRCODE='23514'; END IF;
        IF (NEW.status='COLLECTED' AND collected<>NEW.amount_minor) OR (NEW.status='VOID' AND collected<>0 AND NOT EXISTS (
            SELECT 1 FROM payments_paymentcontext c JOIN payments_paymentresolution r ON r.id=c.resolution_id
            WHERE c.booking_charge_id=NEW.id AND c.status='CANCELLED' AND r.outcome='CAPTURE_NOT_ACCEPTED'
                AND r.allocated_minor=collected)) THEN
            RAISE EXCEPTION 'Charge state disagrees with captured allocations.' USING ERRCODE='23514';
        END IF;
    END IF;
    RETURN NEW;
END;
$$;
CREATE OR REPLACE FUNCTION checkout_context_binding() RETURNS trigger LANGUAGE plpgsql AS $$
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
            IF (NEW.resolution_id IS NULL AND EXISTS (SELECT 1 FROM payments_gatewaytransaction t JOIN payments_paymentattempt a ON a.id=COALESCE(t.attempt_id,(SELECT attempt_id FROM payments_gatewaytransactionmatch WHERE gateway_transaction_id=t.id))
                WHERE a.context_id=NEW.id AND t.transaction_kind='CAPTURE'))
                OR (OLD.status='REVIEW_REQUIRED' AND NEW.resolution_id IS NULL)
                OR EXISTS (SELECT 1 FROM payments_paymentattempt a WHERE a.context_id=NEW.id
                    AND a.provider_order_id IS NOT NULL AND NEW.resolution_id IS NULL) THEN
                RAISE EXCEPTION 'Reconcile provider outcomes before releasing payment context.' USING ERRCODE='23514';
            END IF;
            IF NEW.resolution_id IS NOT NULL AND NOT checkout_resolution_current(NEW.resolution_id, NEW.id) THEN
                RAISE EXCEPTION 'Resolution does not reconcile current context evidence.' USING ERRCODE='23514'; END IF;
            IF NEW.status='EXPIRED' AND NEW.closed_at<NEW.expires_at THEN
                RAISE EXCEPTION 'Context cannot expire before its deadline.' USING ERRCODE='23514';
            END IF;
        END IF;
        IF NEW.status='PAID' THEN
            IF booking.status<>'PAYMENT_PENDING' OR charge.status<>'COLLECTED' OR NOT EXISTS (SELECT 1 FROM payments_paymentattempt
                WHERE context_id=NEW.id AND status='SUCCEEDED')
                OR NOT checkout_context_complete(NEW.id,'COMMITTED') THEN
                RAISE EXCEPTION 'Paid context requires collection and complete committed inventory.' USING ERRCODE='23514';
            END IF;
        END IF;
    END IF;
    RETURN NEW;
END;
$$;
CREATE OR REPLACE FUNCTION checkout_reservation_binding() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE unit inventory_inventoryunit%ROWTYPE; context payments_paymentcontext%ROWTYPE; market bigint; city bigint;
BEGIN
    SELECT * INTO context FROM payments_paymentcontext WHERE id=NEW.payment_context_id FOR UPDATE;
    SELECT b.market_id INTO market FROM trials_trialbooking b JOIN payments_bookingcharge q ON q.booking_id=b.id
        WHERE q.id=context.booking_charge_id;
    SELECT * INTO unit FROM inventory_inventoryunit WHERE id=NEW.unit_id FOR UPDATE;
    SELECT market_id INTO city FROM markets_hub WHERE id=unit.hub_id;
    IF city IS DISTINCT FROM market THEN RAISE EXCEPTION 'Reservation city mismatch.' USING ERRCODE='23514'; END IF;
    IF (TG_OP='INSERT' OR (TG_OP='UPDATE' AND OLD.status<>NEW.status AND NEW.status='COMMITTED')) AND NOT checkout_unit_active(NEW.unit_id) THEN RAISE EXCEPTION 'Commitments require active catalogue and operating references.' USING ERRCODE='23514'; END IF;
    IF TG_OP='INSERT' AND (unit.status<>'AVAILABLE' OR unit.custody<>'HUB' OR context.status<>'OPEN'
        OR NEW.held_at>=context.expires_at OR context.expires_at<=statement_timestamp()) THEN
        RAISE EXCEPTION 'Reservation requires available stock and a live context.' USING ERRCODE='23514';
    END IF;
    IF TG_OP='UPDATE' AND OLD.status<>NEW.status THEN
        IF NEW.status='CAPTURE_PENDING' AND (context.status<>'CAPTURING' OR NEW.capture_started_at IS DISTINCT FROM context.capture_started_at) THEN
            RAISE EXCEPTION 'Reservation capture must match its context.' USING ERRCODE='23514';
        END IF;
        IF NEW.status='COMMITTED' AND (NOT EXISTS (SELECT 1 FROM trials_trialbooking b JOIN payments_bookingcharge q ON q.booking_id=b.id
            WHERE q.id=context.booking_charge_id AND b.status='PAYMENT_PENDING' AND q.status='DUE') OR NOT EXISTS (SELECT 1 FROM payments_paymentattempt
            WHERE context_id=context.id AND status='SUCCEEDED')) THEN
            RAISE EXCEPTION 'Commitment requires verified successful payment.' USING ERRCODE='23514';
        END IF;
        IF NEW.status IN ('EXPIRED','RELEASED') AND context.status NOT IN ('EXPIRED','CANCELLED') THEN
            RAISE EXCEPTION 'Close/reconcile context before releasing inventory.' USING ERRCODE='23514';
        END IF;
        IF OLD.status<>'COMMITTED' AND NEW.status IN ('EXPIRED','RELEASED') AND (
            OLD.status='CAPTURE_PENDING' OR context.resolution_id IS NOT NULL OR EXISTS (
                SELECT 1 FROM payments_paymentattempt WHERE context_id=context.id AND provider_order_id IS NOT NULL))
            AND (NEW.resolution_id IS NULL OR NOT checkout_resolution_current(NEW.resolution_id,context.id)) THEN
            RAISE EXCEPTION 'Capture uncertainty requires reconciliation evidence.' USING ERRCODE='23514';
        END IF;
    END IF;
    RETURN NEW;
END;
$$;
CREATE OR REPLACE FUNCTION checkout_attempt_binding() RETURNS trigger LANGUAGE plpgsql AS $$
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
            SELECT 1 FROM payments_gatewaytransaction WHERE COALESCE(attempt_id,(SELECT attempt_id FROM payments_gatewaytransactionmatch WHERE gateway_transaction_id=payments_gatewaytransaction.id))=NEW.id AND transaction_kind='AUTHORISATION'
                AND amount_minor=NEW.amount_minor AND currency=NEW.currency) THEN
            RAISE EXCEPTION 'Authorization requires verified provider evidence.' USING ERRCODE='23514';
        END IF;
        IF OLD.status<>NEW.status AND NEW.status='CAPTURE_PENDING' AND (context.status<>'CAPTURING' OR charge.status<>'DUE') THEN
            RAISE EXCEPTION 'Capture requires a protected context.' USING ERRCODE='23514';
        END IF;
        IF OLD.status<>NEW.status AND NEW.status='SUCCEEDED' AND (
            context.status NOT IN ('CAPTURING','REVIEW_REQUIRED') OR charge.status<>'DUE'
            OR NOT EXISTS (SELECT 1 FROM trials_trialbooking WHERE id=charge.booking_id AND status='PAYMENT_PENDING') OR NEW.last_verified_at IS NULL OR NOT EXISTS (
                SELECT 1 FROM payments_gatewaytransaction t JOIN payments_paymentallocation a ON a.gateway_transaction_id=t.id
                WHERE COALESCE(t.attempt_id,(SELECT attempt_id FROM payments_gatewaytransactionmatch WHERE gateway_transaction_id=t.id))=NEW.id AND t.transaction_kind='CAPTURE' AND a.booking_charge_id=charge.id
                    AND a.amount_minor=charge.amount_minor AND a.currency=charge.currency)) THEN
            RAISE EXCEPTION 'Success requires captured and allocated evidence.' USING ERRCODE='23514';
        END IF;
    END IF;
    RETURN NEW;
END;
$$;
CREATE OR REPLACE FUNCTION checkout_receipt_binding() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE attempt payments_paymentattempt%ROWTYPE; event payments_gatewayevent%ROWTYPE;
BEGIN
    SELECT * INTO attempt FROM payments_paymentattempt WHERE id=NEW.attempt_id FOR SHARE;
    IF (NEW.attempt_id IS NOT NULL AND ROW(NEW.provider,NEW.provider_account,NEW.environment) IS DISTINCT FROM
        ROW(attempt.provider,attempt.provider_account,attempt.environment)) OR btrim(NEW.provider)='' OR btrim(NEW.provider_account)='' OR btrim(NEW.provider_payment_id)=''
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
CREATE OR REPLACE FUNCTION checkout_payment_allocation() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE receipt payments_gatewaytransaction%ROWTYPE; charge payments_bookingcharge%ROWTYPE;
DECLARE context payments_paymentcontext%ROWTYPE; allocated numeric; applied numeric;
BEGIN
    -- Lock the obligation and receipt before summing; distinct callbacks cannot overspend either.
    SELECT * INTO charge FROM payments_bookingcharge WHERE id=NEW.booking_charge_id FOR UPDATE;
    SELECT * INTO receipt FROM payments_gatewaytransaction WHERE id=NEW.gateway_transaction_id FOR UPDATE;
    SELECT c.* INTO context FROM payments_paymentcontext c JOIN payments_paymentattempt a ON a.context_id=c.id
        WHERE a.id=COALESCE(receipt.attempt_id,(SELECT attempt_id FROM payments_gatewaytransactionmatch WHERE gateway_transaction_id=receipt.id)) FOR UPDATE OF c;
    IF receipt.transaction_kind<>'CAPTURE' OR receipt.currency<>NEW.currency OR charge.currency<>NEW.currency
        OR context.booking_charge_id IS DISTINCT FROM charge.id OR charge.status<>'DUE'
        OR context.status NOT IN ('CAPTURING','REVIEW_REQUIRED') OR NOT EXISTS (
            SELECT 1 FROM trials_trialbooking WHERE id=charge.booking_id AND status='PAYMENT_PENDING') THEN
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
CREATE TRIGGER checkout_serialization BEFORE INSERT OR UPDATE OR DELETE ON trials_trialbooking FOR EACH STATEMENT EXECUTE FUNCTION checkout_statement_lock();
CREATE TRIGGER checkout_serialization BEFORE INSERT OR UPDATE OR DELETE ON trials_guestbrowsersession FOR EACH STATEMENT EXECUTE FUNCTION checkout_statement_lock();
CREATE TRIGGER checkout_serialization BEFORE INSERT OR UPDATE OR DELETE ON trials_guestbookingaccessgrant FOR EACH STATEMENT EXECUTE FUNCTION checkout_statement_lock();
CREATE TRIGGER checkout_serialization BEFORE INSERT OR UPDATE OR DELETE ON trials_trialbox FOR EACH STATEMENT EXECUTE FUNCTION checkout_statement_lock();
CREATE TRIGGER checkout_serialization BEFORE INSERT OR UPDATE OR DELETE ON trials_trialboxitem FOR EACH STATEMENT EXECUTE FUNCTION checkout_statement_lock();
CREATE TRIGGER checkout_serialization BEFORE INSERT OR UPDATE OR DELETE ON trials_guestbookingrecoverygrant FOR EACH STATEMENT EXECUTE FUNCTION checkout_statement_lock();
CREATE TRIGGER checkout_serialization BEFORE INSERT OR UPDATE OR DELETE ON trials_bookingphonechallenge FOR EACH STATEMENT EXECUTE FUNCTION checkout_statement_lock();
CREATE TRIGGER checkout_serialization BEFORE INSERT OR UPDATE OR DELETE ON trials_guestrecoveryphonechallenge FOR EACH STATEMENT EXECUTE FUNCTION checkout_statement_lock();
CREATE TRIGGER checkout_serialization BEFORE INSERT OR UPDATE OR DELETE ON trials_bookingpolicyacceptance FOR EACH STATEMENT EXECUTE FUNCTION checkout_statement_lock();
CREATE TRIGGER checkout_serialization BEFORE INSERT OR UPDATE OR DELETE ON delivery_deliveryassignment FOR EACH STATEMENT EXECUTE FUNCTION checkout_statement_lock();
CREATE TRIGGER checkout_serialization BEFORE INSERT OR UPDATE OR DELETE ON delivery_arrivalotp FOR EACH STATEMENT EXECUTE FUNCTION checkout_statement_lock();
CREATE TRIGGER checkout_serialization BEFORE INSERT OR UPDATE OR DELETE ON trials_trialplanrevision FOR EACH STATEMENT EXECUTE FUNCTION checkout_statement_lock();
CREATE TRIGGER checkout_serialization BEFORE INSERT OR UPDATE OR DELETE ON trials_trialitemallocation FOR EACH STATEMENT EXECUTE FUNCTION checkout_statement_lock();
CREATE TRIGGER checkout_serialization BEFORE INSERT OR UPDATE OR DELETE ON payments_bookingcharge FOR EACH STATEMENT EXECUTE FUNCTION checkout_statement_lock();
CREATE TRIGGER checkout_serialization BEFORE INSERT OR UPDATE OR DELETE ON payments_paymentcontext FOR EACH STATEMENT EXECUTE FUNCTION checkout_statement_lock();
CREATE TRIGGER checkout_serialization BEFORE INSERT OR UPDATE OR DELETE ON payments_paymentattempt FOR EACH STATEMENT EXECUTE FUNCTION checkout_statement_lock();
CREATE TRIGGER checkout_serialization BEFORE INSERT OR UPDATE OR DELETE ON payments_gatewaytransaction FOR EACH STATEMENT EXECUTE FUNCTION checkout_statement_lock();
CREATE TRIGGER checkout_serialization BEFORE INSERT OR UPDATE OR DELETE ON payments_paymentallocation FOR EACH STATEMENT EXECUTE FUNCTION checkout_statement_lock();
CREATE TRIGGER checkout_serialization BEFORE INSERT OR UPDATE OR DELETE ON payments_paymentresolution FOR EACH STATEMENT EXECUTE FUNCTION checkout_statement_lock();
CREATE TRIGGER checkout_serialization BEFORE INSERT OR UPDATE OR DELETE ON payments_gatewaytransactionmatch FOR EACH STATEMENT EXECUTE FUNCTION checkout_statement_lock();
CREATE TRIGGER checkout_serialization BEFORE INSERT OR UPDATE OR DELETE ON inventory_inventoryreservation FOR EACH STATEMENT EXECUTE FUNCTION checkout_statement_lock();
CREATE TRIGGER checkout_serialization BEFORE INSERT OR UPDATE OR DELETE ON inventory_inventoryunit FOR EACH STATEMENT EXECUTE FUNCTION checkout_statement_lock();
CREATE TRIGGER checkout_serialization BEFORE INSERT OR UPDATE OR DELETE ON markets_market FOR EACH STATEMENT EXECUTE FUNCTION checkout_statement_lock();
CREATE TRIGGER checkout_serialization BEFORE INSERT OR UPDATE OR DELETE ON markets_hub FOR EACH STATEMENT EXECUTE FUNCTION checkout_statement_lock();
CREATE TRIGGER checkout_serialization BEFORE INSERT OR UPDATE OR DELETE ON catalog_product FOR EACH STATEMENT EXECUTE FUNCTION checkout_statement_lock();
CREATE TRIGGER checkout_serialization BEFORE INSERT OR UPDATE OR DELETE ON catalog_productvariant FOR EACH STATEMENT EXECUTE FUNCTION checkout_statement_lock();
CREATE TRIGGER checkout_serialization BEFORE INSERT OR UPDATE OR DELETE ON catalog_category FOR EACH STATEMENT EXECUTE FUNCTION checkout_statement_lock();
CREATE TRIGGER checkout_serialization BEFORE INSERT OR UPDATE OR DELETE ON catalog_material FOR EACH STATEMENT EXECUTE FUNCTION checkout_statement_lock();
CREATE TRIGGER checkout_serialization BEFORE INSERT OR UPDATE OR DELETE ON catalog_purity FOR EACH STATEMENT EXECUTE FUNCTION checkout_statement_lock();
CREATE TRIGGER checkout_serialization BEFORE INSERT OR UPDATE OR DELETE ON compliance_businessconfiguration FOR EACH STATEMENT EXECUTE FUNCTION checkout_statement_lock();
CREATE TRIGGER checkout_binding BEFORE INSERT ON payments_paymentresolution FOR EACH ROW EXECUTE FUNCTION checkout_resolution_binding();
CREATE TRIGGER checkout_history BEFORE UPDATE OR DELETE ON payments_paymentresolution FOR EACH ROW EXECUTE FUNCTION trials_reject_evidence_mutation();
CREATE TRIGGER checkout_binding BEFORE INSERT ON payments_gatewaytransactionmatch FOR EACH ROW EXECUTE FUNCTION checkout_match_binding();
CREATE TRIGGER checkout_history BEFORE UPDATE OR DELETE ON payments_gatewaytransactionmatch FOR EACH ROW EXECUTE FUNCTION trials_reject_evidence_mutation();
CREATE TRIGGER checkout_policy BEFORE INSERT OR UPDATE ON payments_bookingcharge FOR EACH ROW EXECUTE FUNCTION checkout_policy_binding();
CREATE TRIGGER checkout_policy BEFORE INSERT OR UPDATE ON trials_trialplanrevision FOR EACH ROW EXECUTE FUNCTION checkout_policy_binding();
DROP TRIGGER checkout_identity ON payments_paymentcontext; CREATE TRIGGER checkout_identity BEFORE INSERT OR UPDATE OR DELETE ON payments_paymentcontext FOR EACH ROW EXECUTE FUNCTION checkout_guard_record('status capture_started_at paid_at closed_at resolution_reference resolution_id','capture_started_at paid_at closed_at resolution_reference resolution_id','OPEN','{"OPEN":["CAPTURING","EXPIRED","CANCELLED","REVIEW_REQUIRED"],"CAPTURING":["PAID","REVIEW_REQUIRED"],"REVIEW_REQUIRED":["PAID","CANCELLED"]}');
DROP TRIGGER checkout_identity ON inventory_inventoryreservation; CREATE TRIGGER checkout_identity BEFORE INSERT OR UPDATE OR DELETE ON inventory_inventoryreservation FOR EACH ROW EXECUTE FUNCTION checkout_guard_record('status capture_started_at committed_at released_at release_reason resolution_reference resolution_id','capture_started_at committed_at released_at release_reason resolution_reference resolution_id','HELD','{"HELD":["CAPTURE_PENDING","EXPIRED","RELEASED"],"CAPTURE_PENDING":["COMMITTED","RELEASED"]}');
"""

REVERSE_SQL = r"""
DROP TRIGGER checkout_serialization ON trials_trialbooking;
DROP TRIGGER checkout_serialization ON trials_guestbrowsersession;
DROP TRIGGER checkout_serialization ON trials_guestbookingaccessgrant;
DROP TRIGGER checkout_serialization ON trials_trialbox;
DROP TRIGGER checkout_serialization ON trials_trialboxitem;
DROP TRIGGER checkout_serialization ON trials_guestbookingrecoverygrant;
DROP TRIGGER checkout_serialization ON trials_bookingphonechallenge;
DROP TRIGGER checkout_serialization ON trials_guestrecoveryphonechallenge;
DROP TRIGGER checkout_serialization ON trials_bookingpolicyacceptance;
DROP TRIGGER checkout_serialization ON delivery_deliveryassignment;
DROP TRIGGER checkout_serialization ON delivery_arrivalotp;
DROP TRIGGER checkout_serialization ON trials_trialplanrevision;
DROP TRIGGER checkout_serialization ON trials_trialitemallocation;
DROP TRIGGER checkout_serialization ON payments_bookingcharge;
DROP TRIGGER checkout_serialization ON payments_paymentcontext;
DROP TRIGGER checkout_serialization ON payments_paymentattempt;
DROP TRIGGER checkout_serialization ON payments_gatewaytransaction;
DROP TRIGGER checkout_serialization ON payments_paymentallocation;
DROP TRIGGER checkout_serialization ON payments_paymentresolution;
DROP TRIGGER checkout_serialization ON payments_gatewaytransactionmatch;
DROP TRIGGER checkout_serialization ON inventory_inventoryreservation;
DROP TRIGGER checkout_serialization ON inventory_inventoryunit;
DROP TRIGGER checkout_serialization ON markets_market;
DROP TRIGGER checkout_serialization ON markets_hub;
DROP TRIGGER checkout_serialization ON catalog_product;
DROP TRIGGER checkout_serialization ON catalog_productvariant;
DROP TRIGGER checkout_serialization ON catalog_category;
DROP TRIGGER checkout_serialization ON catalog_material;
DROP TRIGGER checkout_serialization ON catalog_purity;
DROP TRIGGER checkout_serialization ON compliance_businessconfiguration;
DROP TRIGGER checkout_binding ON payments_paymentresolution; DROP TRIGGER checkout_history ON payments_paymentresolution;
DROP TRIGGER checkout_binding ON payments_gatewaytransactionmatch; DROP TRIGGER checkout_history ON payments_gatewaytransactionmatch;
DROP TRIGGER checkout_policy ON payments_bookingcharge;
DROP TRIGGER checkout_policy ON trials_trialplanrevision;
CREATE OR REPLACE FUNCTION checkout_booking_binding() RETURNS trigger LANGUAGE plpgsql AS $$
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
CREATE OR REPLACE FUNCTION checkout_guest_binding() RETURNS trigger LANGUAGE plpgsql AS $$
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
CREATE OR REPLACE FUNCTION checkout_charge_binding() RETURNS trigger LANGUAGE plpgsql AS $$
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
CREATE OR REPLACE FUNCTION checkout_context_binding() RETURNS trigger LANGUAGE plpgsql AS $$
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
CREATE OR REPLACE FUNCTION checkout_reservation_binding() RETURNS trigger LANGUAGE plpgsql AS $$
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
CREATE OR REPLACE FUNCTION checkout_attempt_binding() RETURNS trigger LANGUAGE plpgsql AS $$
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
CREATE OR REPLACE FUNCTION checkout_receipt_binding() RETURNS trigger LANGUAGE plpgsql AS $$
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
CREATE OR REPLACE FUNCTION checkout_payment_allocation() RETURNS trigger LANGUAGE plpgsql AS $$
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
DROP TRIGGER checkout_identity ON payments_paymentcontext; CREATE TRIGGER checkout_identity BEFORE INSERT OR UPDATE OR DELETE ON payments_paymentcontext FOR EACH ROW EXECUTE FUNCTION checkout_guard_record('status capture_started_at paid_at closed_at resolution_reference','capture_started_at paid_at closed_at resolution_reference','OPEN','{"OPEN":["CAPTURING","EXPIRED","CANCELLED","REVIEW_REQUIRED"],"CAPTURING":["PAID","REVIEW_REQUIRED"],"REVIEW_REQUIRED":["PAID","CANCELLED"]}');
DROP TRIGGER checkout_identity ON inventory_inventoryreservation; CREATE TRIGGER checkout_identity BEFORE INSERT OR UPDATE OR DELETE ON inventory_inventoryreservation FOR EACH ROW EXECUTE FUNCTION checkout_guard_record('status capture_started_at committed_at released_at release_reason resolution_reference','capture_started_at committed_at released_at release_reason resolution_reference','HELD','{"HELD":["CAPTURE_PENDING","EXPIRED","RELEASED"],"CAPTURE_PENDING":["COMMITTED","RELEASED"]}');
DROP FUNCTION checkout_statement_lock(); DROP FUNCTION checkout_serialize_writes();
DROP FUNCTION checkout_unit_active(bigint); DROP FUNCTION checkout_resolution_current(bigint,bigint);
DROP FUNCTION checkout_resolution_binding(); DROP FUNCTION checkout_match_binding(); DROP FUNCTION checkout_policy_binding();

"""

def require_empty(apps, schema_editor):
    for table in ("payments_paymentresolution", "payments_gatewaytransactionmatch",
                  "payments_paymentcontext", "inventory_inventoryreservation"):
        with schema_editor.connection.cursor() as cursor:
            cursor.execute(f'SELECT EXISTS (SELECT 1 FROM "{table}")')
            if cursor.fetchone()[0]:
                raise IrreversibleError("Checkout evidence exists; use reviewed forward recovery.")

class Migration(migrations.Migration):
    dependencies = [
        ("trials", "0004_checkout_integrity"),
        ("payments", "0002_alter_gatewaytransaction_attempt_paymentresolution_and_more"),
        ("inventory", "0007_inventoryreservation_resolution"),
    ]
    operations = [migrations.RunSQL(SQL, REVERSE_SQL),
                  migrations.RunPython(migrations.RunPython.noop, require_empty)]
