"""Exact geographic coverage and policy lookup, never a booking authorization."""

from datetime import datetime
from itertools import islice

from django.db.models import Q
from django.utils import timezone

from common.reference import ReferenceStatus
from compliance.configuration import ConfigurationUnavailable, get_approved_configuration
from markets.models import Market, ServiceArea


def coverage_state(market: Market, *, country_code: str, postal_code: str, at=None) -> str:
    if market.status == ReferenceStatus.COMING_SOON:
        return "COMING_SOON"
    if market.status != ReferenceStatus.ACTIVE:
        return "INACTIVE"
    instant = at if at is not None else timezone.now()
    if timezone.is_naive(instant):
        raise ValueError("Coverage lookup requires a timezone-aware instant.")
    candidates = (
        ServiceArea.objects.filter(
            market=market,
            country_code=country_code,
            postal_code=postal_code,
            status=ServiceArea.Status.ACTIVE,
            effective_from__lte=instant,
        )
        .filter(Q(effective_to__isnull=True) | Q(effective_to__gt=instant))
        .exclude(approval_reference="")
        .order_by("pk")
    )
    eligible = list(
        islice((row for row in candidates.iterator() if row.approval_reference.strip()), 2)
    )
    if len(eligible) > 1:
        return "CONFLICT"
    return "CONFIGURED" if eligible else "UNCONFIGURED"


def category_eligibility_revision(*, market, material, category, at: datetime | None = None):
    """Return explicit policy evidence; no fallback, implicit eligibility or booking.

    Scope dimensions are stable public identifiers. Future plan/mixed-material
    policy remains separate; this lookup only describes one category/material.
    """
    if any(item.status != ReferenceStatus.ACTIVE for item in (market, material, category)):
        raise ConfigurationUnavailable("Market, material and category must all be active.")
    return get_approved_configuration(
        "trials",
        "category-eligibility",
        scope={
            "market": str(market.public_id),
            "material": str(material.public_id),
            "category": str(category.public_id),
        },
        at=at,
    )
