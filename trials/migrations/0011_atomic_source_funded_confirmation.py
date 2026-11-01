"""Commit internal zero-balance collection and purchase confirmation together.

There is no provider callback to reconcile for a zero balance. An interrupted
local transaction must not strand physical stock or rewrite the source funds.
Refs: BUSINESS_RULES.md R-14, R-24, R-25, R-28; DECISIONS.md BD-01, CA-01.
"""

from django.db import migrations
from django.db.migrations.exceptions import IrreversibleError


SQL = r"""
CREATE FUNCTION source_funded_confirmation_consistency() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE obligation billing_purchaseobligation%ROWTYPE;
BEGIN
    SELECT * INTO obligation FROM billing_purchaseobligation WHERE id=NEW.id;
    IF obligation.amount_minor=0 AND obligation.status='COLLECTED' AND NOT EXISTS(
        SELECT 1 FROM billing_purchase WHERE id=obligation.purchase_id AND status='CONFIRMED') THEN
        RAISE EXCEPTION 'Source-funded collection and purchase confirmation must commit together.' USING ERRCODE='23514';
    END IF;
    RETURN NULL;
END; $$;
CREATE CONSTRAINT TRIGGER source_funded_confirmation_consistency
AFTER INSERT OR UPDATE ON billing_purchaseobligation DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION source_funded_confirmation_consistency();
"""

REVERSE_SQL = r"""
DROP TRIGGER source_funded_confirmation_consistency ON billing_purchaseobligation;
DROP FUNCTION source_funded_confirmation_consistency();
"""


def require_coherent_history(apps, schema_editor):
    Obligation = apps.get_model("billing", "PurchaseObligation")
    if (
        Obligation.objects.using(schema_editor.connection.alias)
        .filter(amount_minor=0, status="COLLECTED")
        .exclude(purchase__status="CONFIRMED")
        .exists()
    ):
        raise RuntimeError(
            "Existing unconfirmed source-funded collections require reviewed forward recovery "
            "before enabling the atomic-confirmation guard."
        )


def preserve_atomic_evidence(apps, schema_editor):
    Obligation = apps.get_model("billing", "PurchaseObligation")
    if Obligation.objects.using(schema_editor.connection.alias).filter(amount_minor=0).exists():
        raise IrreversibleError(
            "Source-funded obligation evidence exists; use reviewed forward recovery."
        )


class Migration(migrations.Migration):
    dependencies = [("trials", "0010_purchase_balance_integrity")]
    operations = [
        migrations.RunPython(require_coherent_history, migrations.RunPython.noop),
        migrations.RunSQL(SQL, REVERSE_SQL),
        migrations.RunPython(migrations.RunPython.noop, preserve_atomic_evidence),
    ]
