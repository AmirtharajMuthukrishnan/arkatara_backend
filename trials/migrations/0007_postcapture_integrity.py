"""Separate provider collection success from fulfilment acceptance.

Refs: BUSINESS_RULES.md R-14, R-16, R-24, R-26, R-28, R-40; DECISIONS.md D-27, BD-02, BD-05.
Original receipt/allocation/closure facts remain immutable; no refund/retention policy is chosen.
"""
from django.db import migrations
from django.db.migrations.exceptions import IrreversibleError

SQL = r"""

CREATE FUNCTION checkout_purchase_protected(target_purchase bigint) RETURNS boolean LANGUAGE sql VOLATILE AS $$
    SELECT EXISTS(SELECT 1 FROM billing_purchaseitem WHERE purchase_id=target_purchase)
    AND NOT EXISTS(SELECT 1 FROM billing_purchaseitem i JOIN inventory_inventoryreservation held ON held.id=i.reservation_id
        JOIN inventory_inventoryunit unit ON unit.id=held.unit_id WHERE i.purchase_id=target_purchase AND
            (held.status<>'COMMITTED' OR unit.status<>'OUT_FOR_DELIVERY' OR unit.custody<>'AGENT'
             OR NOT EXISTS(SELECT 1 FROM delivery_reservationcustodyevent e WHERE e.reservation_id=held.id AND e.kind='DISPATCH'
                AND NOT EXISTS(SELECT 1 FROM delivery_reservationcustodyevent later WHERE later.previous_id=e.id))))
$$;

CREATE FUNCTION checkout_obligation_rejected(obligation_kind text,target_obligation bigint) RETURNS boolean LANGUAGE sql VOLATILE AS $$
    SELECT EXISTS(SELECT 1 FROM payments_paymentcontext c JOIN payments_paymentresolution r ON r.context_id=c.id
        WHERE ((obligation_kind='BOOKING' AND c.booking_charge_id=target_obligation)
            OR (obligation_kind='PURCHASE' AND c.purchase_obligation_id=target_obligation))
        AND c.status='CANCELLED' AND r.outcome='CAPTURE_NOT_ACCEPTED' AND checkout_resolution_current(r.id,c.id))
    AND NOT EXISTS(SELECT 1 FROM payments_paymentcontext c WHERE
        ((obligation_kind='BOOKING' AND c.booking_charge_id=target_obligation)
            OR (obligation_kind='PURCHASE' AND c.purchase_obligation_id=target_obligation))
        AND c.status NOT IN ('EXPIRED','CANCELLED'))
$$;

CREATE OR REPLACE FUNCTION checkout_resolution_current(target_resolution bigint, target_context bigint) RETURNS boolean LANGUAGE sql VOLATILE AS $$
    SELECT EXISTS (SELECT 1 FROM payments_paymentresolution r JOIN payments_paymentcontext c ON c.id=r.context_id
        LEFT JOIN payments_bookingcharge q ON q.id=c.booking_charge_id LEFT JOIN billing_purchaseobligation o ON o.id=c.purchase_obligation_id WHERE r.id=target_resolution AND r.context_id=target_context
        AND (
        (r.outcome='NO_CAPTURE' AND NOT EXISTS(SELECT 1 FROM payments_gatewaytransaction t JOIN payments_paymentattempt a
            ON a.id=checkout_effective_attempt(t.id) WHERE a.context_id=target_context AND t.transaction_kind='CAPTURE'))
        OR (r.outcome='CAPTURE_NOT_ACCEPTED' AND EXISTS(SELECT 1 FROM payments_gatewaytransaction t JOIN payments_paymentattempt a
            ON a.id=checkout_effective_attempt(t.id) WHERE a.context_id=target_context AND t.transaction_kind='CAPTURE')))
        AND NOT EXISTS(SELECT 1 FROM payments_gatewaytransaction t JOIN payments_paymentattempt a ON a.id=checkout_effective_attempt(t.id)
            WHERE a.context_id=target_context AND t.transaction_kind='CAPTURE' AND NOT EXISTS(
                SELECT 1 FROM payments_paymentresolutionreceipt x WHERE x.resolution_id=r.id AND x.gateway_transaction_id=t.id))
        AND r.currency=COALESCE(q.currency,o.currency) AND r.captured_minor=(SELECT COALESCE(SUM(t.amount_minor),0)
            FROM payments_gatewaytransaction t JOIN payments_paymentattempt a ON a.id=COALESCE(t.attempt_id,
                (SELECT attempt_id FROM payments_gatewaytransactionmatch WHERE gateway_transaction_id=t.id))
            WHERE a.context_id=target_context AND t.transaction_kind='CAPTURE' AND t.currency=r.currency)
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
                OR a.status NOT IN ('FAILED','EXPIRED','REVIEW_REQUIRED','SUCCEEDED'))))
$$;
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
            EXISTS (SELECT 1 FROM payments_bookingcharge q WHERE q.booking_id=NEW.id AND (q.status='DUE' OR (q.status='COLLECTED' AND NOT checkout_obligation_rejected('BOOKING',q.id))))
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
        IF OLD.status='COMMITTED' AND NEW.status='RELEASED' THEN
            IF NOT EXISTS(SELECT 1 FROM delivery_reservationcustodyevent e WHERE e.id=NEW.disposition_id AND e.kind='SALE')
                AND EXISTS(SELECT 1 FROM billing_purchaseitem i JOIN billing_purchaseobligation o ON o.purchase_id=i.purchase_id
                    WHERE i.reservation_id=NEW.id AND (o.status='DUE' OR
                        (o.status='COLLECTED' AND NOT checkout_obligation_rejected('PURCHASE',o.id)))) THEN
                RAISE EXCEPTION 'Resolve selected final-payment outcomes before releasing returned stock.' USING ERRCODE='23514'; END IF;
            IF NOT EXISTS(SELECT 1 FROM delivery_reservationcustodyevent e WHERE e.id=NEW.disposition_id
                AND e.reservation_id=NEW.id AND e.kind IN ('SALE','QC_PASS','QC_FAIL')
                AND NOT EXISTS(SELECT 1 FROM delivery_reservationcustodyevent later WHERE later.previous_id=e.id))
                OR NEW.released_at<OLD.committed_at THEN
                RAISE EXCEPTION 'Committed stock closes only on confirmed sale or physically received QC disposition.' USING ERRCODE='23514'; END IF;
        ELSIF NEW.status IN ('EXPIRED','RELEASED') AND context.status NOT IN ('EXPIRED','CANCELLED') THEN
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
CREATE OR REPLACE FUNCTION billing_binding() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE p billing_purchase%ROWTYPE; r inventory_inventoryreservation%ROWTYPE;
DECLARE d billing_financialdocument%ROWTYPE; q billing_purchaseobligation%ROWTYPE;
DECLARE total numeric; target bigint; tax_status text; issuer bigint; expected_issuer bigint;
BEGIN
    IF TG_TABLE_NAME='billing_purchaseitem' THEN
        SELECT * INTO p FROM billing_purchase WHERE id=NEW.purchase_id FOR UPDATE;
        SELECT * INTO r FROM inventory_inventoryreservation WHERE id=NEW.reservation_id FOR UPDATE;
        SELECT c.booking_charge_id INTO target FROM payments_paymentcontext c WHERE c.id=r.payment_context_id;
        IF p.status<>'DRAFT' OR r.status<>'COMMITTED' OR NEW.currency<>p.currency
            OR NOT EXISTS(SELECT 1 FROM payments_bookingcharge WHERE id=target AND booking_id=p.booking_id)
            OR NEW.product_snapshot='{}' OR NEW.price_snapshot='{}' THEN
            RAISE EXCEPTION 'Purchase item requires exact committed booking stock and historical prices.' USING ERRCODE='23514'; END IF;
    ELSIF TG_TABLE_NAME='billing_purchase' THEN
        IF NEW.seller_snapshot='{}' OR NEW.customer_snapshot='{}' OR NEW.balance_snapshot='{}' THEN
            RAISE EXCEPTION 'Purchase requires independent historical party and balance facts.' USING ERRCODE='23514'; END IF;
        IF TG_OP='UPDATE' AND OLD.status<>NEW.status THEN
            IF NEW.status='SEALED' THEN
                SELECT COALESCE(SUM(amount_minor),0) INTO total FROM billing_purchaseitem WHERE purchase_id=NEW.id;
                IF total<>NEW.jewellery_total_minor OR NOT checkout_purchase_protected(NEW.id) OR NOT EXISTS(SELECT 1 FROM delivery_homevisit
                    WHERE booking_id=NEW.booking_id AND status='IN_PERSON_TRIAL') THEN
                    RAISE EXCEPTION 'Seal requires complete selected jewellery value and verified trial.' USING ERRCODE='23514'; END IF;
                IF EXISTS(SELECT 1 FROM billing_purchaseitem i JOIN billing_purchaseitem other ON other.reservation_id=i.reservation_id
                    JOIN billing_purchase previous ON previous.id=other.purchase_id WHERE i.purchase_id=NEW.id AND previous.id<>NEW.id
                    AND previous.status IN ('SEALED','CONFIRMED')) THEN
                    RAISE EXCEPTION 'A committed piece cannot support two live purchase versions.' USING ERRCODE='23514'; END IF;
            END IF;
            IF NEW.status='CONFIRMED' AND (NOT checkout_purchase_protected(NEW.id) OR NOT EXISTS (SELECT 1 FROM billing_purchaseobligation o
                JOIN payments_paymentcontext c ON c.purchase_obligation_id=o.id
                WHERE o.purchase_id=NEW.id AND o.status='COLLECTED' AND c.status='PAID')) THEN
                RAISE EXCEPTION 'Financial confirmation requires the paid purchase obligation.' USING ERRCODE='23514'; END IF;
            IF NEW.status='VOID' AND EXISTS(SELECT 1 FROM billing_purchaseobligation WHERE purchase_id=NEW.id AND (status='DUE' OR (status='COLLECTED' AND NOT checkout_obligation_rejected('PURCHASE',id)))) THEN
                RAISE EXCEPTION 'Resolve obligation before voiding selection.' USING ERRCODE='23514'; END IF;
        END IF;
    ELSIF TG_TABLE_NAME='billing_purchaseobligation' THEN
        SELECT * INTO p FROM billing_purchase WHERE id=NEW.purchase_id FOR UPDATE;
        IF ROW(NEW.amount_minor,NEW.currency) IS DISTINCT FROM ROW(p.payable_minor,p.currency) THEN
            RAISE EXCEPTION 'Purchase balance cannot be redirected or repriced.' USING ERRCODE='23514'; END IF;
        IF TG_OP='UPDATE' AND NEW.status<>OLD.status THEN
            SELECT COALESCE(SUM(amount_minor),0) INTO total FROM payments_paymentallocation WHERE purchase_obligation_id=NEW.id;
            IF NEW.status='DUE' AND (p.status<>'SEALED' OR NEW.quote_expires_at<=statement_timestamp()) THEN
                RAISE EXCEPTION 'Final collection requires a sealed selection and current quote.' USING ERRCODE='23514'; END IF;
            IF NEW.status='COLLECTED' AND (total<>NEW.amount_minor OR p.status<>'SEALED') THEN
                RAISE EXCEPTION 'Final collection requires the exact captured balance.' USING ERRCODE='23514'; END IF;
            IF NEW.status='VOID' AND (EXISTS(SELECT 1 FROM payments_paymentcontext WHERE purchase_obligation_id=NEW.id
                AND status NOT IN ('EXPIRED','CANCELLED')) OR (total<>0 AND NOT EXISTS(
                    SELECT 1 FROM payments_paymentcontext c JOIN payments_paymentresolution x ON x.id=c.resolution_id
                    WHERE c.purchase_obligation_id=NEW.id AND c.status='CANCELLED' AND x.allocated_minor=total))) THEN
                RAISE EXCEPTION 'Resolve captured purchase funds before voiding obligation.' USING ERRCODE='23514'; END IF;
        END IF;
    ELSIF TG_TABLE_NAME='billing_financialdocumentline' THEN
        SELECT * INTO d FROM billing_financialdocument WHERE id=NEW.document_id FOR UPDATE;
        IF d.status<>'DRAFT' OR NEW.snapshot='{}' THEN
            RAISE EXCEPTION 'Issued line sets and descriptions are immutable.' USING ERRCODE='23514'; END IF;
        IF d.kind='DISPATCH' THEN
            IF NEW.purchase_item_id IS NOT NULL OR NOT EXISTS(SELECT 1 FROM inventory_inventoryreservation held
                JOIN payments_paymentcontext c ON c.id=held.payment_context_id JOIN payments_bookingcharge charge ON charge.id=c.booking_charge_id
                WHERE held.id=NEW.reservation_id AND held.status='COMMITTED' AND charge.booking_id=d.booking_id) THEN
                RAISE EXCEPTION 'Dispatch manifest lines require exact committed booking pieces.' USING ERRCODE='23514'; END IF;
        ELSIF d.kind='TAX_INVOICE' THEN
            IF NEW.reservation_id IS NOT NULL OR NOT EXISTS(SELECT 1 FROM billing_purchaseitem i JOIN billing_purchaseobligation o ON o.purchase_id=i.purchase_id
                WHERE i.id=NEW.purchase_item_id AND o.id=d.purchase_obligation_id AND i.amount_minor=NEW.amount_minor) THEN
                RAISE EXCEPTION 'Invoice lines require exact historical selected purchase pieces.' USING ERRCODE='23514'; END IF;
        ELSIF d.kind='UPFRONT_RECORD' AND (NEW.reservation_id IS NOT NULL OR NEW.purchase_item_id IS NOT NULL) THEN
            RAISE EXCEPTION 'Upfront obligation documents are distinct from jewellery sale lines.' USING ERRCODE='23514'; END IF;
    ELSIF TG_TABLE_NAME='billing_financialdocument' THEN
        IF NEW.currency !~ '^[A-Z]{3}$' THEN RAISE EXCEPTION 'Document currency must be explicit.' USING ERRCODE='23514'; END IF;
        IF NEW.issuer_snapshot='{}' OR NEW.recipient_snapshot='{}' OR btrim(NEW.review_reference)='' THEN
            RAISE EXCEPTION 'Documents require historical parties and reviewed applicability.' USING ERRCODE='23514'; END IF;
        IF NEW.booking_charge_id IS NOT NULL THEN
            SELECT booking_id,payee_legal_entity_id INTO target,expected_issuer FROM payments_bookingcharge WHERE id=NEW.booking_charge_id;
        ELSIF NEW.purchase_obligation_id IS NOT NULL THEN
            SELECT p0.booking_id,p0.seller_id INTO target,expected_issuer FROM billing_purchase p0 JOIN billing_purchaseobligation o ON o.purchase_id=p0.id WHERE o.id=NEW.purchase_obligation_id;
        ELSE target:=NEW.booking_id;
        END IF;
        IF target IS DISTINCT FROM NEW.booking_id THEN
            RAISE EXCEPTION 'Document obligation must belong to the same booking.' USING ERRCODE='23514'; END IF;
        SELECT issuer_id INTO issuer FROM billing_documentseries WHERE id=NEW.series_id FOR SHARE;
        IF expected_issuer IS NOT NULL AND issuer IS DISTINCT FROM expected_issuer THEN
            RAISE EXCEPTION 'Numbering series cannot relabel the obligation legal party.' USING ERRCODE='23514'; END IF;
        IF (NEW.kind='ADJUSTMENT') <> (NEW.corrects_id IS NOT NULL) THEN
            RAISE EXCEPTION 'Corrections preserve their original document.' USING ERRCODE='23514'; END IF;
        IF NEW.corrects_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM billing_financialdocument oldd WHERE oldd.id=NEW.corrects_id
            AND oldd.status='ISSUED' AND oldd.booking_id=NEW.booking_id AND oldd.currency=NEW.currency
            AND oldd.booking_charge_id IS NOT DISTINCT FROM NEW.booking_charge_id
            AND oldd.purchase_obligation_id IS NOT DISTINCT FROM NEW.purchase_obligation_id) THEN
            RAISE EXCEPTION 'Correction document cannot redirect historical obligations.' USING ERRCODE='23514'; END IF;
        IF TG_OP='UPDATE' AND OLD.status<>NEW.status AND NEW.status='ISSUED' THEN
            SELECT status INTO tax_status FROM billing_taxevidence WHERE id=NEW.tax_evidence_id;
            SELECT COALESCE(SUM(amount_minor),0) INTO total FROM billing_financialdocumentline WHERE document_id=NEW.id;
            IF tax_status<>'REVIEWED' OR total<>NEW.amount_minor OR NOT EXISTS(
                SELECT 1 FROM billing_financialdocumentline WHERE document_id=NEW.id) THEN
                RAISE EXCEPTION 'Issue requires sealed complete lines and explicit reviewed tax treatment.' USING ERRCODE='23514'; END IF;
            IF NEW.kind='UPFRONT_RECORD' AND NOT EXISTS(SELECT 1 FROM payments_bookingcharge o WHERE o.id=NEW.booking_charge_id
                AND o.amount_minor=NEW.amount_minor AND o.currency=NEW.currency) THEN
                RAISE EXCEPTION 'Upfront document must preserve obligation amount and currency.' USING ERRCODE='23514'; END IF;
            IF NEW.kind='TAX_INVOICE' AND NOT EXISTS(SELECT 1 FROM billing_purchaseobligation o JOIN billing_purchase p0 ON p0.id=o.purchase_id
                WHERE o.id=NEW.purchase_obligation_id AND p0.status='CONFIRMED' AND p0.jewellery_total_minor=NEW.amount_minor AND p0.currency=NEW.currency) THEN
                RAISE EXCEPTION 'Invoice must preserve the confirmed jewellery value.' USING ERRCODE='23514'; END IF;
            IF NEW.kind='DISPATCH' AND EXISTS(SELECT 1 FROM inventory_inventoryreservation held JOIN payments_paymentcontext c ON c.id=held.payment_context_id
                JOIN payments_bookingcharge charge ON charge.id=c.booking_charge_id WHERE charge.booking_id=NEW.booking_id AND held.status='COMMITTED'
                AND NOT EXISTS(SELECT 1 FROM billing_financialdocumentline line WHERE line.document_id=NEW.id AND line.reservation_id=held.id)) THEN
                RAISE EXCEPTION 'Issue seals the complete combined dispatch manifest.' USING ERRCODE='23514'; END IF;
        END IF;
    ELSIF TG_TABLE_NAME='billing_documentseries' THEN
        IF btrim(NEW.code)='' OR btrim(NEW.review_reference)='' OR NEW.scope_snapshot='{}' THEN
            RAISE EXCEPTION 'Numbering series requires reviewed entity scope.' USING ERRCODE='23514'; END IF;
    END IF;
    RETURN NEW;
END; $$;
CREATE OR REPLACE FUNCTION checkout_context_binding() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE charge payments_bookingcharge%ROWTYPE; booking trials_trialbooking%ROWTYPE;
DECLARE purchase_charge billing_purchaseobligation%ROWTYPE; purchase billing_purchase%ROWTYPE;
BEGIN

    IF NEW.purchase_obligation_id IS NOT NULL THEN
        SELECT * INTO purchase_charge FROM billing_purchaseobligation WHERE id=NEW.purchase_obligation_id FOR UPDATE;
        SELECT * INTO purchase FROM billing_purchase WHERE id=purchase_charge.purchase_id FOR UPDATE;
        IF TG_OP='INSERT' AND (purchase_charge.status<>'DUE' OR purchase.status<>'SEALED' OR NOT checkout_purchase_protected(purchase.id) OR NEW.opened_at>=purchase_charge.quote_expires_at
            OR NEW.expires_at<=statement_timestamp() OR NEW.policy_snapshot='{}') THEN
            RAISE EXCEPTION 'Final context requires accepted current purchase balance.' USING ERRCODE='23514'; END IF;
        IF TG_OP='UPDATE' AND OLD.status<>NEW.status THEN
            IF NEW.status='CAPTURING' AND (purchase_charge.status<>'DUE' OR purchase.status<>'SEALED' OR NOT checkout_purchase_protected(purchase.id)
                OR OLD.expires_at<=statement_timestamp() OR NEW.capture_started_at>=NEW.expires_at
                OR EXISTS(SELECT 1 FROM billing_purchaseitem i JOIN inventory_inventoryreservation r ON r.id=i.reservation_id
                    WHERE i.purchase_id=purchase.id AND r.status<>'COMMITTED')) THEN
                RAISE EXCEPTION 'Final capture requires valid protected purchase selection.' USING ERRCODE='23514'; END IF;
            IF NEW.status IN ('EXPIRED','CANCELLED') AND (
                (NEW.resolution_id IS NULL AND EXISTS(SELECT 1 FROM payments_paymentattempt WHERE context_id=NEW.id AND provider_order_id IS NOT NULL))
                OR (NEW.resolution_id IS NOT NULL AND NOT checkout_resolution_current(NEW.resolution_id,NEW.id))
                OR (NEW.status='EXPIRED' AND NEW.closed_at<NEW.expires_at)) THEN
                RAISE EXCEPTION 'Reconcile final provider outcome before closure.' USING ERRCODE='23514'; END IF;
            IF NEW.status='PAID' AND (purchase_charge.status<>'COLLECTED' OR NOT checkout_purchase_protected(purchase.id) OR NOT EXISTS(SELECT 1 FROM payments_paymentattempt
                WHERE context_id=NEW.id AND status='SUCCEEDED')) THEN
                RAISE EXCEPTION 'Paid final context requires exact verified collection.' USING ERRCODE='23514'; END IF;
        END IF;
        RETURN NEW;
    END IF;
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
CREATE OR REPLACE FUNCTION checkout_payment_allocation() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE receipt payments_gatewaytransaction%ROWTYPE; charge payments_bookingcharge%ROWTYPE;
DECLARE context payments_paymentcontext%ROWTYPE; allocated numeric; applied numeric;
DECLARE purchase_charge billing_purchaseobligation%ROWTYPE;
BEGIN

    IF NEW.purchase_obligation_id IS NOT NULL THEN
        SELECT * INTO purchase_charge FROM billing_purchaseobligation WHERE id=NEW.purchase_obligation_id FOR UPDATE;
        SELECT * INTO receipt FROM payments_gatewaytransaction WHERE id=NEW.gateway_transaction_id FOR UPDATE;
        SELECT c.* INTO context FROM payments_paymentcontext c JOIN payments_paymentattempt a ON a.context_id=c.id
            WHERE a.id=checkout_effective_attempt(receipt.id) FOR UPDATE OF c;
        IF receipt.transaction_kind<>'CAPTURE' OR receipt.currency<>NEW.currency OR purchase_charge.currency<>NEW.currency
            OR context.purchase_obligation_id IS DISTINCT FROM purchase_charge.id OR purchase_charge.status<>'DUE' OR NOT checkout_purchase_protected(purchase_charge.purchase_id)
            OR context.status NOT IN ('CAPTURING','REVIEW_REQUIRED') THEN
            RAISE EXCEPTION 'Captured funds cannot cross upfront/final obligation boundaries.' USING ERRCODE='23514'; END IF;
        SELECT COALESCE(SUM(amount_minor),0) INTO allocated FROM payments_paymentallocation WHERE gateway_transaction_id=receipt.id;
        SELECT COALESCE(SUM(amount_minor),0) INTO applied FROM payments_paymentallocation WHERE purchase_obligation_id=purchase_charge.id;
        IF allocated+NEW.amount_minor>receipt.amount_minor OR applied+NEW.amount_minor>purchase_charge.amount_minor THEN
            RAISE EXCEPTION 'Final allocation exceeds captured receipt or purchase balance.' USING ERRCODE='23514'; END IF;
        RETURN NEW;
    END IF;
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
"""
REVERSE_SQL = r"""
CREATE OR REPLACE FUNCTION checkout_resolution_current(target_resolution bigint, target_context bigint) RETURNS boolean LANGUAGE sql VOLATILE AS $$
    SELECT EXISTS (SELECT 1 FROM payments_paymentresolution r JOIN payments_paymentcontext c ON c.id=r.context_id
        LEFT JOIN payments_bookingcharge q ON q.id=c.booking_charge_id LEFT JOIN billing_purchaseobligation o ON o.id=c.purchase_obligation_id WHERE r.id=target_resolution AND r.context_id=target_context
        AND (
        (r.outcome='NO_CAPTURE' AND NOT EXISTS(SELECT 1 FROM payments_gatewaytransaction t JOIN payments_paymentattempt a
            ON a.id=checkout_effective_attempt(t.id) WHERE a.context_id=target_context AND t.transaction_kind='CAPTURE'))
        OR (r.outcome='CAPTURE_NOT_ACCEPTED' AND EXISTS(SELECT 1 FROM payments_gatewaytransaction t JOIN payments_paymentattempt a
            ON a.id=checkout_effective_attempt(t.id) WHERE a.context_id=target_context AND t.transaction_kind='CAPTURE')))
        AND NOT EXISTS(SELECT 1 FROM payments_gatewaytransaction t JOIN payments_paymentattempt a ON a.id=checkout_effective_attempt(t.id)
            WHERE a.context_id=target_context AND t.transaction_kind='CAPTURE' AND NOT EXISTS(
                SELECT 1 FROM payments_paymentresolutionreceipt x WHERE x.resolution_id=r.id AND x.gateway_transaction_id=t.id))
        AND r.currency=COALESCE(q.currency,o.currency) AND r.captured_minor=(SELECT COALESCE(SUM(t.amount_minor),0)
            FROM payments_gatewaytransaction t JOIN payments_paymentattempt a ON a.id=COALESCE(t.attempt_id,
                (SELECT attempt_id FROM payments_gatewaytransactionmatch WHERE gateway_transaction_id=t.id))
            WHERE a.context_id=target_context AND t.transaction_kind='CAPTURE' AND t.currency=r.currency)
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
        IF OLD.status='COMMITTED' AND NEW.status='RELEASED' THEN
            IF NOT EXISTS(SELECT 1 FROM delivery_reservationcustodyevent e WHERE e.id=NEW.disposition_id
                AND e.reservation_id=NEW.id AND e.kind IN ('SALE','QC_PASS','QC_FAIL')
                AND NOT EXISTS(SELECT 1 FROM delivery_reservationcustodyevent later WHERE later.previous_id=e.id))
                OR NEW.released_at<OLD.committed_at THEN
                RAISE EXCEPTION 'Committed stock closes only on confirmed sale or physically received QC disposition.' USING ERRCODE='23514'; END IF;
        ELSIF NEW.status IN ('EXPIRED','RELEASED') AND context.status NOT IN ('EXPIRED','CANCELLED') THEN
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
CREATE OR REPLACE FUNCTION billing_binding() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE p billing_purchase%ROWTYPE; r inventory_inventoryreservation%ROWTYPE;
DECLARE d billing_financialdocument%ROWTYPE; q billing_purchaseobligation%ROWTYPE;
DECLARE total numeric; target bigint; tax_status text; issuer bigint; expected_issuer bigint;
BEGIN
    IF TG_TABLE_NAME='billing_purchaseitem' THEN
        SELECT * INTO p FROM billing_purchase WHERE id=NEW.purchase_id FOR UPDATE;
        SELECT * INTO r FROM inventory_inventoryreservation WHERE id=NEW.reservation_id FOR UPDATE;
        SELECT c.booking_charge_id INTO target FROM payments_paymentcontext c WHERE c.id=r.payment_context_id;
        IF p.status<>'DRAFT' OR r.status<>'COMMITTED' OR NEW.currency<>p.currency
            OR NOT EXISTS(SELECT 1 FROM payments_bookingcharge WHERE id=target AND booking_id=p.booking_id)
            OR NEW.product_snapshot='{}' OR NEW.price_snapshot='{}' THEN
            RAISE EXCEPTION 'Purchase item requires exact committed booking stock and historical prices.' USING ERRCODE='23514'; END IF;
    ELSIF TG_TABLE_NAME='billing_purchase' THEN
        IF NEW.seller_snapshot='{}' OR NEW.customer_snapshot='{}' OR NEW.balance_snapshot='{}' THEN
            RAISE EXCEPTION 'Purchase requires independent historical party and balance facts.' USING ERRCODE='23514'; END IF;
        IF TG_OP='UPDATE' AND OLD.status<>NEW.status THEN
            IF NEW.status='SEALED' THEN
                SELECT COALESCE(SUM(amount_minor),0) INTO total FROM billing_purchaseitem WHERE purchase_id=NEW.id;
                IF total<>NEW.jewellery_total_minor OR NOT EXISTS(SELECT 1 FROM delivery_homevisit
                    WHERE booking_id=NEW.booking_id AND status='IN_PERSON_TRIAL') THEN
                    RAISE EXCEPTION 'Seal requires complete selected jewellery value and verified trial.' USING ERRCODE='23514'; END IF;
                IF EXISTS(SELECT 1 FROM billing_purchaseitem i JOIN billing_purchaseitem other ON other.reservation_id=i.reservation_id
                    JOIN billing_purchase previous ON previous.id=other.purchase_id WHERE i.purchase_id=NEW.id AND previous.id<>NEW.id
                    AND previous.status IN ('SEALED','CONFIRMED')) THEN
                    RAISE EXCEPTION 'A committed piece cannot support two live purchase versions.' USING ERRCODE='23514'; END IF;
            END IF;
            IF NEW.status='CONFIRMED' AND NOT EXISTS (SELECT 1 FROM billing_purchaseobligation o
                JOIN payments_paymentcontext c ON c.purchase_obligation_id=o.id
                WHERE o.purchase_id=NEW.id AND o.status='COLLECTED' AND c.status='PAID') THEN
                RAISE EXCEPTION 'Financial confirmation requires the paid purchase obligation.' USING ERRCODE='23514'; END IF;
            IF NEW.status='VOID' AND EXISTS(SELECT 1 FROM billing_purchaseobligation WHERE purchase_id=NEW.id AND status IN ('DUE','COLLECTED')) THEN
                RAISE EXCEPTION 'Resolve obligation before voiding selection.' USING ERRCODE='23514'; END IF;
        END IF;
    ELSIF TG_TABLE_NAME='billing_purchaseobligation' THEN
        SELECT * INTO p FROM billing_purchase WHERE id=NEW.purchase_id FOR UPDATE;
        IF ROW(NEW.amount_minor,NEW.currency) IS DISTINCT FROM ROW(p.payable_minor,p.currency) THEN
            RAISE EXCEPTION 'Purchase balance cannot be redirected or repriced.' USING ERRCODE='23514'; END IF;
        IF TG_OP='UPDATE' AND NEW.status<>OLD.status THEN
            SELECT COALESCE(SUM(amount_minor),0) INTO total FROM payments_paymentallocation WHERE purchase_obligation_id=NEW.id;
            IF NEW.status='DUE' AND (p.status<>'SEALED' OR NEW.quote_expires_at<=statement_timestamp()) THEN
                RAISE EXCEPTION 'Final collection requires a sealed selection and current quote.' USING ERRCODE='23514'; END IF;
            IF NEW.status='COLLECTED' AND (total<>NEW.amount_minor OR p.status<>'SEALED') THEN
                RAISE EXCEPTION 'Final collection requires the exact captured balance.' USING ERRCODE='23514'; END IF;
            IF NEW.status='VOID' AND (EXISTS(SELECT 1 FROM payments_paymentcontext WHERE purchase_obligation_id=NEW.id
                AND status NOT IN ('EXPIRED','CANCELLED')) OR (total<>0 AND NOT EXISTS(
                    SELECT 1 FROM payments_paymentcontext c JOIN payments_paymentresolution x ON x.id=c.resolution_id
                    WHERE c.purchase_obligation_id=NEW.id AND c.status='CANCELLED' AND x.allocated_minor=total))) THEN
                RAISE EXCEPTION 'Resolve captured purchase funds before voiding obligation.' USING ERRCODE='23514'; END IF;
        END IF;
    ELSIF TG_TABLE_NAME='billing_financialdocumentline' THEN
        SELECT * INTO d FROM billing_financialdocument WHERE id=NEW.document_id FOR UPDATE;
        IF d.status<>'DRAFT' OR NEW.snapshot='{}' THEN
            RAISE EXCEPTION 'Issued line sets and descriptions are immutable.' USING ERRCODE='23514'; END IF;
        IF d.kind='DISPATCH' THEN
            IF NEW.purchase_item_id IS NOT NULL OR NOT EXISTS(SELECT 1 FROM inventory_inventoryreservation held
                JOIN payments_paymentcontext c ON c.id=held.payment_context_id JOIN payments_bookingcharge charge ON charge.id=c.booking_charge_id
                WHERE held.id=NEW.reservation_id AND held.status='COMMITTED' AND charge.booking_id=d.booking_id) THEN
                RAISE EXCEPTION 'Dispatch manifest lines require exact committed booking pieces.' USING ERRCODE='23514'; END IF;
        ELSIF d.kind='TAX_INVOICE' THEN
            IF NEW.reservation_id IS NOT NULL OR NOT EXISTS(SELECT 1 FROM billing_purchaseitem i JOIN billing_purchaseobligation o ON o.purchase_id=i.purchase_id
                WHERE i.id=NEW.purchase_item_id AND o.id=d.purchase_obligation_id AND i.amount_minor=NEW.amount_minor) THEN
                RAISE EXCEPTION 'Invoice lines require exact historical selected purchase pieces.' USING ERRCODE='23514'; END IF;
        ELSIF d.kind='UPFRONT_RECORD' AND (NEW.reservation_id IS NOT NULL OR NEW.purchase_item_id IS NOT NULL) THEN
            RAISE EXCEPTION 'Upfront obligation documents are distinct from jewellery sale lines.' USING ERRCODE='23514'; END IF;
    ELSIF TG_TABLE_NAME='billing_financialdocument' THEN
        IF NEW.currency !~ '^[A-Z]{3}$' THEN RAISE EXCEPTION 'Document currency must be explicit.' USING ERRCODE='23514'; END IF;
        IF NEW.issuer_snapshot='{}' OR NEW.recipient_snapshot='{}' OR btrim(NEW.review_reference)='' THEN
            RAISE EXCEPTION 'Documents require historical parties and reviewed applicability.' USING ERRCODE='23514'; END IF;
        IF NEW.booking_charge_id IS NOT NULL THEN
            SELECT booking_id,payee_legal_entity_id INTO target,expected_issuer FROM payments_bookingcharge WHERE id=NEW.booking_charge_id;
        ELSIF NEW.purchase_obligation_id IS NOT NULL THEN
            SELECT p0.booking_id,p0.seller_id INTO target,expected_issuer FROM billing_purchase p0 JOIN billing_purchaseobligation o ON o.purchase_id=p0.id WHERE o.id=NEW.purchase_obligation_id;
        ELSE target:=NEW.booking_id;
        END IF;
        IF target IS DISTINCT FROM NEW.booking_id THEN
            RAISE EXCEPTION 'Document obligation must belong to the same booking.' USING ERRCODE='23514'; END IF;
        SELECT issuer_id INTO issuer FROM billing_documentseries WHERE id=NEW.series_id FOR SHARE;
        IF expected_issuer IS NOT NULL AND issuer IS DISTINCT FROM expected_issuer THEN
            RAISE EXCEPTION 'Numbering series cannot relabel the obligation legal party.' USING ERRCODE='23514'; END IF;
        IF (NEW.kind='ADJUSTMENT') <> (NEW.corrects_id IS NOT NULL) THEN
            RAISE EXCEPTION 'Corrections preserve their original document.' USING ERRCODE='23514'; END IF;
        IF NEW.corrects_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM billing_financialdocument oldd WHERE oldd.id=NEW.corrects_id
            AND oldd.status='ISSUED' AND oldd.booking_id=NEW.booking_id AND oldd.currency=NEW.currency
            AND oldd.booking_charge_id IS NOT DISTINCT FROM NEW.booking_charge_id
            AND oldd.purchase_obligation_id IS NOT DISTINCT FROM NEW.purchase_obligation_id) THEN
            RAISE EXCEPTION 'Correction document cannot redirect historical obligations.' USING ERRCODE='23514'; END IF;
        IF TG_OP='UPDATE' AND OLD.status<>NEW.status AND NEW.status='ISSUED' THEN
            SELECT status INTO tax_status FROM billing_taxevidence WHERE id=NEW.tax_evidence_id;
            SELECT COALESCE(SUM(amount_minor),0) INTO total FROM billing_financialdocumentline WHERE document_id=NEW.id;
            IF tax_status<>'REVIEWED' OR total<>NEW.amount_minor OR NOT EXISTS(
                SELECT 1 FROM billing_financialdocumentline WHERE document_id=NEW.id) THEN
                RAISE EXCEPTION 'Issue requires sealed complete lines and explicit reviewed tax treatment.' USING ERRCODE='23514'; END IF;
            IF NEW.kind='UPFRONT_RECORD' AND NOT EXISTS(SELECT 1 FROM payments_bookingcharge o WHERE o.id=NEW.booking_charge_id
                AND o.amount_minor=NEW.amount_minor AND o.currency=NEW.currency) THEN
                RAISE EXCEPTION 'Upfront document must preserve obligation amount and currency.' USING ERRCODE='23514'; END IF;
            IF NEW.kind='TAX_INVOICE' AND NOT EXISTS(SELECT 1 FROM billing_purchaseobligation o JOIN billing_purchase p0 ON p0.id=o.purchase_id
                WHERE o.id=NEW.purchase_obligation_id AND p0.status='CONFIRMED' AND p0.jewellery_total_minor=NEW.amount_minor AND p0.currency=NEW.currency) THEN
                RAISE EXCEPTION 'Invoice must preserve the confirmed jewellery value.' USING ERRCODE='23514'; END IF;
            IF NEW.kind='DISPATCH' AND EXISTS(SELECT 1 FROM inventory_inventoryreservation held JOIN payments_paymentcontext c ON c.id=held.payment_context_id
                JOIN payments_bookingcharge charge ON charge.id=c.booking_charge_id WHERE charge.booking_id=NEW.booking_id AND held.status='COMMITTED'
                AND NOT EXISTS(SELECT 1 FROM billing_financialdocumentline line WHERE line.document_id=NEW.id AND line.reservation_id=held.id)) THEN
                RAISE EXCEPTION 'Issue seals the complete combined dispatch manifest.' USING ERRCODE='23514'; END IF;
        END IF;
    ELSIF TG_TABLE_NAME='billing_documentseries' THEN
        IF btrim(NEW.code)='' OR btrim(NEW.review_reference)='' OR NEW.scope_snapshot='{}' THEN
            RAISE EXCEPTION 'Numbering series requires reviewed entity scope.' USING ERRCODE='23514'; END IF;
    END IF;
    RETURN NEW;
END; $$;
CREATE OR REPLACE FUNCTION checkout_context_binding() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE charge payments_bookingcharge%ROWTYPE; booking trials_trialbooking%ROWTYPE;
DECLARE purchase_charge billing_purchaseobligation%ROWTYPE; purchase billing_purchase%ROWTYPE;
BEGIN

    IF NEW.purchase_obligation_id IS NOT NULL THEN
        SELECT * INTO purchase_charge FROM billing_purchaseobligation WHERE id=NEW.purchase_obligation_id FOR UPDATE;
        SELECT * INTO purchase FROM billing_purchase WHERE id=purchase_charge.purchase_id FOR UPDATE;
        IF TG_OP='INSERT' AND (purchase_charge.status<>'DUE' OR purchase.status<>'SEALED' OR NEW.opened_at>=purchase_charge.quote_expires_at
            OR NEW.expires_at<=statement_timestamp() OR NEW.policy_snapshot='{}') THEN
            RAISE EXCEPTION 'Final context requires accepted current purchase balance.' USING ERRCODE='23514'; END IF;
        IF TG_OP='UPDATE' AND OLD.status<>NEW.status THEN
            IF NEW.status='CAPTURING' AND (purchase_charge.status<>'DUE' OR purchase.status<>'SEALED'
                OR OLD.expires_at<=statement_timestamp() OR NEW.capture_started_at>=NEW.expires_at
                OR EXISTS(SELECT 1 FROM billing_purchaseitem i JOIN inventory_inventoryreservation r ON r.id=i.reservation_id
                    WHERE i.purchase_id=purchase.id AND r.status<>'COMMITTED')) THEN
                RAISE EXCEPTION 'Final capture requires valid protected purchase selection.' USING ERRCODE='23514'; END IF;
            IF NEW.status IN ('EXPIRED','CANCELLED') AND (
                (NEW.resolution_id IS NULL AND EXISTS(SELECT 1 FROM payments_paymentattempt WHERE context_id=NEW.id AND provider_order_id IS NOT NULL))
                OR (NEW.resolution_id IS NOT NULL AND NOT checkout_resolution_current(NEW.resolution_id,NEW.id))
                OR (NEW.status='EXPIRED' AND NEW.closed_at<NEW.expires_at)) THEN
                RAISE EXCEPTION 'Reconcile final provider outcome before closure.' USING ERRCODE='23514'; END IF;
            IF NEW.status='PAID' AND (purchase_charge.status<>'COLLECTED' OR NOT EXISTS(SELECT 1 FROM payments_paymentattempt
                WHERE context_id=NEW.id AND status='SUCCEEDED')) THEN
                RAISE EXCEPTION 'Paid final context requires exact verified collection.' USING ERRCODE='23514'; END IF;
        END IF;
        RETURN NEW;
    END IF;
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
CREATE OR REPLACE FUNCTION checkout_payment_allocation() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE receipt payments_gatewaytransaction%ROWTYPE; charge payments_bookingcharge%ROWTYPE;
DECLARE context payments_paymentcontext%ROWTYPE; allocated numeric; applied numeric;
DECLARE purchase_charge billing_purchaseobligation%ROWTYPE;
BEGIN

    IF NEW.purchase_obligation_id IS NOT NULL THEN
        SELECT * INTO purchase_charge FROM billing_purchaseobligation WHERE id=NEW.purchase_obligation_id FOR UPDATE;
        SELECT * INTO receipt FROM payments_gatewaytransaction WHERE id=NEW.gateway_transaction_id FOR UPDATE;
        SELECT c.* INTO context FROM payments_paymentcontext c JOIN payments_paymentattempt a ON a.context_id=c.id
            WHERE a.id=checkout_effective_attempt(receipt.id) FOR UPDATE OF c;
        IF receipt.transaction_kind<>'CAPTURE' OR receipt.currency<>NEW.currency OR purchase_charge.currency<>NEW.currency
            OR context.purchase_obligation_id IS DISTINCT FROM purchase_charge.id OR purchase_charge.status<>'DUE'
            OR context.status NOT IN ('CAPTURING','REVIEW_REQUIRED') THEN
            RAISE EXCEPTION 'Captured funds cannot cross upfront/final obligation boundaries.' USING ERRCODE='23514'; END IF;
        SELECT COALESCE(SUM(amount_minor),0) INTO allocated FROM payments_paymentallocation WHERE gateway_transaction_id=receipt.id;
        SELECT COALESCE(SUM(amount_minor),0) INTO applied FROM payments_paymentallocation WHERE purchase_obligation_id=purchase_charge.id;
        IF allocated+NEW.amount_minor>receipt.amount_minor OR applied+NEW.amount_minor>purchase_charge.amount_minor THEN
            RAISE EXCEPTION 'Final allocation exceeds captured receipt or purchase balance.' USING ERRCODE='23514'; END IF;
        RETURN NEW;
    END IF;
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
DROP FUNCTION checkout_obligation_rejected(text,bigint); DROP FUNCTION checkout_purchase_protected(bigint);
"""

def require_empty(apps,schema_editor):
    for table in ("payments_paymentcontext","billing_purchase","inventory_inventoryreservation"):
        with schema_editor.connection.cursor() as cursor:
            cursor.execute(f'SELECT EXISTS (SELECT 1 FROM "{table}")')
            if cursor.fetchone()[0]:
                raise IrreversibleError("Payment/custody evidence exists; use reviewed forward recovery.")

class Migration(migrations.Migration):
    dependencies=[("trials","0006_lifecycle_financial_integrity")]
    operations=[migrations.RunSQL(SQL,REVERSE_SQL),migrations.RunPython(migrations.RunPython.noop,require_empty)]
