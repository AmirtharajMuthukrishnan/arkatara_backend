"""Explicit fictional values test representation, never production pricing/tax policy."""

from dataclasses import FrozenInstanceError
from decimal import Decimal
from uuid import uuid4

import pytest
from django.core.exceptions import ValidationError
from django.db.models.deletion import ProtectedError

from billing.snapshots import HistoricalPriceSnapshot
from catalog.models import Category, Material, Product, ProductVariant
from catalog.pricing import PriceRevision, decimal_text, exact_decimal, validate_pricing_inputs


@pytest.fixture
def variant():
    material = Material.objects.create(code="FIXTURE_METAL", name="Fictional material")
    category = Category.objects.create(code="FIXTURE_CATEGORY", name="Fictional category")
    product = Product.objects.create(
        code="FIXTURE_DESIGN", name="Original design", category=category
    )
    return ProductVariant.objects.create(
        product=product,
        material=material,
        sku="FIXTURE-PRICING-VARIANT",
        size_label="Original specification",
    )


def snapshot_data(**overrides):
    data = {
        "schema_version": 1,
        "catalogue": {
            "product_id": str(uuid4()),
            "variant_id": str(uuid4()),
            "product_name": "Fictional design",
            "variant_sku": "FIXTURE-SNAPSHOT",
            "category_code": "FIXTURE_CATEGORY",
            "material_code": "FIXTURE_METAL",
            "purity_code": None,
            "specifications": {"size": "Fictional size"},
        },
        "price_revision_id": str(uuid4()),
        "price_revision": 1,
        "mode": "FIXED",
        "currency": "INR",
        "agreed_amount": Decimal("123.456789"),
        "inputs": {},
        "tax": {"state": "UNCONFIGURED"},
    }
    return data | overrides


@pytest.mark.parametrize(
    "value",
    [True, 12.5, float("nan"), Decimal("NaN"), Decimal("Infinity"), "invalid", "1.1234567"],
)
def test_decimal_contract_rejects_lossy_or_invalid_inputs(value):
    with pytest.raises(ValidationError):
        exact_decimal(value)


def test_decimal_capacity_does_not_round_or_choose_currency_exponent():
    assert decimal_text("-0.000000") == "0.000000"
    assert exact_decimal("123.456789") == Decimal("123.456789")
    assert exact_decimal("999999999999999999.999999") == Decimal("999999999999999999.999999")
    with pytest.raises(ValidationError):
        exact_decimal("1000000000000000000")
    with pytest.raises(ValidationError):
        exact_decimal("-0.01")


@pytest.mark.parametrize(
    "inputs",
    [
        {"weight_value": "2.25"},
        {"weight_unit": "g"},
        {"weight_value": "0", "weight_unit": "g"},
        {"rate_amount": "3.75"},
        {"rate_source_reference": "fixture:unmatched-source"},
        {"charges": [{"code": "fictional-fee", "amount": 1.5}]},
        {"charges": [{"code": "fictional-fee", "amount": "2", "tax_rate": "1"}]},
        {"charges": [{"code": "same", "amount": "2"}, {"code": "same", "amount": "3"}]},
        {"formula": "weight * rate"},
    ],
)
def test_inputs_require_explicit_units_and_typed_components(inputs):
    with pytest.raises(ValidationError):
        validate_pricing_inputs(inputs)


def test_weight_evidence_is_representable_without_executing_a_formula():
    inputs = {
        "weight_value": Decimal("2.123456"),
        "weight_unit": "g",
        "rate_amount": Decimal("3.765432"),
        "rate_unit": "INR/g",
        "rate_source_reference": "fixture:rate-source-v2",
        "formula_reference": "fixture:unimplemented-formula-v4",
        "charges": [{"code": "fictional-charge", "amount": Decimal("5.125")}],
    }
    # Deliberately not a computed weight*rate result. The contract records supplied evidence.
    data = snapshot_data(mode="WEIGHT_BASED", agreed_amount="78.654321", inputs=inputs)
    stored = HistoricalPriceSnapshot(data).to_dict()
    assert stored["agreed_amount"] == "78.654321"
    assert stored["inputs"]["weight_value"] == "2.123456"
    assert stored["inputs"]["rate_amount"] == "3.765432"
    assert stored["inputs"]["formula_reference"] == inputs["formula_reference"]
    assert stored["tax"] == {"state": "UNCONFIGURED"}


def test_snapshot_is_deeply_independent_of_input_and_output_mutations():
    source = snapshot_data()
    original_name = source["catalogue"]["product_name"]
    snapshot = HistoricalPriceSnapshot(source)
    source["catalogue"]["product_name"] = "Changed after recording"
    source["catalogue"]["specifications"]["size"] = "Changed size"
    source["tax"]["state"] = "RECORDED"
    output = snapshot.to_dict()
    output["catalogue"]["specifications"]["size"] = "Changed returned copy"
    output["agreed_amount"] = "0"
    persisted = snapshot.to_dict()
    assert persisted["catalogue"]["product_name"] == original_name
    assert persisted["catalogue"]["specifications"]["size"] == "Fictional size"
    assert persisted["agreed_amount"] == "123.456789"
    assert persisted["tax"] == {"state": "UNCONFIGURED"}
    assert HistoricalPriceSnapshot(persisted).to_dict() == persisted
    with pytest.raises(FrozenInstanceError):
        snapshot._serialized = "{}"


def test_tax_evidence_keeps_supplied_policy_rates_and_amounts_separate():
    tax = {
        "state": "RECORDED",
        "policy_reference": "fixture:tax-policy-v4",
        "classification_reference": "fixture:classification",
        "treatment_reference": "fixture:explicit-treatment",
        "rounding_reference": "fixture:explicit-rounding",
        "components": [
            {
                "code": "fictional-tax",
                "rate": "1.234567",
                "rate_unit": "percent",
                "taxable_amount": "100",
                "amount": "1.234567",
            }
        ],
    }
    snapshot = HistoricalPriceSnapshot(snapshot_data(tax=tax))
    assert snapshot.to_dict()["tax"] == tax
    assert snapshot.to_dict()["agreed_amount"] == "123.456789"
    tax["components"][0]["rate"] = "99"
    assert snapshot.to_dict()["tax"]["components"][0]["rate"] == "1.234567"


@pytest.mark.parametrize(
    "tax",
    [
        {},
        {"state": "UNCONFIGURED", "amount": "0"},
        {"state": "RECORDED", "components": []},
        {"state": "RECORDED", "policy_reference": "", "components": []},
        {"state": "RECORDED", "policy_reference": "fixture:policy", "components": [{"rate": 3}]},
    ],
)
def test_missing_tax_is_never_silently_zero_or_approved(tax):
    with pytest.raises(ValidationError):
        HistoricalPriceSnapshot(snapshot_data(tax=tax))


@pytest.mark.parametrize(
    "overrides",
    [
        {"agreed_amount": None},
        {"agreed_amount": 1.25},
        {"agreed_amount": "1.0000001"},
        {"currency": "inr"},
        {"currency": ""},
        {"price_revision": True},
        {"price_revision": 0},
        {"price_revision_id": "not-a-uuid"},
        {"mode": "AUTO"},
        {"schema_version": 2},
        {"schema_version": True},
    ],
)
def test_snapshot_requires_explicit_exact_values_and_provenance(overrides):
    with pytest.raises(ValidationError):
        HistoricalPriceSnapshot(snapshot_data(**overrides))


@pytest.mark.django_db
def test_missing_price_is_retained_as_unknown_and_draft(variant):
    revision = PriceRevision.objects.create(
        variant=variant, revision=1, mode=PriceRevision.Mode.FIXED, currency="INR"
    )
    revision.refresh_from_db()
    assert revision.status == "DRAFT"
    assert revision.fixed_amount is None
    assert revision.pricing_inputs()["charges"] == []
    with pytest.raises(ValidationError):
        PriceRevision.objects.create(
            variant=variant, revision=2, status="ACTIVE", mode="FIXED", currency="INR"
        )


@pytest.mark.django_db
def test_price_revisions_preserve_both_modes_and_explicit_components(variant):
    fixed = PriceRevision.objects.create(
        variant=variant, revision=1, mode="FIXED", currency="INR", fixed_amount="12.123456"
    )
    weighted = PriceRevision.objects.create(
        variant=variant,
        revision=2,
        mode="WEIGHT_BASED",
        currency="INR",
        weight_value="2.123456",
        weight_unit="g",
        rate_amount="3.654321",
        rate_unit="INR/g",
        rate_source_reference="fixture:rate-source-v1",
        formula_reference="fixture:formula-v2",
        charges=[{"code": "fictional-charge", "amount": Decimal("5.123456")}],
    )
    fixed.refresh_from_db()
    weighted.refresh_from_db()
    assert fixed.fixed_amount == Decimal("12.123456")
    assert weighted.fixed_amount is None
    assert weighted.weight_value == Decimal("2.123456")
    assert weighted.charges[0]["amount"] == "5.123456"
    with pytest.raises(ValidationError):
        PriceRevision.objects.create(
            variant=variant,
            revision=3,
            mode="WEIGHT_BASED",
            currency="INR",
            fixed_amount="1",
        )


@pytest.mark.django_db
@pytest.mark.parametrize("amount", [1.125, "1.0000001", Decimal("NaN"), "-1"])
def test_model_writes_reject_inexact_price_values(variant, amount):
    with pytest.raises(ValidationError):
        PriceRevision.objects.create(
            variant=variant, revision=1, mode="FIXED", currency="INR", fixed_amount=amount
        )
    assert not PriceRevision.objects.exists()


@pytest.mark.django_db
def test_revision_history_blocks_orm_overwrite_deletion_and_bulk_writes(variant):
    original = PriceRevision.objects.create(
        variant=variant, revision=1, mode="FIXED", currency="INR", fixed_amount="12.123456"
    )
    original.fixed_amount = Decimal("99")
    attempts = [
        original.save,
        original.delete,
        lambda: PriceRevision.objects.filter(pk=original.pk).update(fixed_amount="99"),
        lambda: PriceRevision.objects.filter(pk=original.pk).delete(),
        lambda: PriceRevision.objects.bulk_update([original], ["fixed_amount"]),
        lambda: PriceRevision.objects.bulk_create([original]),
    ]
    for attempt in attempts:
        with pytest.raises(TypeError, match="append-only"):
            attempt()
    with pytest.raises(ValidationError):
        PriceRevision.objects.create(
            variant=variant, revision=1, mode="FIXED", currency="INR", fixed_amount="3"
        )
    with pytest.raises(ProtectedError):
        variant.delete()
    original.refresh_from_db()
    assert original.fixed_amount == Decimal("12.123456")


@pytest.mark.django_db
def test_recorded_snapshot_survives_catalogue_edits_and_new_price_revision(variant):
    first = PriceRevision.objects.create(
        variant=variant, revision=1, mode="FIXED", currency="INR", fixed_amount="12.123456"
    )
    catalogue = {
        "product_id": str(variant.product.public_id),
        "variant_id": str(variant.public_id),
        "product_name": variant.product.name,
        "variant_sku": variant.sku,
        "category_code": variant.product.category.code,
        "material_code": variant.material.code,
        "specifications": {"size": variant.size_label},
    }
    snapshot = HistoricalPriceSnapshot(
        snapshot_data(
            catalogue=catalogue,
            price_revision_id=str(first.public_id),
            price_revision=first.revision,
            agreed_amount=first.fixed_amount,
            inputs=first.pricing_inputs(),
        )
    )
    before = snapshot.to_dict()
    variant.product.name = "Changed catalogue design"
    variant.product.save()
    variant.material.name = "Changed catalogue material display name"
    variant.material.save()
    PriceRevision.objects.create(
        variant=variant, revision=2, mode="FIXED", currency="INR", fixed_amount="88.765432"
    )
    assert snapshot.to_dict() == before
    assert before["agreed_amount"] == "12.123456"
    assert before["catalogue"]["product_name"] == "Original design"
    assert before["price_revision_id"] == str(first.public_id)
