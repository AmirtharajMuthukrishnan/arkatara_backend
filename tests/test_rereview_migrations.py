"""Legacy scope preservation, safe correction upgrades and effective frozen SQL."""

import uuid
from datetime import timedelta

import pytest
from django.db import connection
from django.db.migrations.exceptions import IrreversibleError
from django.db.migrations.executor import MigrationExecutor
from django.utils import timezone

from compliance.configuration import configuration_scope_key
from scripts.inspect_integrity_guards import (
    check_database,
    check_pending_balances,
    effective_functions,
)
from tests.test_checkout_models import setup as setup
from tests.test_lifecycle_financial_models import trial_graph
from tests.test_upfront_application import draft_purchase, seal


@pytest.mark.django_db
def test_effective_forward_function_bodies_match_migrated_database():
    check_database(effective_functions())


@pytest.mark.django_db(transaction=True)
def test_legacy_sealed_selection_blocks_guard_rollout_without_rewriting_history(setup):
    executor = MigrationExecutor(connection)
    latest = executor.loader.graph.leaf_nodes()
    targets = [
        (app, "0009_plan_scope_identity_version" if app == "trials" else name)
        for app, name in latest
    ]
    purchase = None
    try:
        executor.migrate(targets)
        graph = trial_graph(setup)
        purchase, _ = draft_purchase(graph, setup, payable=500)
        seal(purchase)
        original = (
            purchase.public_id,
            purchase.booking_id,
            purchase.jewellery_total_minor,
            purchase.payable_minor,
            purchase.balance_snapshot,
        )
        with pytest.raises(SystemExit, match="sealed legacy selection"):
            check_pending_balances()
        with pytest.raises(RuntimeError, match="reviewed balance evidence or forward recovery"):
            MigrationExecutor(connection).migrate(latest)
        purchase.refresh_from_db()
        assert purchase.status == "SEALED"
        assert (
            purchase.public_id,
            purchase.booking_id,
            purchase.jewellery_total_minor,
            purchase.payable_minor,
            purchase.balance_snapshot,
        ) == original
        executor = MigrationExecutor(connection)
        assert (
            "trials",
            "0010_purchase_balance_integrity",
        ) not in executor.loader.applied_migrations
        with connection.cursor() as cursor:
            cursor.execute("SELECT COUNT(*) FROM pg_proc WHERE proname='purchase_balance_binding'")
            assert cursor.fetchone()[0] == 0
        # Fixture-only resolution retains selection facts; no refund/credit is inferred.
        purchase.status = "VOID"
        purchase.save()
        check_pending_balances()
        MigrationExecutor(connection).migrate(latest)
        purchase.refresh_from_db()
        assert purchase.status == "VOID"
        assert (
            purchase.public_id,
            purchase.booking_id,
            purchase.jewellery_total_minor,
            purchase.payable_minor,
            purchase.balance_snapshot,
        ) == original
        check_database(effective_functions())
    finally:
        if purchase is not None:
            purchase.refresh_from_db()
            if purchase.status == "SEALED":
                purchase.status = "VOID"
                purchase.save()
        MigrationExecutor(connection).migrate(latest)


@pytest.mark.django_db(transaction=True)
def test_upgrade_preserves_code_scoped_plan_and_charge_identity():
    executor = MigrationExecutor(connection)
    latest = executor.loader.graph.leaf_nodes()
    earlier = {
        "trials": "0007_postcapture_integrity",
        "billing": "0002_financialdocumentline_purchase_item_and_more",
        "delivery": "0004_homevisit_reservationcustodyevent_and_more",
        "payments": "0004_paymentresolutionreceipt_and_more",
    }
    targets = [(app, earlier.get(app, name)) for app, name in latest]
    try:
        executor.migrate(targets)
        old = MigrationExecutor(connection).loader.project_state(targets).apps
        market = old.get_model("markets", "Market").objects.create(
            code="REVIEW-UPGRADE", name="Fictional city", country_code="IN"
        )
        category = old.get_model("catalog", "Category").objects.create(
            code="REVIEW-ITEM", name="Fictional category"
        )
        scopes = ({"market": market.code, "category": category.code}, {"market": market.code})
        policies = []
        for namespace, key, scope in zip(
            ("trials", "payments"),
            ("plan-policy-reference", "booking-charge-policy-reference"),
            scopes,
            strict=True,
        ):
            policies.append(
                old.get_model("compliance", "BusinessConfiguration").objects.create(
                    namespace=namespace,
                    key=key,
                    scope=scope,
                    scope_key=configuration_scope_key(scope),
                    value="fictional-policy",
                )
            )
        plan = old.get_model("trials", "TrialPlanRevision").objects.create(
            code="legacy-plan",
            version=1,
            market=market,
            category=category,
            policy_revision=policies[0],
            policy_snapshot={"fictional": True},
            effective_from=timezone.now(),
        )
        booking = old.get_model("trials", "TrialBooking").objects.create(
            market=market,
            checkout_mode="GUEST",
            contact_name="Fictional recipient",
            contact_phone="+919000000040",
            address="Fictional address",
            country_code="IN",
            postal_code="560001",
            request_id=uuid.uuid4(),
            request_fingerprint="a" * 64,
        )
        entity = old.get_model("compliance", "LegalEntity").objects.create(
            code="review-payee", registered_name="Fictional party", country_code="IN"
        )
        charge = old.get_model("payments", "BookingCharge").objects.create(
            booking=booking,
            revision=1,
            payee_legal_entity=entity,
            payee_snapshot={"fictional": True},
            policy_revision=policies[1],
            policy_snapshot={"fictional": True},
            amount_minor=100,
            currency="INR",
            quote_expires_at=timezone.now() + timedelta(minutes=5),
        )
        MigrationExecutor(connection).migrate(latest)
        from payments.models import BookingCharge
        from trials.models import TrialPlanRevision

        retained_plan = TrialPlanRevision.objects.get(pk=plan.pk)
        retained_charge = BookingCharge.objects.get(pk=charge.pk)
        assert retained_plan.policy_scope_version == retained_charge.policy_scope_version == 1
        assert retained_plan.public_id == plan.public_id
        assert retained_plan.policy_revision.scope == scopes[0]
        assert retained_charge.policy_revision.scope == scopes[1]
        retained_plan.full_clean()
        retained_charge.status = "VOID"
        retained_charge.save()
        assert retained_charge.policy_scope_version == 1
        check_database(effective_functions())
        # Old values remain compatible, but no evidence-bearing delivery downgrade.
        with connection.schema_editor(atomic=False) as editor:
            from importlib import import_module

            guard = import_module("trials.migrations.0009_plan_scope_identity_version")
            # A version-2 record blocks a meaning-losing downgrade even with no visit.
            from compliance.models import BusinessConfiguration

            new_policy = BusinessConfiguration.objects.create(
                namespace="trials",
                key="plan-policy-reference",
                scope={"market": str(market.public_id)},
                value="fictional-policy",
            )
            TrialPlanRevision.objects.create(
                code="new-plan",
                version=1,
                market_id=market.pk,
                category_id=category.pk,
                policy_revision=new_policy,
                policy_snapshot={"fictional": True},
                effective_from=timezone.now(),
            )
            new_apps = MigrationExecutor(connection).loader.project_state(latest).apps
            with pytest.raises(IrreversibleError, match="forward recovery"):
                guard.preserve_versioned_history(new_apps, editor)
    finally:
        MigrationExecutor(connection).migrate(latest)
