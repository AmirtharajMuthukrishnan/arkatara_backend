"""Fictional policies demonstrate boundaries; these are not launch configuration."""

from datetime import timedelta

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.models import StaffUser
from catalog.models import Category, Material, Purity
from common.reference import ReferenceStatus
from compliance.configuration import ConfigurationConflict, ConfigurationUnavailable
from compliance.models import BusinessConfiguration
from markets.models import Market, ServiceArea
from markets.services import category_eligibility_revision, coverage_state

pytestmark = pytest.mark.django_db


@pytest.fixture
def market():
    return Market.objects.create(
        code="SCENARIO-CITY", name="Scenario city", country_code="IN", status="ACTIVE"
    )


def area(market, **overrides):
    return ServiceArea.objects.create(
        **(
            {
                "market": market,
                "country_code": "IN",
                "postal_code": "560001",
                "status": "ACTIVE",
                "effective_from": timezone.now() - timedelta(days=1),
                "approval_reference": "scenario-only: reviewed coverage",
            }
            | overrides
        )
    )


def query(selected_market, **overrides):
    return APIClient().get(
        "/api/v1/serviceability/",
        {"market": selected_market.code, "country_code": "IN", "postal_code": "560001"} | overrides,
    )


def test_reference_api_handles_new_identities_and_hides_drafts(market):
    material = Material.objects.create(code="SCENARIO-METAL", name="Another metal", status="ACTIVE")
    purity = Purity.objects.create(
        material=material, code="SPEC-A", name="Specification A", status="ACTIVE"
    )
    hidden = Material.objects.create(code="DRAFT-METAL", name="Private draft")
    Purity.objects.create(material=hidden, code="HIDDEN", name="Hidden", status="ACTIVE")
    Category.objects.create(
        code="SCENARIO-CATEGORY", name="Scenario category", status="COMING_SOON"
    )
    Category.objects.create(code="DRAFT-CATEGORY", name="Private category")
    response = APIClient().get("/api/v1/reference-data/")
    assert response.status_code == 200
    data = response.json()
    assert data["schema_version"] == 1
    assert next(row for row in data["markets"] if row["code"] == market.code) == {
        "id": str(market.public_id),
        "code": market.code,
        "name": market.name,
        "country_code": "IN",
        "status": "ACTIVE",
    }
    assert "DRAFT-METAL" not in {row["code"] for row in data["materials"]}
    assert "HIDDEN" not in {row["code"] for row in data["purities"]}
    assert next(row for row in data["purities"] if row["id"] == str(purity.public_id))[
        "material_id"
    ] == str(material.public_id)
    assert {row["code"] for row in data["categories"]} == {"SCENARIO-CATEGORY"}
    assert set(data) == {"schema_version", "markets", "materials", "purities", "categories"}


def test_coverage_never_enables_booking_and_is_exactly_market_scoped(market):
    assert query(market).json()["state"] == "UNCONFIGURED"
    area(market)
    assert query(market).json() == {
        "market_code": market.code,
        "country_code": "IN",
        "postal_code": "560001",
        "state": "CONFIGURED",
        "bookable": False,
    }
    other = Market.objects.create(
        code="OTHER-SCENARIO", name="Other city", country_code="IN", status="ACTIVE"
    )
    assert query(other).json()["state"] == "UNCONFIGURED"
    assert query(market, postal_code="560002").json()["state"] == "UNCONFIGURED"
    assert query(market, country_code="GB").json()["state"] == "UNCONFIGURED"


@pytest.mark.parametrize(
    "status,expected", [("COMING_SOON", "COMING_SOON"), ("INACTIVE", "INACTIVE")]
)
def test_nonactive_market_cannot_be_enabled_by_coverage(market, status, expected):
    area(market)
    market.status = status
    market.save()
    assert query(market).json()["state"] == expected
    assert query(market).json()["bookable"] is False


def test_conflicting_coverage_is_reported_without_hub_or_latest_precedence(market):
    area(market)
    area(market)
    assert query(market).json()["state"] == "CONFLICT"
    assert query(market).json()["bookable"] is False


def test_coverage_requires_current_approved_rule_and_uses_exclusive_end(market):
    now = timezone.now()
    area(market, status="DRAFT", approval_reference="", effective_from=None)
    area(market, effective_from=now + timedelta(days=1))
    area(market, effective_from=now - timedelta(days=1), effective_to=now)
    assert coverage_state(market, country_code="IN", postal_code="560001", at=now) == "UNCONFIGURED"
    area(market, effective_from=now)
    assert coverage_state(market, country_code="IN", postal_code="560001", at=now) == "CONFIGURED"


@pytest.mark.parametrize(
    "values",
    [
        {},
        {"market": "SCENARIO-CITY"},
        {"market": "SCENARIO-CITY", "country_code": "IN\n", "postal_code": "560001"},
        {"market": "SCENARIO-CITY", "country_code": "IN", "postal_code": " "},
        {
            "market": "SCENARIO-CITY",
            "country_code": "IN",
            "postal_code": "560001",
            "hub": "anything",
        },
        {"market": ["SCENARIO-CITY", "OTHER"], "country_code": "IN", "postal_code": "560001"},
    ],
)
def test_malformed_coverage_queries_return_standard_errors(market, values):
    response = APIClient().get("/api/v1/serviceability/", values)
    assert response.status_code == 400
    assert response.json()["error"]["status_code"] == 400


def test_unknown_and_draft_markets_are_not_exposed(market):
    assert query(market, market="UNKNOWN").status_code == 404
    market.status = "DRAFT"
    market.save()
    assert query(market).status_code == 404


@pytest.mark.parametrize("path", ["reference-data/", "serviceability/"])
def test_reference_contracts_reject_mutations(path):
    assert APIClient().post(f"/api/v1/{path}", {}).status_code == 405


def test_category_policy_requires_exact_scope_and_retains_revision_evidence(market):
    material = Material.objects.create(code="POLICY-METAL", name="Policy metal", status="ACTIVE")
    category = Category.objects.create(
        code="POLICY-CATEGORY", name="Policy category", status="ACTIVE"
    )
    actor = StaffUser.objects.create_user(username="scenario-policy-reviewer", is_staff=True)
    now = timezone.now() - timedelta(minutes=1)
    policy = {
        "namespace": "trials",
        "key": "category-eligibility",
        "value": True,
        "status": "ACTIVE",
        "approval_recorded_by": actor,
        "approved_at": now,
        "effective_from": now,
        "approval_reference": "scenario-only: eligibility review",
    }
    BusinessConfiguration.objects.create(**policy, scope={})
    dimensions = {"market": market, "material": material, "category": category}
    with pytest.raises(ConfigurationUnavailable):
        category_eligibility_revision(**dimensions)
    scope = {key: str(row.public_id) for key, row in dimensions.items()}
    revision = BusinessConfiguration.objects.create(**(policy | {"value": False}), scope=scope)
    assert category_eligibility_revision(**dimensions).public_id == revision.public_id
    assert category_eligibility_revision(**dimensions).value is False
    BusinessConfiguration.objects.create(**policy, scope=scope, version=2)
    with pytest.raises(ConfigurationConflict):
        category_eligibility_revision(**dimensions)
    material.status = ReferenceStatus.COMING_SOON
    material.save()
    with pytest.raises(ConfigurationUnavailable, match="must all be active"):
        category_eligibility_revision(**dimensions)
