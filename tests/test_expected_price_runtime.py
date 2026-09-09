import json
import math
from copy import deepcopy

import pytest

from kr_stock_autotrader.expected_price import (
    CALCULATION_ERROR,
    COMPUTED,
    HOLD_INVALID_INPUT,
    HOLD_MISSING_INPUT,
    evaluate_expected_price,
    evaluate_expected_prices,
    evaluate_persisted_expected_price,
)


INPUT_AS_OF = "2026-09-07T07:30:00+09:00"
INPUT_KNOWN_AT = "2026-09-07T07:30:00+09:00"
EVIDENCE_KNOWN_AT = "2026-09-07T07:00:00+09:00"
EVALUATION_AS_OF = "2026-09-07T09:05:00+09:00"
ISSUER = "005930"
EVENT = "event-1"


def field(
    value,
    *,
    unit="KRW",
    currency="KRW",
    source_status="official_exact",
    issuer_id=ISSUER,
    event_id=EVENT,
    as_of=INPUT_AS_OF,
    known_at=INPUT_KNOWN_AT,
    source_ref="DART:receipt-1",
    **extra,
):
    return {
        "value": value,
        "unit": unit,
        "currency": currency,
        "source_status": source_status,
        "issuer_id": issuer_id,
        "event_id": event_id,
        "as_of": as_of,
        "known_at": known_at,
        "source_ref": source_ref,
        **extra,
    }


def ratio(value, **extra):
    return field(value, unit="ratio", currency=None, **extra)


def years(value, **extra):
    return field(value, unit="years", currency=None, **extra)


def baseline(**override):
    result = {
        "schema_version": "giraffe-premarket-baseline-v1",
        "security_id": ISSUER,
        "source": "KIS",
        "source_field": "previous_close_krw",
        "price_krw": 20_000,
        "unit": "KRW/share",
        "session_date": "2026-09-04",
        "price_known_at": "2026-09-04T15:30:00+09:00",
        "retrieved_at": "2026-09-07T08:00:03+09:00",
    }
    result.update(override)
    return result


def valid_inputs(branch="peer_existing", *, scenario="BASE"):
    result = {
        "schema_version": "giraffe-expected-price-input-v1",
        "formula_id": "ENTERPRISE_PLUS_EVENT_ANNUAL_FCF_V1",
        "issuer_id": ISSUER,
        "security_id": ISSUER,
        "event_id": EVENT,
        "scenario": scenario,
        "as_of": INPUT_AS_OF,
        "known_at": INPUT_KNOWN_AT,
        "valuation_branch": branch,
        "existing": {
            "normalized_fcf": field(10_000_000_000),
            "discount_rate": ratio(0.10),
            "periods": years(2),
            "terminal_growth": ratio(0.02),
            "normalized_metric": field(100_000_000_000),
            "peer_multiple": field(2.0, unit="multiple", currency=None),
        },
        "contract": {
            "amount": field(50_000_000_000),
            "margin": ratio(0.20),
            "tax_rate": ratio(0.20),
            "fulfillment_probability": ratio(0.80),
            "discount_rate": ratio(0.10),
            "annual_cash_flows": [
                {
                    "year": 1,
                    "revenue_proportion": ratio(0.50),
                    "cash_time": years(1.0),
                    "non_cash_expense": field(0),
                    "working_capital": field(500_000_000),
                    "capex": field(500_000_000),
                },
                {
                    "year": 2,
                    "revenue_proportion": ratio(0.50),
                    "cash_time": years(2.0),
                    "non_cash_expense": field(0),
                    "working_capital": field(500_000_000),
                    "capex": field(500_000_000),
                },
            ],
        },
        "balance_sheet": {
            "cash": field(10_000_000_000),
            "debt": field(5_000_000_000),
            "senior_claims": field(0),
        },
        "diluted_shares": field(10_000_000, unit="shares", currency=None),
        "duplicate_potential_shares": False,
        "market_baseline": baseline(),
    }
    return result


def persisted_inputs(inputs=None, *, snapshot_as_string=True, root_expected_price=None):
    raw = deepcopy(inputs or valid_inputs())
    raw.pop("market_baseline")
    snapshot = {"economic_terms": {"expected_price_inputs": raw}}
    if root_expected_price is not None:
        snapshot["expected_price"] = root_expected_price
    return {
        "id": 11,
        "symbol": ISSUER,
        "dedupe_key": EVENT,
        "known_at": EVIDENCE_KNOWN_AT,
        "snapshot": json.dumps(snapshot) if snapshot_as_string else snapshot,
    }


def filter_result(base=None):
    return {
        "id": 22,
        "as_of": "2026-09-07T08:00:03+09:00",
        "known_at": "2026-09-07T08:00:03+09:00",
        "raw_inputs": {
            "market_data_known_at": "2026-09-07T08:00:03+09:00",
            "expected_price_baseline": deepcopy(base or baseline()),
        },
    }


def evaluate(inputs):
    return evaluate_expected_price(
        inputs,
        evaluation_as_of=EVALUATION_AS_OF,
        evidence_known_at=EVIDENCE_KNOWN_AT,
    )


def test_full_selected_peer_branch_and_annual_cash_flow_stream_compute_integer_tick():
    result = evaluate(valid_inputs())

    assert result["status"] == COMPUTED
    assert result["calculated_value"] == 20_900
    assert type(result["calculated_value"]) is int
    assert result["tick_size_krw"] == 50
    assert result["calculated_value"] % result["tick_size_krw"] == 0
    assert result["formula_id"] == "ENTERPRISE_PLUS_EVENT_ANNUAL_FCF_V1"
    assert result["calculated_value_unit"] == "KRW/share"
    assert result["baseline_price_krw"] == 20_000


def test_event_cash_time_is_used_in_each_annual_discount_not_only_terminal_year():
    prompt = valid_inputs()
    near = evaluate(prompt)
    prompt["contract"]["annual_cash_flows"][0]["cash_time"]["value"] = 10.0
    prompt["contract"]["annual_cash_flows"][1]["cash_time"]["value"] = 20.0
    far = evaluate(prompt)

    assert near["status"] == far["status"] == COMPUTED
    assert near["calculated_value"] > far["calculated_value"]
    assert "annual_cash_flows" in near["formula"]
    assert "SUPPLY_CONTRACT_NPV_V1" not in near["formula"]


def test_persisted_json_string_economic_terms_compute_and_root_snapshot_field_is_not_required():
    result = evaluate_persisted_expected_price(
        evidence=persisted_inputs(snapshot_as_string=True),
        filter_result=filter_result(),
        as_of=EVALUATION_AS_OF,
    )

    assert result["status"] == COMPUTED
    assert result["calculated_value"] == 20_900


def test_prior_candidate_root_snapshot_expected_price_wiring_is_ignored():
    evidence = persisted_inputs()
    evidence["snapshot"] = json.dumps({"economic_terms": {}, "expected_price": valid_inputs()})

    result = evaluate_persisted_expected_price(
        evidence=evidence,
        filter_result=filter_result(),
        as_of=EVALUATION_AS_OF,
    )

    assert result["status"] == HOLD_MISSING_INPUT
    assert result["missing_fields"] == ["economic_terms.expected_price_inputs"]
    assert result["reason"] == "missing_persisted_valuation_inputs"


@pytest.mark.parametrize(
    ("branch", "unused"),
    [
        ("DCF_existing", ("normalized_metric", "peer_multiple")),
        ("peer_existing", ("normalized_fcf", "discount_rate", "periods", "terminal_growth")),
    ],
)
def test_only_selected_existing_value_branch_is_required(branch, unused):
    inputs = valid_inputs(branch)
    for name in unused:
        inputs["existing"].pop(name)

    assert evaluate(inputs)["status"] == COMPUTED


@pytest.mark.parametrize(
    ("branch", "field_name"),
    [("DCF_existing", "normalized_fcf"), ("peer_existing", "normalized_metric")],
)
def test_missing_selected_existing_branch_holds(branch, field_name):
    inputs = valid_inputs(branch)
    del inputs["existing"][field_name]

    result = evaluate(inputs)

    assert result["status"] == HOLD_MISSING_INPUT
    assert f"existing.{field_name}" in result["missing_fields"]
    assert result["calculated_value"] is None


@pytest.mark.parametrize(
    ("mutate", "missing_field"),
    [
        (lambda value: value["contract"].pop("margin"), "contract.margin"),
        (
            lambda value: value["contract"]["annual_cash_flows"][0].pop("cash_time"),
            "contract.annual_cash_flows[0].cash_time",
        ),
        (lambda value: value["balance_sheet"].pop("debt"), "balance_sheet.debt"),
        (lambda value: value.pop("diluted_shares"), "diluted_shares"),
        (lambda value: value.pop("market_baseline"), "market_baseline"),
    ],
)
def test_each_strict_required_input_holds_with_exact_field(mutate, missing_field):
    inputs = valid_inputs()
    mutate(inputs)

    result = evaluate(inputs)

    assert result["status"] == HOLD_MISSING_INPUT
    assert missing_field in result["missing_fields"]
    assert result["calculated_value"] is None
    assert "price_range" not in result


@pytest.mark.parametrize("bad", ["1", "", True, math.nan, math.inf, -math.inf])
def test_numeric_strings_booleans_and_nonfinite_values_never_calculate(bad):
    inputs = valid_inputs()
    inputs["contract"]["tax_rate"]["value"] = bad

    result = evaluate(inputs)

    assert result["status"] == HOLD_INVALID_INPUT
    assert "contract.tax_rate" in result["invalid_fields"]
    assert result["calculated_value"] is None


@pytest.mark.parametrize(
    ("mutate", "invalid_field"),
    [
        (lambda value: value["contract"]["amount"].update(currency="USD"), "contract.amount"),
        (lambda value: value["contract"]["margin"].update(issuer_id="other"), "contract.margin"),
        (lambda value: value["contract"]["tax_rate"].update(event_id="other"), "contract.tax_rate"),
        (lambda value: value["contract"]["discount_rate"].update(unit="percent"), "contract.discount_rate"),
        (
            lambda value: value["contract"]["annual_cash_flows"][0]["capex"].update(
                known_at="2026-09-07T10:00:00+09:00"
            ),
            "contract.annual_cash_flows[0].capex",
        ),
        (
            lambda value: value["balance_sheet"]["cash"].update(as_of="2026-09-06T07:30:00+09:00"),
            "balance_sheet.cash",
        ),
        (lambda value: value["market_baseline"].update(security_id="other"), "market_baseline.security_id"),
        (lambda value: value["market_baseline"].update(unit="KRW"), "market_baseline.unit"),
        (
            lambda value: value["market_baseline"].update(retrieved_at="2026-09-07T10:00:00+09:00"),
            "market_baseline.retrieved_at",
        ),
    ],
)
def test_units_identity_snapshot_basis_and_known_at_are_strict(mutate, invalid_field):
    inputs = valid_inputs()
    mutate(inputs)

    result = evaluate(inputs)

    assert result["status"] == HOLD_INVALID_INPUT
    assert invalid_field in result["invalid_fields"]


def test_canonical_evidence_identity_cannot_be_overridden_by_persisted_payload():
    raw = valid_inputs()
    raw["issuer_id"] = "other-issuer"
    result = evaluate_persisted_expected_price(
        evidence=persisted_inputs(raw),
        filter_result=filter_result(),
        as_of=EVALUATION_AS_OF,
    )

    assert result["status"] == HOLD_INVALID_INPUT
    assert "issuer_id" in result["invalid_fields"]
    assert result["issuer_id"] == ISSUER


def test_assumptions_require_explicit_scenario_approval_and_approval_reference():
    invalid = valid_inputs()
    invalid["contract"]["margin"].update(source_status="assumption")
    held = evaluate(invalid)
    assert held["status"] == HOLD_INVALID_INPUT
    assert "contract.margin" in held["invalid_fields"]

    invalid["contract"]["margin"].update(
        approved_scenarios=["BASE"], approval_ref="policy:issuer-005930:event-1:v1"
    )
    computed = evaluate(invalid)
    assert computed["status"] == COMPUTED
    assert computed["used_assumptions"] == ["contract.margin"]


def test_official_derived_value_requires_reproducible_derivation_reference():
    inputs = valid_inputs()
    inputs["contract"]["tax_rate"].update(source_status="official_derived")
    invalid = evaluate(inputs)
    assert invalid["status"] == HOLD_INVALID_INPUT
    assert "contract.tax_rate" in invalid["invalid_fields"]

    inputs["contract"]["tax_rate"]["derivation"] = "income_tax_expense / pretax_income"
    assert evaluate(inputs)["status"] == COMPUTED


@pytest.mark.parametrize("shares", [0, -1])
def test_nonpositive_diluted_share_denominator_holds_without_division(shares):
    inputs = valid_inputs()
    inputs["diluted_shares"]["value"] = shares

    result = evaluate(inputs)

    assert result["status"] == HOLD_INVALID_INPUT
    assert "diluted_shares" in result["invalid_fields"]
    assert result["calculated_value"] is None


def test_duplicate_potential_shares_and_dcf_denominator_are_invalid():
    duplicate = valid_inputs()
    duplicate["duplicate_potential_shares"] = True
    assert evaluate(duplicate)["status"] == HOLD_INVALID_INPUT

    dcf = valid_inputs("DCF_existing")
    dcf["existing"]["terminal_growth"]["value"] = 0.10
    result = evaluate(dcf)
    assert result["status"] == HOLD_INVALID_INPUT
    assert "existing.discount_rate" in result["invalid_fields"]


def test_annual_proportions_and_cash_times_must_be_complete_and_ordered():
    proportions = valid_inputs()
    proportions["contract"]["annual_cash_flows"][1]["revenue_proportion"]["value"] = 0.40
    assert "contract.annual_cash_flows" in evaluate(proportions)["invalid_fields"]

    timing = valid_inputs()
    timing["contract"]["annual_cash_flows"][1]["cash_time"]["value"] = 0.5
    result = evaluate(timing)
    assert result["status"] == HOLD_INVALID_INPUT
    assert "contract.annual_cash_flows[1].cash_time" in result["invalid_fields"]


def test_one_hold_does_not_block_computed_item_and_hold_only_aggregate_is_done():
    held = valid_inputs()
    del held["contract"]["margin"]

    mixed = evaluate_expected_prices(
        [held, valid_inputs(scenario="GOOD")],
        evaluation_as_of=EVALUATION_AS_OF,
        evidence_known_at=EVIDENCE_KNOWN_AT,
    )
    assert [item["status"] for item in mixed["results"]] == [HOLD_MISSING_INPUT, COMPUTED]
    assert mixed["status"] == "done"
    assert mixed["counts"] == {
        "target": 2,
        "computed": 1,
        "hold_missing_input": 1,
        "hold_invalid_input": 0,
        "calculation_error": 0,
    }

    hold_only = evaluate_expected_prices(
        [held, deepcopy(held)],
        evaluation_as_of=EVALUATION_AS_OF,
        evidence_known_at=EVIDENCE_KNOWN_AT,
    )
    assert hold_only["status"] == "done"
    assert hold_only["counts"]["computed"] == 0


def test_unexpected_calculation_error_is_isolated_and_aggregate_is_error(monkeypatch):
    import kr_stock_autotrader.expected_price as module

    original = module._calculate
    calls = 0

    def explode_once(inputs, values):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("boom")
        return original(inputs, values)

    monkeypatch.setattr(module, "_calculate", explode_once)
    aggregate = evaluate_expected_prices(
        [valid_inputs(), valid_inputs(scenario="GOOD")],
        evaluation_as_of=EVALUATION_AS_OF,
        evidence_known_at=EVIDENCE_KNOWN_AT,
    )

    assert [item["status"] for item in aggregate["results"]] == [CALCULATION_ERROR, COMPUTED]
    assert aggregate["status"] == "error"
    assert aggregate["results"][0]["calculated_value"] is None


@pytest.mark.parametrize(
    ("raw_value", "expected", "tick"),
    [
        (1_999.5, 2_000, 1),
        (2_002.5, 2_005, 5),
        (4_997.5, 5_000, 5),
        (5_005.0, 5_010, 10),
        (19_995.0, 20_000, 10),
        (20_025.0, 20_050, 50),
        (49_975.0, 50_000, 50),
        (50_050.0, 50_100, 100),
        (199_950.0, 200_000, 100),
        (200_250.0, 200_500, 500),
        (499_750.0, 500_000, 500),
        (500_500.0, 501_000, 1_000),
    ],
)
def test_computed_values_round_half_up_to_integer_krx_tick(monkeypatch, raw_value, expected, tick):
    import kr_stock_autotrader.expected_price as module

    monkeypatch.setattr(module, "_calculate", lambda _inputs, _values: raw_value)
    result = evaluate(valid_inputs())

    assert result["status"] == COMPUTED
    assert result["calculated_value"] == expected
    assert result["tick_size_krw"] == tick
