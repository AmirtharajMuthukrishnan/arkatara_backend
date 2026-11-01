"""Frozen, hand-maintained visit attribution and undispatched disposition guards.

Refs: BUSINESS_RULES.md R-02, R-14, R-17, R-30; DECISIONS.md D-24, BD-07, BD-09.
Physical commitment closure does not cancel a confirmed booking or classify its money.
"""

from importlib import import_module

from django.db import migrations
from django.db.migrations.exceptions import IrreversibleError


SQL = r"""
CREATE FUNCTION delivery_assignment_visit_binding() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.ended_at IS NULL AND NEW.ended_at IS NOT NULL AND EXISTS(
        SELECT 1 FROM delivery_homevisit v WHERE v.assignment_id=NEW.id
            AND (v.status IN ('DISPATCHED','IN_PERSON_TRIAL') OR
                (v.status IN ('PREPARING','READY') AND EXISTS(
                    SELECT 1 FROM delivery_reservationcustodyevent WHERE visit_id=v.id)))) THEN
        RAISE EXCEPTION 'Dispatched responsibility requires reviewed custody transfer or visit closure.' USING ERRCODE='23514'; END IF;
    RETURN NEW;
END; $$;

CREATE FUNCTION delivery_visit_evidence_binding() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE v delivery_homevisit%ROWTYPE; prior delivery_visitevidence%ROWTYPE;
DECLARE assignment delivery_deliveryassignment%ROWTYPE; old_assignment delivery_deliveryassignment%ROWTYPE;
BEGIN
    SELECT * INTO v FROM delivery_homevisit WHERE id=NEW.visit_id FOR UPDATE;
    SELECT * INTO assignment FROM delivery_deliveryassignment WHERE id=NEW.assignment_id FOR SHARE;
    IF assignment.booking_id IS DISTINCT FROM v.booking_id OR NEW.observed_at<assignment.assigned_at
        OR NEW.observed_at<v.created_at OR NEW.observed_at>statement_timestamp()
        OR btrim(NEW.reason)='' OR btrim(NEW.evidence_reference)=''
        OR jsonb_typeof(NEW.policy_snapshot)<>'object' OR NEW.policy_snapshot='{}'
        OR NOT EXISTS(SELECT 1 FROM accounts_staffuser WHERE id=NEW.actor_id AND is_active)
        OR NEW.status NOT IN ('PREPARING','READY','DISPATCHED','IN_PERSON_TRIAL','CONCLUDED','CLOSED_BEFORE_DISPATCH')
        OR NEW.kind NOT IN ('STATE','REASSIGN','PREDISPATCH_CLOSE','OUTCOME_CORRECTION') THEN
        RAISE EXCEPTION 'Visit evidence requires attributable compatible observed facts.' USING ERRCODE='23514'; END IF;
    IF (NEW.status IN ('CONCLUDED','CLOSED_BEFORE_DISPATCH') AND NEW.outcome NOT IN ('SELECTED','NO_PURCHASE','FAILED_VISIT','ABORTED'))
        OR (NEW.status NOT IN ('CONCLUDED','CLOSED_BEFORE_DISPATCH') AND NEW.outcome<>'')
        OR (NEW.status='CLOSED_BEFORE_DISPATCH' AND NEW.outcome<>'ABORTED')
        OR (NEW.outcome IN ('SELECTED','NO_PURCHASE') AND v.trial_started_at IS NULL) THEN
        RAISE EXCEPTION 'Outcome evidence must preserve whether a verified trial occurred.' USING ERRCODE='23514'; END IF;
    IF NEW.previous_id IS NULL THEN
        IF v.current_evidence_id IS NOT NULL OR NEW.kind<>'STATE'
            OR ROW(NEW.assignment_id,NEW.status,NEW.outcome) IS DISTINCT FROM ROW(v.assignment_id,v.status,v.outcome) THEN
            RAISE EXCEPTION 'Initial evidence must describe this visit projection.' USING ERRCODE='23514'; END IF;
    ELSE
        SELECT * INTO prior FROM delivery_visitevidence WHERE id=NEW.previous_id FOR SHARE;
        IF prior.visit_id IS DISTINCT FROM v.id OR NEW.previous_id IS DISTINCT FROM v.current_evidence_id
            OR NEW.observed_at<prior.observed_at THEN
            RAISE EXCEPTION 'Visit evidence must follow its current chain chronologically.' USING ERRCODE='23514'; END IF;
        IF NEW.kind='REASSIGN' THEN
            SELECT * INTO old_assignment FROM delivery_deliveryassignment WHERE id=v.assignment_id FOR SHARE;
            IF v.status NOT IN ('PREPARING','READY') OR v.dispatched_at IS NOT NULL OR v.arrival_challenge_id IS NOT NULL
                OR EXISTS(SELECT 1 FROM delivery_reservationcustodyevent WHERE visit_id=v.id)
                OR assignment.ended_at IS NOT NULL OR old_assignment.ended_at IS NULL
                OR assignment.assigned_at<old_assignment.ended_at OR NEW.observed_at<old_assignment.ended_at
                OR NEW.assignment_id=v.assignment_id OR NEW.status<>v.status OR NEW.outcome<>prior.outcome THEN
                RAISE EXCEPTION 'Reassignment requires preserved superseded preparation responsibility.' USING ERRCODE='23514'; END IF;
        ELSIF NEW.kind='PREDISPATCH_CLOSE' THEN
            IF v.status NOT IN ('PREPARING','READY') OR NEW.status<>'CLOSED_BEFORE_DISPATCH'
                OR NEW.assignment_id<>v.assignment_id OR v.dispatched_at IS NOT NULL
                OR v.arrival_challenge_id IS NOT NULL OR EXISTS(SELECT 1 FROM delivery_reservationcustodyevent WHERE visit_id=v.id) THEN
                RAISE EXCEPTION 'Undispatched closure cannot erase physical dispatch or arrival.' USING ERRCODE='23514'; END IF;
        ELSIF NEW.kind='OUTCOME_CORRECTION' THEN
            IF v.status NOT IN ('CONCLUDED','CLOSED_BEFORE_DISPATCH') OR NEW.status<>v.status
                OR NEW.assignment_id<>v.assignment_id OR NEW.outcome=prior.outcome THEN
                RAISE EXCEPTION 'Outcome corrections retain original closure and append different observed facts.' USING ERRCODE='23514'; END IF;
        ELSIF NEW.kind='STATE' THEN
            IF NEW.assignment_id<>v.assignment_id OR assignment.ended_at IS NOT NULL
                OR NEW.status='CLOSED_BEFORE_DISPATCH'
                OR (NEW.status=v.status AND NEW.outcome<>prior.outcome)
                OR (NEW.status<>v.status AND NOT (
                    (v.status='PREPARING' AND NEW.status='READY') OR (v.status='READY' AND NEW.status='DISPATCHED')
                    OR (v.status='DISPATCHED' AND NEW.status IN ('IN_PERSON_TRIAL','CONCLUDED'))
                    OR (v.status='IN_PERSON_TRIAL' AND NEW.status='CONCLUDED'))) THEN
                RAISE EXCEPTION 'Progression evidence must follow the current open assignment and lifecycle.' USING ERRCODE='23514'; END IF;
        END IF;
    END IF;
    RETURN NEW;
END; $$;

CREATE OR REPLACE FUNCTION delivery_visit_binding() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE assignment delivery_deliveryassignment%ROWTYPE; proof delivery_arrivalotp%ROWTYPE;
DECLARE e delivery_visitevidence%ROWTYPE;
BEGIN
    SELECT * INTO assignment FROM delivery_deliveryassignment WHERE id=NEW.assignment_id FOR SHARE;
    IF assignment.booking_id IS DISTINCT FROM NEW.booking_id OR NOT EXISTS(SELECT 1 FROM trials_trialbooking
        WHERE id=NEW.booking_id AND status='CONFIRMED') THEN
        RAISE EXCEPTION 'Visit requires a confirmed combined booking and its assignment.' USING ERRCODE='23514'; END IF;
    IF (NEW.status IN ('PREPARING','READY','CLOSED_BEFORE_DISPATCH') AND
            (NEW.dispatched_at IS NOT NULL OR NEW.trial_started_at IS NOT NULL OR NEW.arrival_challenge_id IS NOT NULL))
        OR (NEW.status IN ('DISPATCHED','IN_PERSON_TRIAL','CONCLUDED') AND (NEW.dispatched_at IS NULL OR NEW.dispatched_at>statement_timestamp()))
        OR (NEW.trial_started_at IS NOT NULL AND (NEW.trial_started_at<NEW.dispatched_at OR NEW.trial_started_at>statement_timestamp()
            OR NEW.status NOT IN ('IN_PERSON_TRIAL','CONCLUDED') OR NEW.arrival_challenge_id IS NULL))
        OR (NEW.concluded_at IS NOT NULL AND (NEW.concluded_at<COALESCE(NEW.trial_started_at,NEW.dispatched_at,NEW.created_at)
            OR NEW.concluded_at>statement_timestamp())) THEN
        RAISE EXCEPTION 'Visit evidence must follow its observed lifecycle chronology.' USING ERRCODE='23514'; END IF;
    IF NEW.arrival_challenge_id IS NOT NULL THEN
        SELECT * INTO proof FROM delivery_arrivalotp WHERE id=NEW.arrival_challenge_id FOR SHARE;
        IF proof.assignment_id IS DISTINCT FROM NEW.assignment_id OR proof.consumed_at IS NULL OR proof.invalidated_at IS NOT NULL
            OR NEW.trial_started_at IS NULL OR NEW.trial_started_at<proof.consumed_at THEN
            RAISE EXCEPTION 'Trial requires the same assignment consumed Arrival OTP.' USING ERRCODE='23514'; END IF;
    END IF;
    IF TG_OP='INSERT' THEN
        IF assignment.ended_at IS NOT NULL OR NEW.current_evidence_id IS NOT NULL THEN
            RAISE EXCEPTION 'New visit requires open responsibility; evidence is then attached atomically.' USING ERRCODE='23514'; END IF;
        RETURN NEW;
    END IF;
    SELECT * INTO e FROM delivery_visitevidence WHERE id=NEW.current_evidence_id FOR SHARE;
    IF e.id IS NULL OR e.visit_id IS DISTINCT FROM NEW.id OR e.assignment_id IS DISTINCT FROM NEW.assignment_id
        OR e.status IS DISTINCT FROM NEW.status OR e.previous_id IS DISTINCT FROM OLD.current_evidence_id
        OR NEW.current_evidence_id IS NOT DISTINCT FROM OLD.current_evidence_id
        OR e.observed_at<COALESCE(NEW.concluded_at,NEW.trial_started_at,NEW.dispatched_at,NEW.created_at)
        OR (e.kind<>'OUTCOME_CORRECTION' AND e.outcome IS DISTINCT FROM NEW.outcome) THEN
        RAISE EXCEPTION 'Visit changes require the next compatible attributable evidence.' USING ERRCODE='23514'; END IF;
    IF NEW.assignment_id IS DISTINCT FROM OLD.assignment_id AND e.kind<>'REASSIGN' THEN
        RAISE EXCEPTION 'Assignment replacement requires preserved supersession evidence.' USING ERRCODE='23514'; END IF;
    IF e.kind='OUTCOME_CORRECTION' AND (to_jsonb(NEW)-ARRAY['updated_at','current_evidence_id'])
        IS DISTINCT FROM (to_jsonb(OLD)-ARRAY['updated_at','current_evidence_id']) THEN
        RAISE EXCEPTION 'Outcome correction cannot rewrite visit, money or custody facts.' USING ERRCODE='23514'; END IF;
    IF NEW.status IS DISTINCT FROM OLD.status THEN
        IF e.kind NOT IN ('STATE','PREDISPATCH_CLOSE') OR (e.kind='STATE' AND assignment.ended_at IS NOT NULL) THEN
            RAISE EXCEPTION 'Progression requires the current open assignment or attributed undispatched closure.' USING ERRCODE='23514'; END IF;
        IF NEW.status IN ('READY','DISPATCHED') AND NOT EXISTS(SELECT 1 FROM billing_financialdocument WHERE id=NEW.dispatch_document_id
            AND booking_id=NEW.booking_id AND kind='DISPATCH' AND status='ISSUED') THEN
            RAISE EXCEPTION 'Ready/dispatch requires reviewed issued dispatch evidence.' USING ERRCODE='23514'; END IF;
        IF NEW.status='IN_PERSON_TRIAL' AND NEW.arrival_challenge_id IS NULL THEN
            RAISE EXCEPTION 'Trial cannot start on an agent assertion.' USING ERRCODE='23514'; END IF;
        IF NEW.status='CONCLUDED' AND ((OLD.status='DISPATCHED' AND NEW.outcome NOT IN ('FAILED_VISIT','ABORTED'))
            OR (NEW.outcome IN ('SELECTED','NO_PURCHASE') AND NEW.trial_started_at IS NULL)) THEN
            RAISE EXCEPTION 'Visit outcome must preserve whether a verified trial occurred.' USING ERRCODE='23514'; END IF;
    END IF;
    RETURN NEW;
END; $$;

CREATE FUNCTION delivery_visit_evidence_consistency() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE v delivery_homevisit%ROWTYPE; latest delivery_visitevidence%ROWTYPE; target bigint;
BEGIN
    IF TG_TABLE_NAME='delivery_homevisit' THEN
        target:=NEW.id;
    ELSE
        target:=NEW.visit_id;
    END IF;
    SELECT * INTO v FROM delivery_homevisit WHERE id=target;
    SELECT * INTO latest FROM delivery_visitevidence e WHERE e.visit_id=target
        AND NOT EXISTS(SELECT 1 FROM delivery_visitevidence n WHERE n.previous_id=e.id);
    IF latest.id IS NULL OR v.current_evidence_id IS DISTINCT FROM latest.id
        OR ROW(v.assignment_id,v.status) IS DISTINCT FROM ROW(latest.assignment_id,latest.status) THEN
        RAISE EXCEPTION 'Visit and complete current evidence chain must commit together.' USING ERRCODE='23514'; END IF;
    RETURN NULL;
END; $$;

CREATE OR REPLACE FUNCTION visit_manifest_consistency() RETURNS trigger LANGUAGE plpgsql AS $$
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

CREATE OR REPLACE FUNCTION delivery_custody_binding() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE r inventory_inventoryreservation%ROWTYPE; v delivery_homevisit%ROWTYPE; prior delivery_reservationcustodyevent%ROWTYPE;
DECLARE booking_id bigint; unit inventory_inventoryunit%ROWTYPE;
BEGIN
    SELECT * INTO r FROM inventory_inventoryreservation WHERE id=NEW.reservation_id FOR UPDATE;
    SELECT * INTO v FROM delivery_homevisit WHERE id=NEW.visit_id FOR UPDATE;
    SELECT q.booking_id INTO booking_id FROM payments_bookingcharge q JOIN payments_paymentcontext c ON c.booking_charge_id=q.id WHERE c.id=r.payment_context_id;
    IF r.status<>'COMMITTED' OR booking_id IS DISTINCT FROM v.booking_id OR btrim(NEW.reason)='' OR btrim(NEW.evidence_reference)=''
        OR jsonb_typeof(NEW.policy_snapshot)<>'object' OR NEW.policy_snapshot='{}' OR NEW.observed_at>statement_timestamp()
        OR NEW.observed_at<r.committed_at OR NOT EXISTS(SELECT 1 FROM accounts_staffuser WHERE id=NEW.actor_id AND is_active) THEN
        RAISE EXCEPTION 'Custody requires committed visit stock and attributable reviewed observation.' USING ERRCODE='23514'; END IF;
    IF NEW.kind='NEVER_DISPATCHED' THEN
        SELECT * INTO unit FROM inventory_inventoryunit WHERE id=r.unit_id FOR UPDATE;
        IF NEW.previous_id IS NOT NULL OR v.status<>'CLOSED_BEFORE_DISPATCH' OR NEW.observed_at<v.concluded_at
            OR v.dispatched_at IS NOT NULL OR v.arrival_challenge_id IS NOT NULL
            OR unit.status<>'AVAILABLE' OR unit.custody<>'HUB'
            OR EXISTS(SELECT 1 FROM delivery_reservationcustodyevent WHERE reservation_id=r.id)
            OR NOT EXISTS(SELECT 1 FROM delivery_visitevidence WHERE id=v.current_evidence_id
                AND kind='PREDISPATCH_CLOSE' AND visit_id=v.id) THEN
            RAISE EXCEPTION 'Undispatched disposition requires observed stock still at the hub and closed preparation.' USING ERRCODE='23514'; END IF;
        RETURN NEW;
    END IF;
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
        OR (NEW.kind='EXCEPTION' AND NEW.previous_id IS NOT NULL AND prior.kind NOT IN ('SALE','QC_PASS','QC_FAIL','EXCEPTION','NEVER_DISPATCHED'))
    ) THEN RAISE EXCEPTION 'Invalid observed custody sequence.' USING ERRCODE='23514'; END IF;
    IF NEW.kind='SALE' AND NOT EXISTS(SELECT 1 FROM billing_purchaseitem i JOIN billing_purchase p ON p.id=i.purchase_id
        WHERE i.id=NEW.purchase_item_id AND i.reservation_id=r.id AND p.status='CONFIRMED') THEN
        RAISE EXCEPTION 'Sale requires exact financially confirmed purchase piece.' USING ERRCODE='23514'; END IF;
    RETURN NEW;
END; $$;

CREATE OR REPLACE FUNCTION delivery_custody_state(kind text) RETURNS text LANGUAGE sql IMMUTABLE AS $$
    SELECT CASE kind WHEN 'DISPATCH' THEN 'OUT_FOR_DELIVERY' WHEN 'RETURNING' THEN 'RETURNING_TO_HUB'
        WHEN 'HUB_RECEIPT' THEN 'QC_PENDING' WHEN 'QC_PASS' THEN 'AVAILABLE' WHEN 'QC_FAIL' THEN 'QUARANTINED'
        WHEN 'SALE' THEN 'SOLD' WHEN 'NEVER_DISPATCHED' THEN 'AVAILABLE' ELSE 'QUARANTINED' END
$$;

CREATE OR REPLACE FUNCTION delivery_custody_consistency() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE unit inventory_inventoryunit%ROWTYPE; observation delivery_reservationcustodyevent%ROWTYPE;
DECLARE reservation inventory_inventoryreservation%ROWTYPE;
BEGIN
    SELECT * INTO observation FROM delivery_reservationcustodyevent WHERE id=NEW.id;
    IF EXISTS(SELECT 1 FROM delivery_reservationcustodyevent WHERE previous_id=NEW.id) THEN RETURN NULL; END IF;
    SELECT * INTO reservation FROM inventory_inventoryreservation WHERE id=observation.reservation_id;
    SELECT * INTO unit FROM inventory_inventoryunit WHERE id=reservation.unit_id;
    IF unit.status IS DISTINCT FROM delivery_custody_state(observation.kind)
        OR (observation.kind IN ('DISPATCH','RETURNING') AND unit.custody<>'AGENT')
        OR (observation.kind IN ('HUB_RECEIPT','QC_PASS','QC_FAIL','NEVER_DISPATCHED') AND unit.custody<>'HUB')
        OR (observation.kind='SALE' AND unit.custody<>'CUSTOMER')
        OR (observation.kind='NEVER_DISPATCHED' AND (reservation.status<>'RELEASED' OR reservation.disposition_id<>observation.id)) THEN
        RAISE EXCEPTION 'Observation, commitment closure and authoritative physical stock must commit together.' USING ERRCODE='23514'; END IF;
    RETURN NULL;
END; $$;

CREATE OR REPLACE FUNCTION checkout_reservation_binding() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE unit inventory_inventoryunit%ROWTYPE; context payments_paymentcontext%ROWTYPE; market bigint; city bigint;
DECLARE disposition delivery_reservationcustodyevent%ROWTYPE;
BEGIN
    SELECT * INTO context FROM payments_paymentcontext WHERE id=NEW.payment_context_id FOR UPDATE;
    SELECT b.market_id INTO market FROM trials_trialbooking b JOIN payments_bookingcharge q ON q.booking_id=b.id WHERE q.id=context.booking_charge_id;
    SELECT * INTO unit FROM inventory_inventoryunit WHERE id=NEW.unit_id FOR UPDATE;
    SELECT market_id INTO city FROM markets_hub WHERE id=unit.hub_id;
    IF city IS DISTINCT FROM market THEN RAISE EXCEPTION 'Reservation city mismatch.' USING ERRCODE='23514'; END IF;
    IF (TG_OP='INSERT' OR (TG_OP='UPDATE' AND OLD.status<>NEW.status AND NEW.status='COMMITTED')) AND NOT checkout_unit_active(NEW.unit_id) THEN
        RAISE EXCEPTION 'Commitments require active catalogue and operating references.' USING ERRCODE='23514'; END IF;
    IF TG_OP='INSERT' AND (unit.status<>'AVAILABLE' OR unit.custody<>'HUB' OR context.status<>'OPEN'
        OR NEW.held_at>=context.expires_at OR context.expires_at<=statement_timestamp()) THEN
        RAISE EXCEPTION 'Reservation requires available stock and a live context.' USING ERRCODE='23514'; END IF;
    IF TG_OP='UPDATE' AND OLD.status<>NEW.status THEN
        IF NEW.status='CAPTURE_PENDING' AND (context.status<>'CAPTURING' OR NEW.capture_started_at IS DISTINCT FROM context.capture_started_at) THEN
            RAISE EXCEPTION 'Reservation capture must match its context.' USING ERRCODE='23514'; END IF;
        IF NEW.status='COMMITTED' AND (NOT EXISTS(SELECT 1 FROM trials_trialbooking b JOIN payments_bookingcharge q ON q.booking_id=b.id
            WHERE q.id=context.booking_charge_id AND b.status='PAYMENT_PENDING' AND q.status='DUE') OR NOT EXISTS(
            SELECT 1 FROM payments_paymentattempt WHERE context_id=context.id AND status='SUCCEEDED')) THEN
            RAISE EXCEPTION 'Commitment requires verified successful payment.' USING ERRCODE='23514'; END IF;
        IF OLD.status='COMMITTED' AND NEW.status='RELEASED' THEN
            SELECT * INTO disposition FROM delivery_reservationcustodyevent WHERE id=NEW.disposition_id;
            IF disposition.kind IS DISTINCT FROM 'SALE' AND EXISTS(SELECT 1 FROM billing_purchaseitem i
                JOIN billing_purchaseobligation o ON o.purchase_id=i.purchase_id WHERE i.reservation_id=NEW.id
                AND (o.status='DUE' OR (o.status='COLLECTED' AND NOT checkout_obligation_rejected('PURCHASE',o.id)))) THEN
                RAISE EXCEPTION 'Resolve selected final-payment outcomes before releasing stock.' USING ERRCODE='23514'; END IF;
            IF disposition.id IS NULL OR disposition.reservation_id IS DISTINCT FROM NEW.id
                OR disposition.kind NOT IN ('SALE','QC_PASS','QC_FAIL','NEVER_DISPATCHED')
                OR EXISTS(SELECT 1 FROM delivery_reservationcustodyevent later WHERE later.previous_id=disposition.id)
                OR NEW.released_at<OLD.committed_at OR NEW.released_at<disposition.observed_at
                OR (disposition.kind='NEVER_DISPATCHED' AND (unit.status<>'AVAILABLE' OR unit.custody<>'HUB'
                    OR context.status<>'PAID' OR EXISTS(SELECT 1 FROM payments_paymentcontext c WHERE c.booking_charge_id=context.booking_charge_id
                        AND c.status NOT IN ('PAID','EXPIRED','CANCELLED')))) THEN
                RAISE EXCEPTION 'Committed stock closes only on its current observed disposition.' USING ERRCODE='23514'; END IF;
        ELSIF NEW.status IN ('EXPIRED','RELEASED') AND context.status NOT IN ('EXPIRED','CANCELLED') THEN
            RAISE EXCEPTION 'Close/reconcile context before releasing inventory.' USING ERRCODE='23514'; END IF;
        IF OLD.status<>'COMMITTED' AND NEW.status IN ('EXPIRED','RELEASED') AND (
            OLD.status='CAPTURE_PENDING' OR context.resolution_id IS NOT NULL OR EXISTS(
                SELECT 1 FROM payments_paymentattempt WHERE context_id=context.id AND provider_order_id IS NOT NULL))
            AND (NEW.resolution_id IS NULL OR NOT checkout_resolution_current(NEW.resolution_id,context.id)) THEN
            RAISE EXCEPTION 'Capture uncertainty requires reconciliation evidence.' USING ERRCODE='23514'; END IF;
    END IF;
    RETURN NEW;
END; $$;

DROP TRIGGER checkout_identity ON delivery_homevisit;
CREATE TRIGGER checkout_identity BEFORE INSERT OR UPDATE OR DELETE ON delivery_homevisit FOR EACH ROW
EXECUTE FUNCTION checkout_guard_record('status dispatch_document_id arrival_challenge_id dispatched_at trial_started_at concluded_at outcome assignment_id current_evidence_id',
    'dispatch_document_id arrival_challenge_id dispatched_at trial_started_at concluded_at outcome','PREPARING',
    '{"PREPARING":["READY","CLOSED_BEFORE_DISPATCH"],"READY":["DISPATCHED","CLOSED_BEFORE_DISPATCH"],"DISPATCHED":["IN_PERSON_TRIAL","CONCLUDED"],"IN_PERSON_TRIAL":["CONCLUDED"]}');
CREATE TRIGGER checkout_serialization BEFORE INSERT OR UPDATE OR DELETE ON delivery_visitevidence FOR EACH STATEMENT EXECUTE FUNCTION checkout_statement_lock();
CREATE TRIGGER checkout_history BEFORE UPDATE OR DELETE ON delivery_visitevidence FOR EACH ROW EXECUTE FUNCTION trials_reject_evidence_mutation();
CREATE TRIGGER checkout_binding BEFORE INSERT ON delivery_visitevidence FOR EACH ROW EXECUTE FUNCTION delivery_visit_evidence_binding();
CREATE TRIGGER visit_assignment_binding BEFORE UPDATE ON delivery_deliveryassignment
    FOR EACH ROW EXECUTE FUNCTION delivery_assignment_visit_binding();
CREATE CONSTRAINT TRIGGER visit_evidence_consistency AFTER INSERT ON delivery_visitevidence DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION delivery_visit_evidence_consistency();
CREATE CONSTRAINT TRIGGER visit_evidence_consistency AFTER INSERT OR UPDATE ON delivery_homevisit DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION delivery_visit_evidence_consistency();
"""


def reverse_guards(apps, schema_editor):
    for name in ("VisitEvidence", "HomeVisit", "ReservationCustodyEvent"):
        if apps.get_model("delivery", name).objects.using(schema_editor.connection.alias).exists():
            raise IrreversibleError("Visit evidence exists; use reviewed forward recovery.")
    # Reinstall the already-frozen prior definitions; no local generator is needed.
    old = import_module("trials.migrations.0006_lifecycle_financial_integrity").SQL
    previous = import_module("trials.migrations.0007_postcapture_integrity").SQL
    for name, source in (
        ("delivery_visit_binding", old), ("visit_manifest_consistency", old),
        ("delivery_custody_binding", old), ("delivery_custody_consistency", old),
        ("checkout_reservation_binding", previous),
    ):
        marker = f"FUNCTION {name}()"
        start = source.index(marker)
        end = source.index("$$;", start) + 3
        schema_editor.execute("CREATE OR REPLACE " + source[start:end], params=None)
    start = old.index("CREATE FUNCTION delivery_custody_state(")
    end = old.index("$$;", start) + 3
    schema_editor.execute(
        old[start:end].replace("CREATE FUNCTION", "CREATE OR REPLACE FUNCTION", 1),
        params=None,
    )
    schema_editor.execute("""
        DROP TRIGGER visit_evidence_consistency ON delivery_homevisit;
        DROP TRIGGER visit_evidence_consistency ON delivery_visitevidence;
        DROP TRIGGER checkout_binding ON delivery_visitevidence;
        DROP TRIGGER checkout_history ON delivery_visitevidence;
        DROP TRIGGER checkout_serialization ON delivery_visitevidence;
        DROP TRIGGER visit_assignment_binding ON delivery_deliveryassignment;
        DROP FUNCTION delivery_assignment_visit_binding();
        DROP FUNCTION delivery_visit_evidence_consistency();
        DROP FUNCTION delivery_visit_evidence_binding();
        DROP TRIGGER checkout_identity ON delivery_homevisit;
        CREATE TRIGGER checkout_identity BEFORE INSERT OR UPDATE OR DELETE ON delivery_homevisit FOR EACH ROW
        EXECUTE FUNCTION checkout_guard_record('status dispatch_document_id arrival_challenge_id dispatched_at trial_started_at concluded_at outcome',
            'dispatch_document_id arrival_challenge_id dispatched_at trial_started_at concluded_at outcome','PREPARING',
            '{"PREPARING":["READY"],"READY":["DISPATCHED"],"DISPATCHED":["IN_PERSON_TRIAL","CONCLUDED"],"IN_PERSON_TRIAL":["CONCLUDED"]}');
    """, params=None)


class Migration(migrations.Migration):
    dependencies = [
        ("trials", "0007_postcapture_integrity"),
        ("delivery", "0005_visit_evidence_and_predispatch_closure"),
    ]
    operations = [
        migrations.RunSQL(SQL, migrations.RunSQL.noop),
        migrations.RunPython(migrations.RunPython.noop, reverse_guards),
    ]
