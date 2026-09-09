"""Deterministic expected-price evaluation from persisted, point-in-time inputs.

This module is deliberately pure: it performs no network or database access.
The 09:05 runtime passes the selected evidence and 08:00 KIS baseline in, and
receives either one integer KRX-tick value or an explicit fail-closed HOLD.
"""
from __future__ import annotations

import hashlib
import json
import math
from copy import deepcopy
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from typing import Mapping

from .domain import KST

COMPUTED = "COMPUTED"
HOLD_MISSING_INPUT = "HOLD_MISSING_INPUT"
HOLD_INVALID_INPUT = "HOLD_INVALID_INPUT"
CALCULATION_ERROR = "CALCULATION_ERROR"

INPUT_SCHEMA_VERSION = "giraffe-expected-price-input-v1"
RESULT_SCHEMA_VERSION = "giraffe-expected-price-result-v1"
BASELINE_SCHEMA_VERSION = "giraffe-premarket-baseline-v1"
FORMULA_ID = "ENTERPRISE_PLUS_EVENT_ANNUAL_FCF_V1"
FORMULA_DESCRIPTION = (
    "(selected_existing_enterprise_value + sum(annual_cash_flows discounted at "
    "cash_time) + cash - debt - senior_claims) / diluted_shares"
)
_SCENARIOS = frozenset({"BAD", "BASE", "GOOD"})
_SOURCE_STATUSES = frozenset({"official_exact", "official_derived", "assumption"})
_MISSING_SOURCE_STATUSES = frozenset({"unavailable", "not_searched"})


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=True)


def _input_hash(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _strict_timestamp(value: object) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value:
        parsed = datetime.fromisoformat(value)
    else:
        raise ValueError("invalid timestamp")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamp must include timezone")
    return parsed.astimezone(KST)


def _strict_number(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("invalid numeric value")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("invalid numeric value")
    return number


def _append_once(items: list[str], path: str) -> None:
    if path not in items:
        items.append(path)


def _identity(inputs: object, canonical_identity: Mapping[str, object] | None) -> tuple[object, object, object]:
    source = inputs if isinstance(inputs, Mapping) else {}
    canonical = canonical_identity or {}
    return (
        canonical.get("issuer_id", source.get("issuer_id")),
        canonical.get("security_id", source.get("security_id")),
        canonical.get("event_id", source.get("event_id")),
    )


def _base_result(
    inputs: object,
    *,
    status: str,
    reason: str,
    missing_fields: list[str] | None = None,
    invalid_fields: list[str] | None = None,
    used_assumptions: list[str] | None = None,
    canonical_identity: Mapping[str, object] | None = None,
) -> dict:
    source = inputs if isinstance(inputs, Mapping) else {}
    issuer_id, security_id, event_id = _identity(inputs, canonical_identity)
    baseline = source.get("market_baseline") if isinstance(source.get("market_baseline"), Mapping) else {}
    return {
        "schema_version": RESULT_SCHEMA_VERSION,
        "issuer_id": issuer_id,
        "security_id": security_id,
        "event_id": event_id,
        "scenario": source.get("scenario"),
        "valuation_branch": source.get("valuation_branch"),
        "status": status,
        "missing_fields": sorted(missing_fields or []),
        "invalid_fields": sorted(invalid_fields or []),
        "used_assumptions": sorted(used_assumptions or []),
        "calculated_value": None,
        "calculated_value_unit": "KRW/share",
        "reason": reason,
        "formula_id": source.get("formula_id"),
        "formula": FORMULA_DESCRIPTION,
        "input_sha256": _input_hash(inputs),
        "baseline_price_krw": baseline.get("price_krw"),
        "baseline_known_at": baseline.get("retrieved_at"),
        "tick_size_krw": None,
    }


class _Validator:
    def __init__(
        self,
        inputs: Mapping[str, object],
        *,
        evaluation_as_of: object,
        evidence_known_at: object | None,
        canonical_identity: Mapping[str, object] | None,
    ) -> None:
        self.inputs = inputs
        self.missing: list[str] = []
        self.invalid: list[str] = []
        self.assumptions: list[str] = []
        self.values: dict[str, object] = {}
        self.canonical_identity = canonical_identity or {}
        try:
            self.evaluation_at = _strict_timestamp(evaluation_as_of)
        except (TypeError, ValueError):
            self.evaluation_at = None
            self.invalid.append("evaluation_as_of")
        try:
            self.evidence_at = _strict_timestamp(evidence_known_at) if evidence_known_at is not None else None
        except (TypeError, ValueError):
            self.evidence_at = None
            self.invalid.append("evidence_known_at")
        self.package_as_of: datetime | None = None
        self.package_known_at: datetime | None = None
        self.issuer_id = inputs.get("issuer_id")
        self.event_id = inputs.get("event_id")
        self.scenario = inputs.get("scenario")

    def required(self, container: object, key: str, path: str) -> object | None:
        if not isinstance(container, Mapping):
            _append_once(self.invalid, path.rsplit(".", 1)[0] if "." in path else path)
            return None
        if key not in container:
            _append_once(self.missing, path)
            return None
        return container[key]

    def envelope(
        self,
        container: object,
        key: str,
        path: str,
        *,
        unit: str,
        currency: str | None,
        minimum: float | None = None,
        maximum: float | None = None,
        strictly_positive: bool = False,
        integer: bool = False,
    ) -> float | None:
        envelope = self.required(container, key, path)
        if envelope is None:
            return None
        if not isinstance(envelope, Mapping):
            _append_once(self.invalid, path)
            return None
        status = envelope.get("source_status")
        if status in _MISSING_SOURCE_STATUSES:
            _append_once(self.missing, path)
            return None
        if status not in _SOURCE_STATUSES:
            _append_once(self.invalid, path)
        if envelope.get("unit") != unit or envelope.get("currency") != currency:
            _append_once(self.invalid, path)
        if envelope.get("issuer_id") != self.issuer_id or envelope.get("event_id") != self.event_id:
            _append_once(self.invalid, path)
        if not isinstance(envelope.get("source_ref"), str) or not envelope.get("source_ref", "").strip():
            _append_once(self.invalid, path)
        try:
            field_as_of = _strict_timestamp(envelope.get("as_of"))
            field_known_at = _strict_timestamp(envelope.get("known_at"))
            if self.package_as_of is None or field_as_of != self.package_as_of:
                _append_once(self.invalid, path)
            if self.package_known_at is None or field_known_at > self.package_known_at:
                _append_once(self.invalid, path)
        except (TypeError, ValueError):
            _append_once(self.invalid, path)
        if status == "official_derived" and (
            not isinstance(envelope.get("derivation"), str) or not envelope.get("derivation", "").strip()
        ):
            _append_once(self.invalid, path)
        if status == "assumption":
            approved = envelope.get("approved_scenarios")
            approval_ref = envelope.get("approval_ref")
            if (
                not isinstance(approved, list)
                or self.scenario not in approved
                or not isinstance(approval_ref, str)
                or not approval_ref.strip()
            ):
                _append_once(self.invalid, path)
            else:
                _append_once(self.assumptions, path)
        try:
            number = _strict_number(envelope.get("value"))
            if strictly_positive and number <= 0:
                raise ValueError("nonpositive")
            if minimum is not None and number < minimum:
                raise ValueError("below range")
            if maximum is not None and number > maximum:
                raise ValueError("above range")
            if integer and not number.is_integer():
                raise ValueError("noninteger")
            self.values[path] = number
            return number
        except (TypeError, ValueError):
            _append_once(self.invalid, path)
            return None

    def validate_top_level(self) -> None:
        required_literals = {
            "schema_version": INPUT_SCHEMA_VERSION,
            "formula_id": FORMULA_ID,
        }
        for key, expected in required_literals.items():
            value = self.required(self.inputs, key, key)
            if value is not None and value != expected:
                _append_once(self.invalid, key)
        for key in ("issuer_id", "security_id", "event_id"):
            value = self.required(self.inputs, key, key)
            if value is not None and (not isinstance(value, str) or not value.strip()):
                _append_once(self.invalid, key)
        if (
            isinstance(self.inputs.get("issuer_id"), str)
            and isinstance(self.inputs.get("security_id"), str)
            and self.inputs["issuer_id"] != self.inputs["security_id"]
        ):
            _append_once(self.invalid, "security_id")
        for key in ("issuer_id", "security_id", "event_id"):
            expected = self.canonical_identity.get(key)
            if expected is not None and self.inputs.get(key) != expected:
                _append_once(self.invalid, key)
        scenario = self.required(self.inputs, "scenario", "scenario")
        if scenario is not None and scenario not in _SCENARIOS:
            _append_once(self.invalid, "scenario")
        branch = self.required(self.inputs, "valuation_branch", "valuation_branch")
        if branch is not None and branch not in {"DCF_existing", "peer_existing"}:
            _append_once(self.invalid, "valuation_branch")
        package_as_of = self.required(self.inputs, "as_of", "as_of")
        package_known_at = self.required(self.inputs, "known_at", "known_at")
        try:
            self.package_as_of = _strict_timestamp(package_as_of)
        except (TypeError, ValueError):
            _append_once(self.invalid, "as_of")
        try:
            self.package_known_at = _strict_timestamp(package_known_at)
        except (TypeError, ValueError):
            _append_once(self.invalid, "known_at")
        if self.package_as_of is not None and self.package_known_at is not None:
            if self.package_as_of > self.package_known_at:
                _append_once(self.invalid, "known_at")
            if self.evidence_at is not None and self.evidence_at > self.package_known_at:
                _append_once(self.invalid, "known_at")
        if self.package_known_at is not None and self.evaluation_at is not None and self.package_known_at > self.evaluation_at:
            _append_once(self.invalid, "known_at")

    def validate_existing(self) -> None:
        existing = self.required(self.inputs, "existing", "existing")
        branch = self.inputs.get("valuation_branch")
        if branch == "DCF_existing":
            self.envelope(existing, "normalized_fcf", "existing.normalized_fcf", unit="KRW", currency="KRW", strictly_positive=True)
            discount = self.envelope(existing, "discount_rate", "existing.discount_rate", unit="ratio", currency=None, minimum=0.0, strictly_positive=True)
            self.envelope(existing, "periods", "existing.periods", unit="years", currency=None, strictly_positive=True, integer=True)
            growth = self.envelope(existing, "terminal_growth", "existing.terminal_growth", unit="ratio", currency=None, minimum=-0.999999)
            if discount is not None and growth is not None and discount <= growth:
                _append_once(self.invalid, "existing.discount_rate")
        elif branch == "peer_existing":
            self.envelope(existing, "normalized_metric", "existing.normalized_metric", unit="KRW", currency="KRW", strictly_positive=True)
            self.envelope(existing, "peer_multiple", "existing.peer_multiple", unit="multiple", currency=None, strictly_positive=True)

    def validate_contract(self) -> None:
        contract = self.required(self.inputs, "contract", "contract")
        self.envelope(contract, "amount", "contract.amount", unit="KRW", currency="KRW", strictly_positive=True)
        self.envelope(contract, "margin", "contract.margin", unit="ratio", currency=None, minimum=0.0, maximum=1.0)
        self.envelope(contract, "tax_rate", "contract.tax_rate", unit="ratio", currency=None, minimum=0.0, maximum=1.0)
        self.envelope(contract, "fulfillment_probability", "contract.fulfillment_probability", unit="ratio", currency=None, minimum=0.0, maximum=1.0)
        self.envelope(contract, "discount_rate", "contract.discount_rate", unit="ratio", currency=None, minimum=0.0)
        rows = self.required(contract, "annual_cash_flows", "contract.annual_cash_flows")
        if rows is None:
            return
        if not isinstance(rows, list) or not rows:
            _append_once(self.invalid, "contract.annual_cash_flows")
            return
        normalized_rows: list[dict] = []
        previous_year = 0
        previous_cash_time = 0.0
        for index, row in enumerate(rows):
            prefix = f"contract.annual_cash_flows[{index}]"
            if not isinstance(row, Mapping):
                _append_once(self.invalid, prefix)
                continue
            year = self.required(row, "year", f"{prefix}.year")
            if isinstance(year, bool) or not isinstance(year, int) or year <= previous_year:
                _append_once(self.invalid, f"{prefix}.year")
            else:
                previous_year = year
            proportion = self.envelope(row, "revenue_proportion", f"{prefix}.revenue_proportion", unit="ratio", currency=None, minimum=0.0, maximum=1.0)
            cash_time = self.envelope(row, "cash_time", f"{prefix}.cash_time", unit="years", currency=None, strictly_positive=True)
            non_cash = self.envelope(row, "non_cash_expense", f"{prefix}.non_cash_expense", unit="KRW", currency="KRW", minimum=0.0)
            working_capital = self.envelope(row, "working_capital", f"{prefix}.working_capital", unit="KRW", currency="KRW", minimum=0.0)
            capex = self.envelope(row, "capex", f"{prefix}.capex", unit="KRW", currency="KRW", minimum=0.0)
            if cash_time is not None:
                if cash_time <= previous_cash_time:
                    _append_once(self.invalid, f"{prefix}.cash_time")
                previous_cash_time = max(previous_cash_time, cash_time)
            normalized_rows.append({
                "year": year,
                "revenue_proportion": proportion,
                "cash_time": cash_time,
                "non_cash_expense": non_cash,
                "working_capital": working_capital,
                "capex": capex,
            })
        proportions = [row["revenue_proportion"] for row in normalized_rows]
        if all(value is not None for value in proportions) and not math.isclose(sum(proportions), 1.0, rel_tol=0.0, abs_tol=1e-9):
            _append_once(self.invalid, "contract.annual_cash_flows")
        self.values["annual_cash_flows"] = normalized_rows

    def validate_equity_bridge(self) -> None:
        balance = self.required(self.inputs, "balance_sheet", "balance_sheet")
        for name in ("cash", "debt", "senior_claims"):
            self.envelope(balance, name, f"balance_sheet.{name}", unit="KRW", currency="KRW", minimum=0.0)
        self.envelope(self.inputs, "diluted_shares", "diluted_shares", unit="shares", currency=None, strictly_positive=True)
        duplicate = self.required(self.inputs, "duplicate_potential_shares", "duplicate_potential_shares")
        if duplicate is not None and duplicate is not False:
            _append_once(self.invalid, "duplicate_potential_shares")

    def validate_baseline(self) -> None:
        baseline = self.required(self.inputs, "market_baseline", "market_baseline")
        if baseline is None:
            return
        if not isinstance(baseline, Mapping):
            _append_once(self.invalid, "market_baseline")
            return
        expected_literals = {
            "schema_version": BASELINE_SCHEMA_VERSION,
            "security_id": self.inputs.get("security_id"),
            "source": "KIS",
            "source_field": "previous_close_krw",
            "unit": "KRW/share",
        }
        for key, expected in expected_literals.items():
            if key not in baseline:
                _append_once(self.missing, f"market_baseline.{key}")
            elif baseline.get(key) != expected:
                _append_once(self.invalid, f"market_baseline.{key}")
        if "price_krw" not in baseline:
            _append_once(self.missing, "market_baseline.price_krw")
        else:
            try:
                price = _strict_number(baseline.get("price_krw"))
                if price <= 0:
                    raise ValueError("nonpositive")
                self.values["baseline_price_krw"] = price
            except (TypeError, ValueError):
                _append_once(self.invalid, "market_baseline.price_krw")
        session_date = baseline.get("session_date")
        try:
            session_day = datetime.strptime(session_date, "%Y-%m-%d").date()
            if self.evaluation_at is not None and session_day >= self.evaluation_at.date():
                _append_once(self.invalid, "market_baseline.session_date")
        except (TypeError, ValueError):
            _append_once(self.invalid, "market_baseline.session_date")
        try:
            price_known = _strict_timestamp(baseline.get("price_known_at"))
            retrieved = _strict_timestamp(baseline.get("retrieved_at"))
            if price_known > retrieved:
                _append_once(self.invalid, "market_baseline.price_known_at")
            if self.evaluation_at is None or retrieved > self.evaluation_at:
                _append_once(self.invalid, "market_baseline.retrieved_at")
        except (TypeError, ValueError):
            if not isinstance(baseline.get("price_known_at"), str):
                _append_once(self.invalid, "market_baseline.price_known_at")
            else:
                try:
                    _strict_timestamp(baseline.get("price_known_at"))
                except (TypeError, ValueError):
                    _append_once(self.invalid, "market_baseline.price_known_at")
            try:
                _strict_timestamp(baseline.get("retrieved_at"))
            except (TypeError, ValueError):
                _append_once(self.invalid, "market_baseline.retrieved_at")

    def run(self) -> None:
        self.validate_top_level()
        self.validate_existing()
        self.validate_contract()
        self.validate_equity_bridge()
        self.validate_baseline()


def _calculate(inputs: Mapping[str, object], values: Mapping[str, object]) -> float:
    branch = inputs["valuation_branch"]
    if branch == "DCF_existing":
        fcf = values["existing.normalized_fcf"]
        discount = values["existing.discount_rate"]
        periods = int(values["existing.periods"])
        growth = values["existing.terminal_growth"]
        existing_value = sum(fcf / ((1.0 + discount) ** period) for period in range(1, periods + 1))
        terminal_value = fcf * (1.0 + growth) / (discount - growth)
        existing_value += terminal_value / ((1.0 + discount) ** periods)
    else:
        existing_value = values["existing.normalized_metric"] * values["existing.peer_multiple"]

    amount = values["contract.amount"]
    margin = values["contract.margin"]
    tax_rate = values["contract.tax_rate"]
    probability = values["contract.fulfillment_probability"]
    event_discount = values["contract.discount_rate"]
    event_value = 0.0
    for row in values["annual_cash_flows"]:
        revenue = amount * row["revenue_proportion"]
        annual_fcf = (
            revenue * margin * (1.0 - tax_rate)
            + row["non_cash_expense"]
            - row["working_capital"]
            - row["capex"]
        )
        event_value += annual_fcf * probability / ((1.0 + event_discount) ** row["cash_time"])

    equity_value = (
        existing_value
        + event_value
        + values["balance_sheet.cash"]
        - values["balance_sheet.debt"]
        - values["balance_sheet.senior_claims"]
    )
    result = equity_value / values["diluted_shares"]
    if not math.isfinite(result) or result <= 0:
        raise ArithmeticError("nonpositive or nonfinite expected price")
    return result


def _krx_tick(price: float) -> int:
    if price < 2_000:
        return 1
    if price < 5_000:
        return 5
    if price < 20_000:
        return 10
    if price < 50_000:
        return 50
    if price < 200_000:
        return 100
    if price < 500_000:
        return 500
    return 1_000


def _round_to_krx_tick(price: float) -> tuple[int, int]:
    tick = _krx_tick(price)
    rounded = int((Decimal(str(price)) / Decimal(tick)).quantize(Decimal("1"), rounding=ROUND_HALF_UP) * tick)
    if rounded <= 0:
        raise ArithmeticError("rounded expected price is nonpositive")
    return rounded, tick


def evaluate_expected_price(
    inputs: object,
    *,
    evaluation_as_of: object,
    evidence_known_at: object | None = None,
    _canonical_identity: Mapping[str, object] | None = None,
) -> dict:
    """Validate and evaluate one already-persisted valuation package."""
    if not isinstance(inputs, Mapping):
        return _base_result(
            inputs,
            status=HOLD_INVALID_INPUT,
            reason="invalid_required_inputs",
            invalid_fields=["expected_price_inputs"],
            canonical_identity=_canonical_identity,
        )
    validator = _Validator(
        inputs,
        evaluation_as_of=evaluation_as_of,
        evidence_known_at=evidence_known_at,
        canonical_identity=_canonical_identity,
    )
    validator.run()
    if validator.missing:
        return _base_result(
            inputs,
            status=HOLD_MISSING_INPUT,
            reason="missing_required_inputs",
            missing_fields=validator.missing,
            invalid_fields=validator.invalid,
            used_assumptions=validator.assumptions,
            canonical_identity=_canonical_identity,
        )
    if validator.invalid:
        return _base_result(
            inputs,
            status=HOLD_INVALID_INPUT,
            reason="invalid_required_inputs",
            invalid_fields=validator.invalid,
            used_assumptions=validator.assumptions,
            canonical_identity=_canonical_identity,
        )
    try:
        raw_value = _calculate(inputs, validator.values)
        calculated_value, tick = _round_to_krx_tick(raw_value)
    except Exception:
        return _base_result(
            inputs,
            status=CALCULATION_ERROR,
            reason="calculation_error",
            used_assumptions=validator.assumptions,
            canonical_identity=_canonical_identity,
        )
    result = _base_result(
        inputs,
        status=COMPUTED,
        reason="calculated_from_persisted_inputs",
        used_assumptions=validator.assumptions,
        canonical_identity=_canonical_identity,
    )
    result["calculated_value"] = calculated_value
    result["tick_size_krw"] = tick
    return result


def _mapping_value(value: object, key: str, default: object = None) -> object:
    if isinstance(value, Mapping):
        return value.get(key, default)
    try:
        return value[key]  # type: ignore[index]
    except (KeyError, TypeError, IndexError):
        return default


def evaluate_persisted_expected_price(*, evidence: object, filter_result: object, as_of: object) -> dict:
    """Evaluate only the nested 07:00 evidence package plus persisted 08:00 baseline."""
    symbol = _mapping_value(evidence, "symbol")
    event_id = _mapping_value(evidence, "dedupe_key")
    if event_id is None:
        evidence_id = _mapping_value(evidence, "id")
        event_id = str(evidence_id) if evidence_id is not None else None
    canonical_identity = {"issuer_id": symbol, "security_id": symbol, "event_id": event_id}
    snapshot = _mapping_value(evidence, "snapshot")
    if isinstance(snapshot, str):
        try:
            snapshot = json.loads(snapshot)
        except (TypeError, ValueError):
            return _base_result(
                {},
                status=HOLD_INVALID_INPUT,
                reason="invalid_persisted_evidence_snapshot",
                invalid_fields=["snapshot"],
                canonical_identity=canonical_identity,
            )
    if not isinstance(snapshot, Mapping):
        return _base_result(
            {},
            status=HOLD_INVALID_INPUT,
            reason="invalid_persisted_evidence_snapshot",
            invalid_fields=["snapshot"],
            canonical_identity=canonical_identity,
        )
    economic_terms = snapshot.get("economic_terms")
    expected_inputs = economic_terms.get("expected_price_inputs") if isinstance(economic_terms, Mapping) else None
    if expected_inputs is None:
        return _base_result(
            {},
            status=HOLD_MISSING_INPUT,
            reason="missing_persisted_valuation_inputs",
            missing_fields=["economic_terms.expected_price_inputs"],
            canonical_identity=canonical_identity,
        )
    if not isinstance(expected_inputs, Mapping):
        return _base_result(
            expected_inputs,
            status=HOLD_INVALID_INPUT,
            reason="invalid_persisted_valuation_inputs",
            invalid_fields=["economic_terms.expected_price_inputs"],
            canonical_identity=canonical_identity,
        )
    raw_inputs = deepcopy(dict(expected_inputs))
    raw_filter_inputs = _mapping_value(filter_result, "raw_inputs", {})
    baseline = raw_filter_inputs.get("expected_price_baseline") if isinstance(raw_filter_inputs, Mapping) else None
    if baseline is not None:
        raw_inputs["market_baseline"] = deepcopy(baseline)
    return evaluate_expected_price(
        raw_inputs,
        evaluation_as_of=as_of,
        evidence_known_at=_mapping_value(evidence, "known_at"),
        _canonical_identity=canonical_identity,
    )


def evaluate_expected_prices(
    items: list[object],
    *,
    evaluation_as_of: object,
    evidence_known_at: object | None = None,
) -> dict:
    """Evaluate independent items and summarize terminal outcomes."""
    results = [
        evaluate_expected_price(
            item,
            evaluation_as_of=evaluation_as_of,
            evidence_known_at=evidence_known_at,
        )
        for item in items
    ]
    counts = {
        "target": len(results),
        "computed": sum(item["status"] == COMPUTED for item in results),
        "hold_missing_input": sum(item["status"] == HOLD_MISSING_INPUT for item in results),
        "hold_invalid_input": sum(item["status"] == HOLD_INVALID_INPUT for item in results),
        "calculation_error": sum(item["status"] == CALCULATION_ERROR for item in results),
    }
    return {"status": "error" if counts["calculation_error"] else "done", "counts": counts, "results": results}
