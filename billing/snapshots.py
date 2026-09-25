"""Immutable price/tax evidence for later transaction records, never a calculator.

Recording a supplied agreed amount does not choose when the business commits it,
approve a price revision, prove payment or issue an invoice (BD-04 and CA-02).
"""

import json
from dataclasses import dataclass
from uuid import UUID

from django.core.exceptions import ValidationError

from catalog.pricing import decimal_text, text_value, validate_currency, validate_pricing_inputs


def _identifier(value) -> str:
    try:
        return str(UUID(str(value)))
    except (ValueError, AttributeError, TypeError) as error:
        raise ValidationError("Snapshot identifiers must be UUIDs.") from error


def _catalogue(value) -> dict:
    required = {
        "product_id",
        "variant_id",
        "product_name",
        "variant_sku",
        "category_code",
        "material_code",
    }
    optional = {"product_code", "purity_code", "specifications"}
    if not isinstance(value, dict) or not required <= value.keys():
        raise ValidationError("Snapshot catalogue identity and description are required.")
    if value.keys() - (required | optional):
        raise ValidationError("Unknown catalogue snapshot fields are not accepted.")
    result = {
        key: _identifier(item) if key.endswith("_id") else text_value(item)
        for key, item in value.items()
        if key not in {"specifications", "purity_code"}
    }
    if "purity_code" in value:
        result["purity_code"] = (
            None if value["purity_code"] is None else text_value(value["purity_code"])
        )
    if "specifications" in value:
        specifications = value["specifications"]
        if not isinstance(specifications, dict):
            raise ValidationError("Snapshot specifications must be a text-to-text object.")
        result["specifications"] = {
            text_value(key): text_value(item) for key, item in specifications.items()
        }
    return result


def _tax_evidence(value) -> dict:
    if not isinstance(value, dict):
        raise ValidationError("Tax evidence must explicitly identify its configuration state.")
    if value.get("state") == "UNCONFIGURED":
        if set(value) != {"state"}:
            raise ValidationError("Unconfigured tax must not imply a rate or tax amount.")
        return {"state": "UNCONFIGURED"}
    required = {"state", "policy_reference", "components"}
    optional = {"classification_reference", "treatment_reference", "rounding_reference"}
    if value.get("state") != "RECORDED" or not required <= value.keys():
        raise ValidationError("Recorded tax requires a policy reference and explicit components.")
    if value.keys() - (required | optional):
        raise ValidationError("Unknown tax evidence fields are not accepted.")
    result = {key: text_value(item) for key, item in value.items() if key != "components"}
    if not isinstance(value["components"], list):
        raise ValidationError("Tax components must be a list.")
    components = []
    codes = set()
    for component in value["components"]:
        keys = {"code", "rate", "rate_unit", "taxable_amount", "amount"}
        if not isinstance(component, dict) or set(component) != keys:
            raise ValidationError(
                "Tax components require code, rate/unit, taxable amount and amount."
            )
        code = text_value(component["code"])
        if code in codes:
            raise ValidationError("Tax component codes must be unique.")
        codes.add(code)
        components.append(
            {
                "code": code,
                "rate": decimal_text(component["rate"]),
                "rate_unit": text_value(component["rate_unit"]),
                "taxable_amount": decimal_text(component["taxable_amount"]),
                "amount": decimal_text(component["amount"]),
            }
        )
    result["components"] = components
    return result


@dataclass(frozen=True, slots=True, init=False)
class HistoricalPriceSnapshot:
    """A versioned, validated value; no live model reference can rewrite its contents."""

    _serialized: str

    def __init__(self, data: dict):
        required = {
            "schema_version",
            "catalogue",
            "price_revision_id",
            "price_revision",
            "mode",
            "currency",
            "agreed_amount",
            "inputs",
            "tax",
        }
        if not isinstance(data, dict) or set(data) != required:
            raise ValidationError(
                "Snapshot requires the complete versioned price evidence contract."
            )
        if type(data["schema_version"]) is not int or data["schema_version"] != 1:
            raise ValidationError("Unsupported price snapshot schema version.")
        if type(data["price_revision"]) is not int or data["price_revision"] < 1:
            raise ValidationError("Price revision must be a positive integer.")
        if data["mode"] not in ("FIXED", "WEIGHT_BASED"):
            raise ValidationError("Snapshot pricing mode must be FIXED or WEIGHT_BASED.")
        normalized = {
            "schema_version": 1,
            "catalogue": _catalogue(data["catalogue"]),
            "price_revision_id": _identifier(data["price_revision_id"]),
            "price_revision": data["price_revision"],
            "mode": data["mode"],
            "currency": validate_currency(data["currency"]),
            "agreed_amount": decimal_text(data["agreed_amount"]),
            "inputs": validate_pricing_inputs(data["inputs"]),
            "tax": _tax_evidence(data["tax"]),
        }
        object.__setattr__(
            self, "_serialized", json.dumps(normalized, sort_keys=True, allow_nan=False)
        )

    def to_dict(self) -> dict:
        """Return an independent JSON-compatible copy for a future immutable record."""
        return json.loads(self._serialized)
