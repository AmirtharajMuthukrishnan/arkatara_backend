"""Bound explicit upfront applications and preserve versioned scope identity.

Frozen hand-maintained SQL. No default credit, adjustment, refund or tax treatment.
Refs: BUSINESS_RULES.md R-07, R-25, R-26, R-28, R-30; DECISIONS.md BD-01, CA-01, CA-02.
"""

from django.db import migrations
from django.db.migrations.exceptions import IrreversibleError


SQL = r"""
CREATE FUNCTION purchase_balance_matches(target_purchase bigint) RETURNS boolean
LANGUAGE sql VOLATILE AS $$
    SELECT EXISTS (
        SELECT 1 FROM billing_purchase p JOIN billing_purchasebalanceevidence b ON b.purchase_id=p.id
        WHERE p.id=target_purchase AND b.currency=p.currency
        AND b.amount_due_before_application=p.jewellery_total_minor::numeric+b.adjustment_minor
        AND p.payable_minor=b.amount_due_before_application-(
            SELECT COALESCE(SUM(a.amount_minor),0) FROM billing_upfrontapplication a WHERE a.balance_evidence_id=b.id)
    )
$$;

CREATE FUNCTION purchase_balance_binding() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE p billing_purchase%ROWTYPE; b billing_purchasebalanceevidence%ROWTYPE;
DECLARE source payments_paymentallocation%ROWTYPE; charge payments_bookingcharge%ROWTYPE;
DECLARE captured payments_gatewaytransaction%ROWTYPE; used_source numeric; used_purchase numeric;
BEGIN
    IF TG_TABLE_NAME='billing_purchase' THEN
        IF TG_OP='UPDATE' AND OLD.status<>NEW.status AND NEW.status IN ('SEALED','CONFIRMED') THEN
            IF NOT purchase_balance_matches(NEW.id) THEN
                RAISE EXCEPTION 'Seal requires an authoritative source-linked balance calculation.' USING ERRCODE='23514'; END IF;
            IF NEW.status='CONFIRMED' AND NEW.payable_minor=0 AND (
                NOT checkout_purchase_protected(NEW.id)
                OR NOT EXISTS(SELECT 1 FROM billing_purchasebalanceevidence WHERE purchase_id=NEW.id AND amount_due_before_application>0)
                OR NOT EXISTS(SELECT 1 FROM billing_purchaseobligation WHERE purchase_id=NEW.id AND amount_minor=0 AND status='COLLECTED')) THEN
                RAISE EXCEPTION 'Zero balance confirmation requires fully applied collected funds and protected selection.' USING ERRCODE='23514'; END IF;
        END IF;
        RETURN NEW;
    END IF;
    IF TG_TABLE_NAME='payments_paymentcontext' THEN
        IF NEW.purchase_obligation_id IS NOT NULL AND EXISTS(
            SELECT 1 FROM billing_purchaseobligation WHERE id=NEW.purchase_obligation_id AND amount_minor=0) THEN
            RAISE EXCEPTION 'A fully source-funded balance has no new provider collection context.' USING ERRCODE='23514'; END IF;
        RETURN NEW;
    END IF;
    IF TG_TABLE_NAME='billing_purchaseobligation' THEN
        IF NEW.amount_minor=0 AND (TG_OP='INSERT' OR NEW.status IN ('DUE','COLLECTED')) THEN
            SELECT * INTO p FROM billing_purchase WHERE id=NEW.purchase_id FOR UPDATE;
            IF p.status<>'SEALED' OR NOT checkout_purchase_protected(p.id) OR NOT purchase_balance_matches(p.id)
                OR NOT EXISTS(SELECT 1 FROM billing_purchasebalanceevidence WHERE purchase_id=p.id AND amount_due_before_application>0)
                OR EXISTS(SELECT 1 FROM payments_paymentcontext WHERE purchase_obligation_id=NEW.id) THEN
                RAISE EXCEPTION 'Zero balance obligation requires fully applied collected source funds.' USING ERRCODE='23514'; END IF;
        END IF;
        RETURN NEW;
    END IF;
    IF NEW.observed_at>statement_timestamp() OR btrim(NEW.reason)='' OR btrim(NEW.evidence_reference)=''
        OR jsonb_typeof(NEW.policy_snapshot)<>'object' OR NEW.policy_snapshot='{}'
        OR NOT EXISTS(SELECT 1 FROM accounts_staffuser WHERE id=NEW.actor_id AND is_active) THEN
        RAISE EXCEPTION 'Balance evidence requires attributable observation and explicit policy facts.' USING ERRCODE='23514'; END IF;
    IF TG_TABLE_NAME='billing_purchasebalanceevidence' THEN
        SELECT * INTO p FROM billing_purchase WHERE id=NEW.purchase_id FOR UPDATE;
        IF p.status<>'DRAFT' OR NEW.currency IS DISTINCT FROM p.currency
            OR NEW.amount_due_before_application::numeric<>p.jewellery_total_minor::numeric+NEW.adjustment_minor
            OR jsonb_typeof(NEW.calculation_snapshot)<>'object' OR NEW.calculation_snapshot='{}' THEN
            RAISE EXCEPTION 'Balance basis must explain this draft jewellery value and explicit adjustment.' USING ERRCODE='23514'; END IF;
    ELSE
        SELECT * INTO b FROM billing_purchasebalanceevidence WHERE id=NEW.balance_evidence_id FOR UPDATE;
        SELECT * INTO p FROM billing_purchase WHERE id=b.purchase_id FOR UPDATE;
        SELECT * INTO source FROM payments_paymentallocation WHERE id=NEW.source_allocation_id FOR UPDATE;
        SELECT * INTO charge FROM payments_bookingcharge WHERE id=source.booking_charge_id FOR UPDATE;
        SELECT * INTO captured FROM payments_gatewaytransaction WHERE id=source.gateway_transaction_id FOR SHARE;
        IF p.status<>'DRAFT' OR source.booking_charge_id IS NULL OR source.purchase_obligation_id IS NOT NULL
            OR NEW.observed_at<b.observed_at OR NEW.observed_at<source.allocated_at
            OR charge.status<>'COLLECTED' OR charge.booking_id IS DISTINCT FROM p.booking_id
            OR charge.payee_legal_entity_id IS DISTINCT FROM p.seller_id
            OR NEW.currency IS DISTINCT FROM source.currency OR NEW.currency IS DISTINCT FROM b.currency
            OR NEW.currency IS DISTINCT FROM captured.currency OR captured.transaction_kind<>'CAPTURE'
            OR NOT EXISTS(SELECT 1 FROM payments_paymentcontext c JOIN payments_paymentattempt a ON a.context_id=c.id
                WHERE c.booking_charge_id=charge.id AND c.status='PAID' AND a.id=checkout_effective_attempt(captured.id)) THEN
            RAISE EXCEPTION 'Application requires captured accepted upfront funds for this draft sale.' USING ERRCODE='23514'; END IF;
        SELECT COALESCE(SUM(amount_minor),0) INTO used_source FROM billing_upfrontapplication WHERE source_allocation_id=source.id;
        SELECT COALESCE(SUM(amount_minor),0) INTO used_purchase FROM billing_upfrontapplication WHERE balance_evidence_id=b.id;
        IF used_source+NEW.amount_minor>source.amount_minor OR used_purchase+NEW.amount_minor>b.amount_due_before_application THEN
            RAISE EXCEPTION 'Application exceeds unconsumed collected source or purchase balance.' USING ERRCODE='23514'; END IF;
    END IF;
    RETURN NEW;
END; $$;

CREATE TRIGGER checkout_serialization BEFORE INSERT OR UPDATE OR DELETE ON billing_purchasebalanceevidence
FOR EACH STATEMENT EXECUTE FUNCTION checkout_statement_lock();
CREATE TRIGGER checkout_serialization BEFORE INSERT OR UPDATE OR DELETE ON billing_upfrontapplication
FOR EACH STATEMENT EXECUTE FUNCTION checkout_statement_lock();
CREATE TRIGGER checkout_history BEFORE UPDATE OR DELETE ON billing_purchasebalanceevidence
FOR EACH ROW EXECUTE FUNCTION trials_reject_evidence_mutation();
CREATE TRIGGER checkout_history BEFORE UPDATE OR DELETE ON billing_upfrontapplication
FOR EACH ROW EXECUTE FUNCTION trials_reject_evidence_mutation();
CREATE TRIGGER balance_binding BEFORE INSERT ON billing_purchasebalanceevidence
FOR EACH ROW EXECUTE FUNCTION purchase_balance_binding();
CREATE TRIGGER balance_binding BEFORE INSERT ON billing_upfrontapplication
FOR EACH ROW EXECUTE FUNCTION purchase_balance_binding();
CREATE TRIGGER balance_binding AFTER UPDATE ON billing_purchase
FOR EACH ROW EXECUTE FUNCTION purchase_balance_binding();
CREATE TRIGGER balance_binding BEFORE INSERT OR UPDATE ON billing_purchaseobligation
FOR EACH ROW EXECUTE FUNCTION purchase_balance_binding();
CREATE TRIGGER balance_binding BEFORE INSERT OR UPDATE ON payments_paymentcontext
FOR EACH ROW EXECUTE FUNCTION purchase_balance_binding();
-- Existing positive-balance/document/item guards remain in place. A zero balance
-- has no provider collection; its separate AFTER guard proves applied source funds.
DROP TRIGGER checkout_binding ON billing_purchase;
CREATE TRIGGER checkout_binding BEFORE INSERT OR UPDATE ON billing_purchase
FOR EACH ROW WHEN (NEW.payable_minor>0 OR NEW.status<>'CONFIRMED') EXECUTE FUNCTION billing_binding();

CREATE OR REPLACE FUNCTION checkout_policy_binding() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE policy compliance_businessconfiguration%ROWTYPE; market_identity text; category_identity text; ns text; policy_key text;
BEGIN
    IF TG_OP='INSERT' AND NEW.policy_scope_version<>2 THEN
        RAISE EXCEPTION 'New bindings require public UUID policy scope identifiers.' USING ERRCODE='23514'; END IF;
    SELECT * INTO policy FROM compliance_businessconfiguration WHERE id=NEW.policy_revision_id FOR SHARE;
    IF TG_TABLE_NAME='trials_trialplanrevision' THEN
        ns:='trials'; policy_key:='plan-policy-reference';
        SELECT CASE NEW.policy_scope_version WHEN 1 THEN code ELSE public_id::text END INTO market_identity FROM markets_market WHERE id=NEW.market_id;
        SELECT CASE NEW.policy_scope_version WHEN 1 THEN code ELSE public_id::text END INTO category_identity FROM catalog_category WHERE id=NEW.category_id;
    ELSE
        ns:='payments'; policy_key:='booking-charge-policy-reference';
        SELECT CASE NEW.policy_scope_version WHEN 1 THEN m.code ELSE m.public_id::text END INTO market_identity
            FROM markets_market m JOIN trials_trialbooking b ON b.market_id=m.id WHERE b.id=NEW.booking_id;
    END IF;
    IF policy.namespace IS DISTINCT FROM ns OR policy.key IS DISTINCT FROM policy_key OR jsonb_typeof(policy.scope)<>'object'
        OR EXISTS(SELECT 1 FROM jsonb_each(policy.scope) d WHERE
            d.key NOT IN ('market','category') OR (ns='payments' AND d.key<>'market')
            OR jsonb_typeof(d.value)<>'string' OR d.value#>>'{}' IS DISTINCT FROM
            CASE d.key WHEN 'market' THEN market_identity WHEN 'category' THEN category_identity END) THEN
        RAISE EXCEPTION 'Policy identity or explicit versioned scope mismatch.' USING ERRCODE='23514'; END IF;
    RETURN NEW;
END; $$;
"""

REVERSE_SQL = r"""
DROP TRIGGER balance_binding ON billing_purchase;
DROP TRIGGER balance_binding ON billing_purchaseobligation;
DROP TRIGGER balance_binding ON payments_paymentcontext;
DROP TRIGGER checkout_binding ON billing_purchase;
CREATE TRIGGER checkout_binding BEFORE INSERT OR UPDATE ON billing_purchase FOR EACH ROW EXECUTE FUNCTION billing_binding();
DROP FUNCTION purchase_balance_binding();
DROP FUNCTION purchase_balance_matches(bigint);
CREATE OR REPLACE FUNCTION checkout_policy_binding() RETURNS trigger LANGUAGE plpgsql AS $$
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
        OR EXISTS(SELECT 1 FROM jsonb_each_text(policy.scope) d WHERE d.value IS DISTINCT FROM
            CASE d.key WHEN 'market' THEN market_code WHEN 'category' THEN category_code END) THEN
        RAISE EXCEPTION 'Policy identity or explicit scope mismatch.' USING ERRCODE='23514'; END IF;
    RETURN NEW;
END; $$;
"""


def require_pending_balance_evidence(apps, schema_editor):
    Purchase = apps.get_model("billing", "Purchase")
    if (
        Purchase.objects.using(schema_editor.connection.alias)
        .filter(status="SEALED", balance_evidence__isnull=True)
        .exists()
    ):
        raise RuntimeError(
            "Sealed legacy purchases require reviewed balance evidence or forward recovery "
            "before enabling new confirmation guards."
        )


def reverse_guards(apps, schema_editor):
    if apps.get_model("billing", "Purchase").objects.using(
        schema_editor.connection.alias
    ).filter(payable_minor=0).exists():
        raise IrreversibleError("Zero-balance purchase evidence exists; use reviewed forward recovery.")
    for table in ("billing_upfrontapplication", "billing_purchasebalanceevidence"):
        with schema_editor.connection.cursor() as cursor:
            cursor.execute(f'SELECT EXISTS(SELECT 1 FROM "{table}")')
            if cursor.fetchone()[0]:
                raise IrreversibleError("Purchase application evidence exists; use reviewed forward recovery.")
    for table in ("billing_upfrontapplication", "billing_purchasebalanceevidence"):
        schema_editor.execute(f'DROP TRIGGER checkout_serialization ON "{table}"')
        schema_editor.execute(f'DROP TRIGGER checkout_history ON "{table}"')
        schema_editor.execute(f'DROP TRIGGER balance_binding ON "{table}"')


class Migration(migrations.Migration):
    dependencies = [
        ("trials", "0009_plan_scope_identity_version"),
        ("billing", "0003_upfront_application_balance_evidence"),
    ]
    operations = [
        migrations.RunPython(require_pending_balance_evidence, migrations.RunPython.noop),
        migrations.RunSQL(SQL, REVERSE_SQL),
        migrations.RunPython(migrations.RunPython.noop, reverse_guards),
    ]
