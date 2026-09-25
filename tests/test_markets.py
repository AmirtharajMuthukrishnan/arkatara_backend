"""Service coverage examples are scenario data, not approved launch PIN codes."""

from datetime import timedelta

import pytest
from django.core.exceptions import ValidationError
from django.utils import timezone

from common.reference import ReferenceStatus
from markets.models import Hub, Market, ServiceArea

pytestmark = pytest.mark.django_db


@pytest.fixture
def scenario_market():
    return Market.objects.create(code="COVERAGE-TEST", name="Coverage scenario", country_code="IN")


def test_market_supports_multiple_hubs_without_choosing_fulfilling_hub(scenario_market):
    first = Hub.objects.create(code="COVERAGE-HUB-1", name="Scenario 1", market=scenario_market)
    second = Hub.objects.create(code="COVERAGE-HUB-2", name="Scenario 2", market=scenario_market)
    assert list(scenario_market.hubs.all()) == [first, second]


def test_service_area_defaults_to_unapproved_draft(scenario_market):
    area = ServiceArea.objects.create(
        market=scenario_market, country_code="IN", postal_code="560001"
    )
    assert area.status == ServiceArea.Status.DRAFT
    assert area.approval_reference == ""
    assert area.effective_from is None


def test_active_service_area_requires_explicit_approval_and_start(scenario_market):
    area = ServiceArea(
        market=scenario_market,
        country_code="IN",
        postal_code="560001",
        status=ServiceArea.Status.ACTIVE,
    )
    with pytest.raises(ValidationError, match="approval reference"):
        area.save()
    area.approval_reference = "test-scenario-only: reviewed coverage"
    area.effective_from = timezone.now()
    area.save()
    assert area.pk is not None


@pytest.mark.parametrize(
    "postal_code", [None, "", " 560001", "560001 ", "56001", "000001", "ABCDE1", "５６０００１"]
)
def test_indian_pin_format_is_validated_without_selecting_serviceable_pins(
    scenario_market, postal_code
):
    with pytest.raises(ValidationError):
        ServiceArea.objects.create(
            market=scenario_market, country_code="IN", postal_code=postal_code
        )


def test_service_area_country_must_match_market(scenario_market):
    with pytest.raises(ValidationError, match="market's country"):
        ServiceArea.objects.create(
            market=scenario_market, country_code="GB", postal_code="SW1A 1AA"
        )


def test_non_indian_country_can_represent_postal_format_without_new_schema():
    market = Market.objects.create(
        code="POSTAL-TEST", name="Postal format scenario", country_code="GB"
    )
    area = ServiceArea.objects.create(market=market, country_code="GB", postal_code="SW1A 1AA")
    assert area.postal_code == "SW1A 1AA"


def test_postal_identity_is_scoped_to_market(scenario_market):
    another = Market.objects.create(code="OTHER-COVERAGE", name="Other scenario", country_code="IN")
    first = ServiceArea.objects.create(
        market=scenario_market, country_code="IN", postal_code="560001"
    )
    second = ServiceArea.objects.create(market=another, country_code="IN", postal_code="560001")
    assert first.market_id != second.market_id


def test_coverage_history_allows_same_postal_identity_with_different_effective_periods(
    scenario_market,
):
    start = timezone.now()
    common = {
        "market": scenario_market,
        "country_code": "IN",
        "postal_code": "560001",
        "status": ServiceArea.Status.ACTIVE,
        "approval_reference": "test-scenario-only: reviewed coverage",
    }
    first = ServiceArea.objects.create(
        **common, effective_from=start, effective_to=start + timedelta(days=1)
    )
    second = ServiceArea.objects.create(**common, effective_from=start + timedelta(days=1))
    assert first.pk != second.pk


def test_coverage_effective_range_and_timezone_are_validated(scenario_market):
    start = timezone.now()
    area = ServiceArea(
        market=scenario_market,
        country_code="IN",
        postal_code="560001",
        effective_from=start,
        effective_to=start - timedelta(days=1),
    )
    with pytest.raises(ValidationError, match="service_area_effective_range_valid"):
        area.save()
    area.effective_to = None
    area.effective_from = start.replace(tzinfo=None)
    with pytest.raises(ValidationError, match="timezone-aware"):
        area.save()


def test_service_area_identity_cannot_be_moved_to_another_market(scenario_market):
    area = ServiceArea.objects.create(
        market=scenario_market, country_code="IN", postal_code="560001"
    )
    area.market = Market.objects.create(
        code="OTHER-AREA-CITY", name="Other city", country_code="IN"
    )
    with pytest.raises(ValidationError, match="identity cannot"):
        area.save()


def test_coming_soon_market_is_distinct_from_active_without_city_enumeration():
    future = Market.objects.create(
        code="FUTURE-SCENARIO",
        name="Future scenario",
        country_code="IN",
        status=ReferenceStatus.COMING_SOON,
    )
    assert future.status == ReferenceStatus.COMING_SOON
    future.status = ReferenceStatus.ACTIVE
    future.save()
    assert future.status == ReferenceStatus.ACTIVE


@pytest.mark.parametrize("country_code", ["in", "IN\n", " IN", "IND", "I", "", None])
def test_invalid_country_identifiers_are_rejected(country_code):
    with pytest.raises(ValidationError):
        Market.objects.create(
            code="COUNTRY-INVALID", name="Invalid scenario", country_code=country_code
        )


@pytest.mark.parametrize("name", [None, "", "   "])
def test_invalid_reference_names_are_validation_errors(name):
    with pytest.raises(ValidationError):
        Market.objects.create(code="NAME-INVALID", name=name, country_code="IN")
