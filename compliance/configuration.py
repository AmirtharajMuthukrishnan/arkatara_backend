"""Typed configuration contracts, without commercial values or scope precedence."""

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from itertools import islice

from django.core.exceptions import ValidationError
from django.db.models import Q
from django.utils import timezone


@dataclass(frozen=True)
class ConfigurationDefinition:
    value_type: type
    decision_reference: str


# Names and units are technical contracts. No entry supplies a policy/default value.
# Refs: BUSINESS_RULES.md R-07, R-28; DECISIONS.md BD-02, BD-13.
CONFIGURATION_DEFINITIONS = {
    ("trials", "maximum-boxes"): ConfigurationDefinition(int, "BD-13"),
    ("cart", "expiry-seconds"): ConfigurationDefinition(int, "BD-13"),
    ("inventory", "reservation-timeout-seconds"): ConfigurationDefinition(int, "BD-02"),
    ("trials", "category-eligibility"): ConfigurationDefinition(bool, "BD-13"),
    ("trials", "plan-policy-reference"): ConfigurationDefinition(str, "BD-03"),
    ("payments", "booking-charge-policy-reference"): ConfigurationDefinition(str, "BD-01"),
    # Reviewed procedure documents are supplied explicitly, never seeded by onboarding.
    # Refs: BUSINESS_RULES.md R-17, R-28; DECISIONS.md BD-09.
    ("inventory", "receiving-procedure-reference"): ConfigurationDefinition(str, "BD-09"),
    ("inventory", "qc-procedure-reference"): ConfigurationDefinition(str, "BD-09"),
}


class ConfigurationUnavailable(RuntimeError):
    """The exact requested policy has no approved, effective revision."""


def validate_policy_binding(revision, namespace, key, *, market, category=None, scope_version=2):
    """Check semantic identity and explicit dimensions; approval is a later gate.

    Version 2 uses public UUIDs, matching existing configuration contracts.
    Version 1 preserves recorded code-based history; new owners must use version 2.
    Global references remain global. No scope fallback or precedence is inferred.
    Refs: BUSINESS_RULES.md R-07, R-28; DECISIONS.md BD-01, BD-03, BD-13.
    """
    if scope_version not in (1, 2):
        raise ValidationError("Unknown policy scope identity version.")
    dimensions = {"market": market.code if scope_version == 1 else str(market.public_id)}
    if category is not None:
        dimensions["category"] = category.code if scope_version == 1 else str(category.public_id)
    if (revision.namespace, revision.key) != (namespace, key) or any(
        dimension not in dimensions or not isinstance(value, str) or dimensions[dimension] != value
        for dimension, value in revision.scope.items()
    ):
        raise ValidationError("Policy identity or scope does not match the obligation/offering.")


class ConfigurationConflict(RuntimeError):
    """Multiple revisions claim to govern the same exact scope and instant."""


def configuration_scope_key(scope: dict) -> str:
    if not isinstance(scope, dict):
        raise ValidationError({"scope": "Scope must be a JSON object."})
    if any(
        not isinstance(key, str)
        or not key.strip()
        or not isinstance(value, str)
        or not value.strip()
        for key, value in scope.items()
    ):
        raise ValidationError(
            {"scope": "Scope dimensions and identifiers must be nonempty strings."}
        )
    if not scope:
        return "global"
    serialized = json.dumps(scope, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return "scope:" + hashlib.sha256(serialized.encode()).hexdigest()


def validate_configuration_value(namespace: str, key: str, value: object) -> None:
    definition = CONFIGURATION_DEFINITIONS.get((namespace, key))
    if definition is None:
        raise ValidationError(
            {"key": "Unknown configuration key; define its typed contract first."}
        )
    # bool is an int subclass, but it must never be accepted as a duration/limit.
    if type(value) is not definition.value_type:
        expected = {bool: "boolean", int: "positive integer", str: "nonblank procedure reference"}[
            definition.value_type
        ]
        raise ValidationError({"value": f"This setting requires a {expected}."})
    if definition.value_type is int and value <= 0:
        raise ValidationError({"value": "This setting requires a positive integer."})
    if definition.value_type is str and (not value.strip() or value != value.strip()):
        raise ValidationError(
            {"value": "Use a nonblank procedure reference without outer whitespace."}
        )


def get_approved_configuration(
    namespace: str, key: str, *, scope: dict, at: datetime | None = None
):
    """Return the immutable revision, so later commitments can retain its identity.

    Callers supply their complete scope. There is no global fallback, latest-version
    winner, merging of dimensions, or interpretation of missing policy as a value.
    Refs: BUSINESS_RULES.md R-07, R-28; DECISIONS.md BD-02, BD-13.
    """
    from compliance.models import BusinessConfiguration

    if (namespace, key) not in CONFIGURATION_DEFINITIONS:
        raise ValidationError({"key": "Unknown configuration key."})
    scope_key = configuration_scope_key(scope)
    instant = at if at is not None else timezone.now()
    if timezone.is_naive(instant):
        raise ValueError("Configuration lookup requires a timezone-aware instant.")
    eligible_revisions = (
        BusinessConfiguration.objects.filter(
            namespace=namespace,
            key=key,
            scope_key=scope_key,
            scope=scope,
            status=BusinessConfiguration.Status.ACTIVE,
            effective_from__lte=instant,
            approval_recorded_by__isnull=False,
            approved_at__lte=instant,
        )
        .filter(Q(effective_to__isnull=True) | Q(effective_to__gt=instant))
        .exclude(approval_reference="")
        .order_by("pk")
    )
    # Legacy ACTIVE rows may have no approval evidence. Match Python's whitespace
    # validation before counting eligible revisions, including non-ASCII whitespace.
    candidates = list(
        islice(
            (
                revision
                for revision in eligible_revisions.iterator()
                if revision.approval_reference.strip()
            ),
            2,
        )
    )
    if len(candidates) > 1:
        raise ConfigurationConflict(f"Conflicting approved policy: {namespace}.{key}:{scope_key}.")
    if not candidates:
        raise ConfigurationUnavailable(f"No approved policy: {namespace}.{key}:{scope_key}.")
    candidate = candidates[0]
    candidate.clean()
    return candidate
