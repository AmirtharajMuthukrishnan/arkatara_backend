"""Public UUID policy bindings and preservation of legacy code-scoped evidence."""

import pytest
from django.core.exceptions import ValidationError
from django.db import DatabaseError, transaction
from django.utils import timezone

from compliance.configuration import validate_policy_binding
from compliance.models import BusinessConfiguration
from tests.test_checkout_models import booking_graph, raw_insert
from tests.test_checkout_models import setup as setup
from trials.models import TrialPlanRevision

pytestmark = pytest.mark.django_db


def plan_values(setup, policy, **extra):
    return dict(
        code="uuid-scope",
        version=1,
        market=setup.market,
        category=setup.category,
        policy_revision=policy,
        policy_snapshot={"test_only": True},
        effective_from=timezone.now(),
        **extra,
    )


@pytest.mark.parametrize("legacy", [False, True])
def test_new_plan_bindings_require_exact_public_uuid_scope(setup, legacy):
    policy = BusinessConfiguration.objects.create(
        namespace="trials",
        key="plan-policy-reference",
        scope={
            "market": setup.market.code if legacy else str(setup.market.public_id),
            "category": setup.category.code if legacy else str(setup.category.public_id),
        },
        value="fictional-policy",
    )
    values = plan_values(setup, policy)
    if legacy:
        with pytest.raises(ValidationError):
            TrialPlanRevision.objects.create(**values)
        with pytest.raises(DatabaseError), transaction.atomic():
            raw_insert(TrialPlanRevision, **values)
    else:
        plan = TrialPlanRevision.objects.create(**values)
        assert plan.policy_scope_version == 2


def test_legacy_scope_version_cannot_be_selected_for_new_record(setup):
    values = plan_values(setup, setup.plan_policy, policy_scope_version=1)
    with pytest.raises(ValidationError):
        TrialPlanRevision.objects.create(**values)
    with pytest.raises(DatabaseError), transaction.atomic():
        raw_insert(TrialPlanRevision, **values)


def test_charge_policy_uses_market_public_uuid_and_rejects_category_scope(setup):
    graph = booking_graph(setup)
    graph.charge.refresh_from_db()
    assert graph.charge.policy_scope_version == 2
    revision = BusinessConfiguration.objects.create(
        namespace="payments",
        key="booking-charge-policy-reference",
        scope={"market": str(setup.market.public_id)},
        value="fictional-policy",
    )
    validate_policy_binding(
        revision, "payments", "booking-charge-policy-reference", market=setup.market
    )
    extra = BusinessConfiguration.objects.create(
        namespace="payments",
        key="booking-charge-policy-reference",
        scope={"market": str(setup.market.public_id), "category": str(setup.category.public_id)},
        value="fictional-policy",
    )
    with pytest.raises(ValidationError):
        validate_policy_binding(
            extra, "payments", "booking-charge-policy-reference", market=setup.market
        )


def test_unknown_null_scope_cannot_bypass_binding_checks(setup):
    import hashlib
    import json
    from types import SimpleNamespace

    scope = {"unsupported": None}
    revision = SimpleNamespace(namespace="trials", key="plan-policy-reference", scope=scope)
    with pytest.raises(ValidationError):
        validate_policy_binding(
            revision,
            "trials",
            "plan-policy-reference",
            market=setup.market,
            category=setup.category,
        )
    # Existing configuration DB guards permit retaining opaque historical scope.
    # The owning plan/charge binder must independently reject malformed dimensions.
    encoded = json.dumps(scope, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    with transaction.atomic():
        policy = raw_insert(
            BusinessConfiguration,
            namespace="trials",
            key="plan-policy-reference",
            scope=scope,
            scope_key="scope:" + hashlib.sha256(encoded.encode()).hexdigest(),
            value="fictional-policy",
        )
        with pytest.raises(DatabaseError), transaction.atomic():
            raw_insert(TrialPlanRevision, **plan_values(setup, policy))
