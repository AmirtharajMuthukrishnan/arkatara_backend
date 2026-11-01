"""Approved persistence boundaries; no operational or provider workflows.
Refs: R-02, R-14..17, R-20, R-23..31; D-26; unresolved BD/CA/LR policies stay gated.
"""
from django.db import migrations
from django.db.migrations.exceptions import IrreversibleError

SQL = r"""

CREATE FUNCTION checkout_effective_attempt(receipt_id bigint) RETURNS bigint LANGUAGE sql STABLE AS $$
    SELECT COALESCE(t.attempt_id,m.attempt_id) FROM payments_gatewaytransaction t
        LEFT JOIN payments_gatewaytransactionmatch m ON m.gateway_transaction_id=t.id WHERE t.id=receipt_id
$$;

CREATE FUNCTION staff_scope_binding() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.hub_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM markets_hub WHERE id=NEW.hub_id AND market_id=NEW.market_id) THEN
        RAISE EXCEPTION 'Operational hub scope must belong to the declared city.' USING ERRCODE='23514'; END IF;
    IF btrim(NEW.reason)='' OR NEW.granted_at>statement_timestamp() THEN
        RAISE EXCEPTION 'Scope grant requires attributable observed reason/time.' USING ERRCODE='23514'; END IF;
    IF TG_OP='INSERT' AND (NEW.revoked_at IS NOT NULL OR NOT EXISTS(SELECT 1 FROM accounts_staffuser WHERE id=NEW.staff_id AND is_active AND is_staff)
        OR NOT EXISTS(SELECT 1 FROM accounts_staffuser WHERE id=NEW.granted_by_id AND is_active AND is_staff)) THEN
        RAISE EXCEPTION 'New scope grants require active staff principals.' USING ERRCODE='23514'; END IF;
    IF NEW.revoked_at IS NOT NULL AND (NEW.revoked_at>statement_timestamp() OR btrim(NEW.revocation_reason)='') THEN
        RAISE EXCEPTION 'Scope revocation requires observed attributable evidence.' USING ERRCODE='23514'; END IF;
    RETURN NEW;
END; $$;

CREATE FUNCTION resolution_receipt_binding() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE target bigint; receipt payments_gatewaytransaction%ROWTYPE;
BEGIN
    SELECT context_id INTO target FROM payments_paymentresolution WHERE id=NEW.resolution_id FOR SHARE;
    SELECT * INTO receipt FROM payments_gatewaytransaction WHERE id=NEW.gateway_transaction_id FOR SHARE;
    IF receipt.transaction_kind<>'CAPTURE' OR NOT EXISTS(SELECT 1 FROM payments_paymentattempt a
        WHERE a.id=checkout_effective_attempt(receipt.id) AND a.context_id=target)
        OR EXISTS(SELECT 1 FROM payments_paymentcontext WHERE resolution_id=NEW.resolution_id)
        OR EXISTS(SELECT 1 FROM inventory_inventoryreservation WHERE resolution_id=NEW.resolution_id) THEN
        RAISE EXCEPTION 'Resolution receipt membership must match its context and remain sealed after use.' USING ERRCODE='23514'; END IF;
    RETURN NEW;
END; $$;

CREATE FUNCTION billing_binding() RETURNS trigger LANGUAGE plpgsql AS $$
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

CREATE FUNCTION delivery_visit_binding() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE assignment delivery_deliveryassignment%ROWTYPE; proof delivery_arrivalotp%ROWTYPE;
BEGIN
    SELECT * INTO assignment FROM delivery_deliveryassignment WHERE id=NEW.assignment_id FOR SHARE;
    IF assignment.booking_id IS DISTINCT FROM NEW.booking_id OR NOT EXISTS(SELECT 1 FROM trials_trialbooking
        WHERE id=NEW.booking_id AND status='CONFIRMED') THEN
        RAISE EXCEPTION 'Visit requires a confirmed combined booking and its assignment.' USING ERRCODE='23514'; END IF;
    IF (NEW.status IN ('PREPARING','READY') AND (NEW.dispatched_at IS NOT NULL OR NEW.trial_started_at IS NOT NULL OR NEW.arrival_challenge_id IS NOT NULL))
        OR (NEW.status IN ('DISPATCHED','IN_PERSON_TRIAL','CONCLUDED') AND (NEW.dispatched_at IS NULL OR NEW.dispatched_at>statement_timestamp()))
        OR (NEW.trial_started_at IS NOT NULL AND (NEW.trial_started_at<NEW.dispatched_at OR NEW.trial_started_at>statement_timestamp()
            OR NEW.status NOT IN ('IN_PERSON_TRIAL','CONCLUDED') OR NEW.arrival_challenge_id IS NULL))
        OR (NEW.concluded_at IS NOT NULL AND (NEW.concluded_at<COALESCE(NEW.trial_started_at,NEW.dispatched_at) OR NEW.concluded_at>statement_timestamp())) THEN
        RAISE EXCEPTION 'Visit evidence must follow its observed lifecycle chronology.' USING ERRCODE='23514'; END IF;
    IF NEW.arrival_challenge_id IS NOT NULL THEN
        SELECT * INTO proof FROM delivery_arrivalotp WHERE id=NEW.arrival_challenge_id FOR SHARE;
        IF proof.assignment_id IS DISTINCT FROM NEW.assignment_id OR proof.consumed_at IS NULL OR proof.invalidated_at IS NOT NULL
            OR NEW.trial_started_at IS NULL OR NEW.trial_started_at<proof.consumed_at THEN
            RAISE EXCEPTION 'Trial requires the same assignment consumed Arrival OTP.' USING ERRCODE='23514'; END IF;
    END IF;
    IF TG_OP='UPDATE' AND NEW.status<>OLD.status THEN
        IF assignment.ended_at IS NOT NULL THEN RAISE EXCEPTION 'Ended assignment cannot advance a visit.' USING ERRCODE='23514'; END IF;
        IF NEW.status IN ('READY','DISPATCHED') AND NOT EXISTS(SELECT 1 FROM billing_financialdocument WHERE id=NEW.dispatch_document_id
            AND booking_id=NEW.booking_id AND kind='DISPATCH' AND status='ISSUED') THEN
            RAISE EXCEPTION 'Ready/dispatch requires reviewed issued dispatch evidence.' USING ERRCODE='23514'; END IF;
        IF NEW.status='DISPATCHED' AND NEW.dispatched_at IS NULL THEN
            RAISE EXCEPTION 'Dispatch requires observed time.' USING ERRCODE='23514'; END IF;
        IF NEW.status='IN_PERSON_TRIAL' AND NEW.arrival_challenge_id IS NULL THEN
            RAISE EXCEPTION 'Trial cannot start on an agent assertion.' USING ERRCODE='23514'; END IF;
        IF NEW.status='CONCLUDED' AND ((OLD.status='DISPATCHED' AND NEW.outcome NOT IN ('FAILED_VISIT','ABORTED'))
            OR (NEW.outcome IN ('SELECTED','NO_PURCHASE') AND NEW.trial_started_at IS NULL)) THEN
            RAISE EXCEPTION 'Visit outcome must preserve whether a verified trial occurred.' USING ERRCODE='23514'; END IF;
    END IF;
    RETURN NEW;
END; $$;

CREATE FUNCTION delivery_custody_binding() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE r inventory_inventoryreservation%ROWTYPE; v delivery_homevisit%ROWTYPE; prior delivery_reservationcustodyevent%ROWTYPE;
DECLARE booking_id bigint;
BEGIN
    SELECT * INTO r FROM inventory_inventoryreservation WHERE id=NEW.reservation_id FOR UPDATE;
    SELECT * INTO v FROM delivery_homevisit WHERE id=NEW.visit_id FOR UPDATE;
    SELECT q.booking_id INTO booking_id FROM payments_bookingcharge q JOIN payments_paymentcontext c ON c.booking_charge_id=q.id WHERE c.id=r.payment_context_id;
    IF r.status<>'COMMITTED' OR booking_id IS DISTINCT FROM v.booking_id OR btrim(NEW.reason)='' OR btrim(NEW.evidence_reference)=''
        OR NEW.policy_snapshot='{}' OR NEW.observed_at>statement_timestamp() OR NOT EXISTS(SELECT 1 FROM accounts_staffuser WHERE id=NEW.actor_id AND is_active) THEN
        RAISE EXCEPTION 'Custody requires committed visit stock and attributable reviewed observation.' USING ERRCODE='23514'; END IF;
    IF NEW.previous_id IS NOT NULL THEN
        SELECT * INTO prior FROM delivery_reservationcustodyevent WHERE id=NEW.previous_id FOR SHARE;
        IF prior.reservation_id IS DISTINCT FROM NEW.reservation_id OR prior.visit_id IS DISTINCT FROM NEW.visit_id OR NEW.observed_at<prior.observed_at THEN
            RAISE EXCEPTION 'Custody chain cannot switch piece, visit or move backwards.' USING ERRCODE='23514'; END IF;
    END IF;
    IF NOT (
        (NEW.kind='DISPATCH' AND NEW.previous_id IS NULL AND v.status IN ('READY','DISPATCHED'))
        OR (NEW.kind='RETURNING' AND prior.kind='DISPATCH' AND v.status IN ('DISPATCHED','IN_PERSON_TRIAL','CONCLUDED'))
        OR (NEW.kind='HUB_RECEIPT' AND prior.kind='RETURNING')
        OR (NEW.kind IN ('QC_PASS','QC_FAIL') AND prior.kind='HUB_RECEIPT')
        OR (NEW.kind='SALE' AND prior.kind='DISPATCH' AND v.trial_started_at IS NOT NULL)
        OR (NEW.kind='EXCEPTION' AND NEW.previous_id IS NOT NULL AND prior.kind NOT IN ('SALE','QC_PASS','QC_FAIL','EXCEPTION'))
    ) THEN RAISE EXCEPTION 'Invalid observed custody sequence.' USING ERRCODE='23514'; END IF;
    IF NEW.kind='SALE' AND NOT EXISTS(SELECT 1 FROM billing_purchaseitem i JOIN billing_purchase p ON p.id=i.purchase_id
        WHERE i.id=NEW.purchase_item_id AND i.reservation_id=r.id AND p.status='CONFIRMED') THEN
        RAISE EXCEPTION 'Sale requires exact financially confirmed purchase piece.' USING ERRCODE='23514'; END IF;
    RETURN NEW;
END; $$;

CREATE FUNCTION delivery_custody_state(kind text) RETURNS text LANGUAGE sql IMMUTABLE AS $$
    SELECT CASE kind WHEN 'DISPATCH' THEN 'OUT_FOR_DELIVERY' WHEN 'RETURNING' THEN 'RETURNING_TO_HUB'
        WHEN 'HUB_RECEIPT' THEN 'QC_PENDING' WHEN 'QC_PASS' THEN 'AVAILABLE' WHEN 'QC_FAIL' THEN 'QUARANTINED'
        WHEN 'SALE' THEN 'SOLD' ELSE 'QUARANTINED' END
$$;
CREATE OR REPLACE FUNCTION checkout_protect_reserved_unit() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE observation delivery_reservationcustodyevent%ROWTYPE;
BEGIN
    IF ROW(OLD.variant_id,OLD.hub_id) IS DISTINCT FROM ROW(NEW.variant_id,NEW.hub_id) THEN
        RAISE EXCEPTION 'Physical piece identity and city cannot change.' USING ERRCODE='23514'; END IF;
    IF OLD.status='SOLD' AND ROW(OLD.status,OLD.custody) IS DISTINCT FROM ROW(NEW.status,NEW.custody) THEN
        RAISE EXCEPTION 'Sold stock requires separately reviewed after-sale return evidence.' USING ERRCODE='23514'; END IF;
    IF ROW(OLD.status,OLD.custody) IS DISTINCT FROM ROW(NEW.status,NEW.custody) AND EXISTS(
        SELECT 1 FROM inventory_inventoryreservation WHERE unit_id=NEW.id AND status IN ('HELD','CAPTURE_PENDING','COMMITTED')) THEN
        SELECT e.* INTO observation FROM delivery_reservationcustodyevent e JOIN inventory_inventoryreservation r ON r.id=e.reservation_id
            WHERE r.unit_id=NEW.id AND r.status='COMMITTED' AND NOT EXISTS(SELECT 1 FROM delivery_reservationcustodyevent n WHERE n.previous_id=e.id);
        IF observation.id IS NULL OR NEW.status IS DISTINCT FROM delivery_custody_state(observation.kind)
            OR (observation.kind IN ('DISPATCH','RETURNING') AND NEW.custody<>'AGENT')
            OR (observation.kind IN ('HUB_RECEIPT','QC_PASS','QC_FAIL') AND NEW.custody<>'HUB')
            OR (observation.kind='SALE' AND NEW.custody<>'CUSTOMER')
            OR (observation.kind='EXCEPTION' AND NEW.custody<>OLD.custody) THEN
            RAISE EXCEPTION 'Reserved disposition requires its recorded physical observation.' USING ERRCODE='23514'; END IF;
    END IF;
    RETURN NEW;
END; $$;
CREATE FUNCTION delivery_custody_consistency() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE unit inventory_inventoryunit%ROWTYPE; observation delivery_reservationcustodyevent%ROWTYPE;
BEGIN
    SELECT * INTO observation FROM delivery_reservationcustodyevent WHERE id=NEW.id;
    IF EXISTS(SELECT 1 FROM delivery_reservationcustodyevent WHERE previous_id=NEW.id) THEN RETURN NULL; END IF;
    SELECT u.* INTO unit FROM inventory_inventoryunit u JOIN inventory_inventoryreservation r ON r.unit_id=u.id WHERE r.id=NEW.reservation_id;
    IF unit.status IS DISTINCT FROM delivery_custody_state(observation.kind)
        OR (observation.kind IN ('DISPATCH','RETURNING') AND unit.custody<>'AGENT')
        OR (observation.kind IN ('HUB_RECEIPT','QC_PASS','QC_FAIL') AND unit.custody<>'HUB')
        OR (observation.kind='SALE' AND unit.custody<>'CUSTOMER') THEN
        RAISE EXCEPTION 'Observation and authoritative physical stock must commit together.' USING ERRCODE='23514'; END IF;
    RETURN NULL;
END; $$;
CREATE CONSTRAINT TRIGGER custody_stock_consistency AFTER INSERT ON delivery_reservationcustodyevent
    DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION delivery_custody_consistency();

CREATE FUNCTION financial_snapshot_objects() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE facts jsonb; field text;
BEGIN
    facts:=to_jsonb(NEW);
    FOREACH field IN ARRAY ARRAY['seller_snapshot','customer_snapshot','balance_snapshot','product_snapshot','price_snapshot',
        'scope_snapshot','issuer_snapshot','recipient_snapshot','snapshot','treatment_snapshot','policy_snapshot'] LOOP
        IF facts ? field AND (jsonb_typeof(facts->field)<>'object' OR
            (facts->field='{}' AND NOT (field='treatment_snapshot' AND facts->>'status'='UNCONFIGURED'))) THEN
            RAISE EXCEPTION 'Structured historical snapshot evidence required.' USING ERRCODE='23514'; END IF;
    END LOOP;
    IF TG_TABLE_NAME='billing_taxevidence' THEN
        IF NEW.status='REVIEWED' AND (btrim(NEW.review_reference)='' OR NEW.reviewed_at>statement_timestamp()) THEN
            RAISE EXCEPTION 'Tax review requires attributable observed evidence.' USING ERRCODE='23514'; END IF;
    END IF;
    RETURN NEW;
END; $$;

CREATE FUNCTION visit_manifest_consistency() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE visit delivery_homevisit%ROWTYPE;
BEGIN
    SELECT * INTO visit FROM delivery_homevisit WHERE id=NEW.id;
    IF visit.status IN ('DISPATCHED','IN_PERSON_TRIAL','CONCLUDED') AND EXISTS(
        SELECT 1 FROM inventory_inventoryreservation r JOIN payments_paymentcontext c ON c.id=r.payment_context_id
        JOIN payments_bookingcharge q ON q.id=c.booking_charge_id WHERE q.booking_id=visit.booking_id AND r.committed_at IS NOT NULL
        AND NOT EXISTS(SELECT 1 FROM delivery_reservationcustodyevent e WHERE e.reservation_id=r.id AND e.visit_id=visit.id AND e.kind='DISPATCH')) THEN
        RAISE EXCEPTION 'Combined dispatch must preserve every committed piece in one visit manifest.' USING ERRCODE='23514'; END IF;
    RETURN NULL;
END; $$;
CREATE CONSTRAINT TRIGGER visit_manifest_consistency AFTER INSERT OR UPDATE ON delivery_homevisit
    DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION visit_manifest_consistency();

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
CREATE OR REPLACE FUNCTION checkout_attempt_binding() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE context payments_paymentcontext%ROWTYPE; charge payments_bookingcharge%ROWTYPE;
DECLARE purchase_charge billing_purchaseobligation%ROWTYPE; total numeric;
BEGIN

    SELECT * INTO context FROM payments_paymentcontext WHERE id=NEW.context_id FOR UPDATE;
    IF context.purchase_obligation_id IS NOT NULL THEN
        SELECT * INTO purchase_charge FROM billing_purchaseobligation WHERE id=context.purchase_obligation_id FOR UPDATE;
        IF ROW(NEW.amount_minor,NEW.currency) IS DISTINCT FROM ROW(purchase_charge.amount_minor,purchase_charge.currency)
            OR btrim(NEW.provider)='' OR btrim(NEW.provider_account)='' THEN
            RAISE EXCEPTION 'Final attempt must bind exact purchase balance.' USING ERRCODE='23514'; END IF;
        IF TG_OP='INSERT' AND (context.status<>'OPEN' OR purchase_charge.status<>'DUE') THEN
            RAISE EXCEPTION 'Final attempt requires an open accepted context.' USING ERRCODE='23514'; END IF;
        IF NEW.status IN ('PENDING','AUTHORIZED','CAPTURE_PENDING','SUCCEEDED') AND (NEW.provider_order_id IS NULL OR btrim(NEW.provider_order_id)='') THEN
            RAISE EXCEPTION 'Provider order identity required.' USING ERRCODE='23514'; END IF;
        IF TG_OP='UPDATE' THEN
            IF OLD.last_verified_at IS NOT NULL AND (NEW.last_verified_at IS NULL OR NEW.last_verified_at<OLD.last_verified_at) THEN
                RAISE EXCEPTION 'Provider observation cannot move backwards.' USING ERRCODE='23514'; END IF;
            IF OLD.status<>NEW.status AND NEW.status='AUTHORIZED' AND NOT EXISTS(SELECT 1 FROM payments_gatewaytransaction t
                WHERE checkout_effective_attempt(t.id)=NEW.id AND t.transaction_kind='AUTHORISATION' AND t.amount_minor=NEW.amount_minor AND t.currency=NEW.currency) THEN
                RAISE EXCEPTION 'Final authorization requires verified evidence.' USING ERRCODE='23514'; END IF;
            IF OLD.status<>NEW.status AND NEW.status='CAPTURE_PENDING' AND (context.status<>'CAPTURING' OR purchase_charge.status<>'DUE') THEN
                RAISE EXCEPTION 'Final capture requires protected live balance.' USING ERRCODE='23514'; END IF;
            IF OLD.status<>NEW.status AND NEW.status='SUCCEEDED' THEN
                SELECT COALESCE(SUM(a.amount_minor),0) INTO total FROM payments_paymentallocation a JOIN payments_gatewaytransaction t ON t.id=a.gateway_transaction_id
                    WHERE checkout_effective_attempt(t.id)=NEW.id AND a.purchase_obligation_id=purchase_charge.id AND t.transaction_kind='CAPTURE' AND a.currency=NEW.currency;
                IF context.status NOT IN ('CAPTURING','REVIEW_REQUIRED') OR purchase_charge.status<>'DUE'
                    OR NEW.last_verified_at IS NULL OR total<>NEW.amount_minor THEN
                    RAISE EXCEPTION 'Final success requires captured allocated balance.' USING ERRCODE='23514'; END IF;
            END IF;
        END IF;
        RETURN NEW;
    END IF;
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
CREATE TRIGGER checkout_serialization BEFORE INSERT OR UPDATE OR DELETE ON accounts_staffscopegrant FOR EACH STATEMENT EXECUTE FUNCTION checkout_statement_lock();
CREATE TRIGGER checkout_identity BEFORE INSERT OR UPDATE OR DELETE ON accounts_staffscopegrant FOR EACH ROW EXECUTE FUNCTION checkout_guard_record('revoked_at revoked_by_id revocation_reason','revoked_at revoked_by_id revocation_reason','','{}');
CREATE TRIGGER checkout_binding BEFORE INSERT OR UPDATE ON accounts_staffscopegrant FOR EACH ROW EXECUTE FUNCTION staff_scope_binding();
CREATE TRIGGER checkout_serialization BEFORE INSERT OR UPDATE OR DELETE ON billing_purchase FOR EACH STATEMENT EXECUTE FUNCTION checkout_statement_lock();
CREATE TRIGGER checkout_identity BEFORE INSERT OR UPDATE OR DELETE ON billing_purchase FOR EACH ROW EXECUTE FUNCTION checkout_guard_record('status sealed_at confirmed_at','sealed_at confirmed_at','DRAFT','{"DRAFT": ["SEALED", "VOID"], "SEALED": ["CONFIRMED", "VOID"]}');
CREATE TRIGGER checkout_binding BEFORE INSERT OR UPDATE ON billing_purchase FOR EACH ROW EXECUTE FUNCTION billing_binding();
CREATE TRIGGER snapshot_objects BEFORE INSERT OR UPDATE ON billing_purchase FOR EACH ROW EXECUTE FUNCTION financial_snapshot_objects();
CREATE TRIGGER checkout_serialization BEFORE INSERT OR UPDATE OR DELETE ON billing_purchaseobligation FOR EACH STATEMENT EXECUTE FUNCTION checkout_statement_lock();
CREATE TRIGGER checkout_identity BEFORE INSERT OR UPDATE OR DELETE ON billing_purchaseobligation FOR EACH ROW EXECUTE FUNCTION checkout_guard_record('status accepted_at','accepted_at','QUOTED','{"QUOTED": ["DUE", "VOID"], "DUE": ["COLLECTED", "VOID"]}');
CREATE TRIGGER checkout_binding BEFORE INSERT OR UPDATE ON billing_purchaseobligation FOR EACH ROW EXECUTE FUNCTION billing_binding();
CREATE TRIGGER snapshot_objects BEFORE INSERT OR UPDATE ON billing_purchaseobligation FOR EACH ROW EXECUTE FUNCTION financial_snapshot_objects();
CREATE TRIGGER checkout_serialization BEFORE INSERT OR UPDATE OR DELETE ON billing_financialdocument FOR EACH STATEMENT EXECUTE FUNCTION checkout_statement_lock();
CREATE TRIGGER checkout_identity BEFORE INSERT OR UPDATE OR DELETE ON billing_financialdocument FOR EACH ROW EXECUTE FUNCTION checkout_guard_record('status number issued_at','number issued_at','DRAFT','{"DRAFT": ["ISSUED"]}');
CREATE TRIGGER checkout_binding BEFORE INSERT OR UPDATE ON billing_financialdocument FOR EACH ROW EXECUTE FUNCTION billing_binding();
CREATE TRIGGER snapshot_objects BEFORE INSERT OR UPDATE ON billing_financialdocument FOR EACH ROW EXECUTE FUNCTION financial_snapshot_objects();
CREATE TRIGGER checkout_serialization BEFORE INSERT OR UPDATE OR DELETE ON delivery_homevisit FOR EACH STATEMENT EXECUTE FUNCTION checkout_statement_lock();
CREATE TRIGGER checkout_identity BEFORE INSERT OR UPDATE OR DELETE ON delivery_homevisit FOR EACH ROW EXECUTE FUNCTION checkout_guard_record('status dispatch_document_id arrival_challenge_id dispatched_at trial_started_at concluded_at outcome','dispatch_document_id arrival_challenge_id dispatched_at trial_started_at concluded_at outcome','PREPARING','{"PREPARING": ["READY"], "READY": ["DISPATCHED"], "DISPATCHED": ["IN_PERSON_TRIAL", "CONCLUDED"], "IN_PERSON_TRIAL": ["CONCLUDED"]}');
CREATE TRIGGER checkout_binding BEFORE INSERT OR UPDATE ON delivery_homevisit FOR EACH ROW EXECUTE FUNCTION delivery_visit_binding();
CREATE TRIGGER checkout_serialization BEFORE INSERT OR UPDATE OR DELETE ON billing_taxevidence FOR EACH STATEMENT EXECUTE FUNCTION checkout_statement_lock();
CREATE TRIGGER checkout_history BEFORE UPDATE OR DELETE ON billing_taxevidence FOR EACH ROW EXECUTE FUNCTION trials_reject_evidence_mutation();
CREATE TRIGGER checkout_binding BEFORE INSERT ON billing_taxevidence FOR EACH ROW EXECUTE FUNCTION billing_binding();
CREATE TRIGGER snapshot_objects BEFORE INSERT OR UPDATE ON billing_taxevidence FOR EACH ROW EXECUTE FUNCTION financial_snapshot_objects();
CREATE TRIGGER checkout_serialization BEFORE INSERT OR UPDATE OR DELETE ON billing_documentseries FOR EACH STATEMENT EXECUTE FUNCTION checkout_statement_lock();
CREATE TRIGGER checkout_history BEFORE UPDATE OR DELETE ON billing_documentseries FOR EACH ROW EXECUTE FUNCTION trials_reject_evidence_mutation();
CREATE TRIGGER checkout_binding BEFORE INSERT ON billing_documentseries FOR EACH ROW EXECUTE FUNCTION billing_binding();
CREATE TRIGGER snapshot_objects BEFORE INSERT OR UPDATE ON billing_documentseries FOR EACH ROW EXECUTE FUNCTION financial_snapshot_objects();
CREATE TRIGGER checkout_serialization BEFORE INSERT OR UPDATE OR DELETE ON billing_purchaseitem FOR EACH STATEMENT EXECUTE FUNCTION checkout_statement_lock();
CREATE TRIGGER checkout_history BEFORE UPDATE OR DELETE ON billing_purchaseitem FOR EACH ROW EXECUTE FUNCTION trials_reject_evidence_mutation();
CREATE TRIGGER checkout_binding BEFORE INSERT ON billing_purchaseitem FOR EACH ROW EXECUTE FUNCTION billing_binding();
CREATE TRIGGER snapshot_objects BEFORE INSERT OR UPDATE ON billing_purchaseitem FOR EACH ROW EXECUTE FUNCTION financial_snapshot_objects();
CREATE TRIGGER checkout_serialization BEFORE INSERT OR UPDATE OR DELETE ON billing_financialdocumentline FOR EACH STATEMENT EXECUTE FUNCTION checkout_statement_lock();
CREATE TRIGGER checkout_history BEFORE UPDATE OR DELETE ON billing_financialdocumentline FOR EACH ROW EXECUTE FUNCTION trials_reject_evidence_mutation();
CREATE TRIGGER checkout_binding BEFORE INSERT ON billing_financialdocumentline FOR EACH ROW EXECUTE FUNCTION billing_binding();
CREATE TRIGGER snapshot_objects BEFORE INSERT OR UPDATE ON billing_financialdocumentline FOR EACH ROW EXECUTE FUNCTION financial_snapshot_objects();
CREATE TRIGGER checkout_serialization BEFORE INSERT OR UPDATE OR DELETE ON delivery_reservationcustodyevent FOR EACH STATEMENT EXECUTE FUNCTION checkout_statement_lock();
CREATE TRIGGER checkout_history BEFORE UPDATE OR DELETE ON delivery_reservationcustodyevent FOR EACH ROW EXECUTE FUNCTION trials_reject_evidence_mutation();
CREATE TRIGGER checkout_binding BEFORE INSERT ON delivery_reservationcustodyevent FOR EACH ROW EXECUTE FUNCTION delivery_custody_binding();
CREATE TRIGGER snapshot_objects BEFORE INSERT OR UPDATE ON delivery_reservationcustodyevent FOR EACH ROW EXECUTE FUNCTION financial_snapshot_objects();
CREATE TRIGGER checkout_serialization BEFORE INSERT OR UPDATE OR DELETE ON payments_paymentresolutionreceipt FOR EACH STATEMENT EXECUTE FUNCTION checkout_statement_lock();
CREATE TRIGGER checkout_history BEFORE UPDATE OR DELETE ON payments_paymentresolutionreceipt FOR EACH ROW EXECUTE FUNCTION trials_reject_evidence_mutation();
CREATE TRIGGER checkout_binding BEFORE INSERT ON payments_paymentresolutionreceipt FOR EACH ROW EXECUTE FUNCTION resolution_receipt_binding();
DROP TRIGGER checkout_identity ON inventory_inventoryreservation; CREATE TRIGGER checkout_identity BEFORE INSERT OR UPDATE OR DELETE ON inventory_inventoryreservation FOR EACH ROW EXECUTE FUNCTION checkout_guard_record('status capture_started_at committed_at released_at release_reason resolution_reference resolution_id disposition_id','capture_started_at committed_at released_at release_reason resolution_reference resolution_id disposition_id','HELD','{"HELD": ["CAPTURE_PENDING", "EXPIRED", "RELEASED"], "CAPTURE_PENDING": ["COMMITTED", "RELEASED"], "COMMITTED": ["RELEASED"]}');
"""
REVERSE_SQL = r"""

DROP TRIGGER checkout_serialization ON accounts_staffscopegrant; DROP TRIGGER checkout_binding ON accounts_staffscopegrant; DROP TRIGGER checkout_identity ON accounts_staffscopegrant;
DROP TRIGGER checkout_serialization ON billing_purchase; DROP TRIGGER checkout_binding ON billing_purchase; DROP TRIGGER checkout_identity ON billing_purchase;
DROP TRIGGER snapshot_objects ON billing_purchase;
DROP TRIGGER checkout_serialization ON billing_purchaseobligation; DROP TRIGGER checkout_binding ON billing_purchaseobligation; DROP TRIGGER checkout_identity ON billing_purchaseobligation;
DROP TRIGGER snapshot_objects ON billing_purchaseobligation;
DROP TRIGGER checkout_serialization ON billing_financialdocument; DROP TRIGGER checkout_binding ON billing_financialdocument; DROP TRIGGER checkout_identity ON billing_financialdocument;
DROP TRIGGER snapshot_objects ON billing_financialdocument;
DROP TRIGGER checkout_serialization ON delivery_homevisit; DROP TRIGGER checkout_binding ON delivery_homevisit; DROP TRIGGER checkout_identity ON delivery_homevisit;
DROP TRIGGER checkout_serialization ON billing_taxevidence; DROP TRIGGER checkout_binding ON billing_taxevidence; DROP TRIGGER checkout_history ON billing_taxevidence;
DROP TRIGGER snapshot_objects ON billing_taxevidence;
DROP TRIGGER checkout_serialization ON billing_documentseries; DROP TRIGGER checkout_binding ON billing_documentseries; DROP TRIGGER checkout_history ON billing_documentseries;
DROP TRIGGER snapshot_objects ON billing_documentseries;
DROP TRIGGER checkout_serialization ON billing_purchaseitem; DROP TRIGGER checkout_binding ON billing_purchaseitem; DROP TRIGGER checkout_history ON billing_purchaseitem;
DROP TRIGGER snapshot_objects ON billing_purchaseitem;
DROP TRIGGER checkout_serialization ON billing_financialdocumentline; DROP TRIGGER checkout_binding ON billing_financialdocumentline; DROP TRIGGER checkout_history ON billing_financialdocumentline;
DROP TRIGGER snapshot_objects ON billing_financialdocumentline;
DROP TRIGGER checkout_serialization ON delivery_reservationcustodyevent; DROP TRIGGER checkout_binding ON delivery_reservationcustodyevent; DROP TRIGGER checkout_history ON delivery_reservationcustodyevent;
DROP TRIGGER snapshot_objects ON delivery_reservationcustodyevent;
DROP TRIGGER checkout_serialization ON payments_paymentresolutionreceipt; DROP TRIGGER checkout_binding ON payments_paymentresolutionreceipt; DROP TRIGGER checkout_history ON payments_paymentresolutionreceipt;
DROP TRIGGER custody_stock_consistency ON delivery_reservationcustodyevent;
DROP TRIGGER visit_manifest_consistency ON delivery_homevisit; DROP FUNCTION visit_manifest_consistency(); DROP FUNCTION financial_snapshot_objects();
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
CREATE OR REPLACE FUNCTION checkout_resolution_current(target_resolution bigint, target_context bigint) RETURNS boolean LANGUAGE sql VOLATILE AS $$
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
CREATE OR REPLACE FUNCTION checkout_protect_reserved_unit() RETURNS trigger LANGUAGE plpgsql AS $$
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
DROP TRIGGER checkout_identity ON inventory_inventoryreservation; CREATE TRIGGER checkout_identity BEFORE INSERT OR UPDATE OR DELETE ON inventory_inventoryreservation FOR EACH ROW EXECUTE FUNCTION checkout_guard_record('status capture_started_at committed_at released_at release_reason resolution_reference resolution_id','capture_started_at committed_at released_at release_reason resolution_reference resolution_id','HELD','{"HELD":["CAPTURE_PENDING","EXPIRED","RELEASED"],"CAPTURE_PENDING":["COMMITTED","RELEASED"]}');
DROP FUNCTION resolution_receipt_binding(); DROP FUNCTION staff_scope_binding(); DROP FUNCTION billing_binding(); DROP FUNCTION delivery_visit_binding(); DROP FUNCTION delivery_custody_binding(); DROP FUNCTION delivery_custody_consistency(); DROP FUNCTION delivery_custody_state(text); DROP FUNCTION checkout_effective_attempt(bigint);
"""

def require_empty(apps, schema_editor):
    for table in ("accounts_staffscopegrant", "billing_purchase", "billing_financialdocument", "delivery_homevisit", "delivery_reservationcustodyevent"):
        with schema_editor.connection.cursor() as cursor:
            cursor.execute(f'SELECT EXISTS (SELECT 1 FROM "{table}")')
            if cursor.fetchone()[0]:
                raise IrreversibleError("Financial/custody evidence exists; use reviewed forward recovery.")

class Migration(migrations.Migration):
    dependencies = [
        ("trials","0005_review_integrity"),
        ("accounts","0004_staffscopegrant"),
        ("payments","0004_paymentresolutionreceipt_and_more"),
        ("billing","0002_financialdocumentline_purchase_item_and_more"),
        ("delivery","0004_homevisit_reservationcustodyevent_and_more"),
        ("inventory","0008_remove_inventoryreservation_reservation_commit_time_and_more"),
    ]
    operations = [migrations.RunSQL(SQL,REVERSE_SQL), migrations.RunPython(migrations.RunPython.noop,require_empty)]
