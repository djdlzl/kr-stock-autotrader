"""Hybrid second-stage recommendation engine.

This module keeps the policy, outcome ledger, calibration snapshots, and final
evaluation append-only and server-owned. It never creates trading artifacts.
"""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .decision_cards import canon, now
from .domain import parse_kst
from .event_scenarios import detail_by_id
from .intraday_market_context import latest_market_context_for_card


HYBRID_POLICY_IDENTITY = "giraffe-hybrid-good-base-bad-v1"
HYBRID_POLICY_VERSION = 1
HYBRID_POLICY_HASH = "policy-hash-unset"
HYBRID_MIN_OVERALL_SAMPLES = 30
HYBRID_MIN_OOS_SAMPLES = 20
HYBRID_GOOD_LCB_THRESHOLD = Decimal("0.2")
HYBRID_TRANSACTION_COST_BPS = 8
HYBRID_ROUND_TRIP_COST_BPS = 16
HYBRID_MAX_SPREAD_PCT = Decimal("0.05")
HYBRID_MIN_TOP_BOOK_QTY = Decimal("1")

DERIVED_FIELDS = {
    "policy_hash",
    "policy_probability",
    "good_probability",
    "good_lower_bound",
    "lower_bound",
    "final_state",
    "recommendation",
}


class HybridWindowIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start: str
    end: str

    @field_validator("start", "end")
    @classmethod
    def _kst(cls, value: str) -> str:
        return parse_kst(value).isoformat()


class HybridOutcomeBarIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbol: str
    exchange_at: str
    known_at: str
    open_krw: float
    high_krw: float
    low_krw: float
    close_krw: float
    volume: float

    @field_validator("symbol")
    @classmethod
    def _symbol(cls, value: str) -> str:
        if not isinstance(value, str) or len(value) != 6 or not value.isdigit():
            raise ValueError("symbol must be a six-digit code")
        return value

    @field_validator("exchange_at", "known_at")
    @classmethod
    def _timestamp(cls, value: str) -> str:
        return parse_kst(value).isoformat()

    @model_validator(mode="after")
    def _ohlc(self):
        values = [self.open_krw, self.high_krw, self.low_krw, self.close_krw, self.volume]
        if any(isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) for value in values):
            raise ValueError("invalid hybrid bar numbers")
        if min(self.open_krw, self.high_krw, self.low_krw, self.close_krw, self.volume) <= 0:
            raise ValueError("hybrid bars must be positive")
        if self.low_krw > self.high_krw:
            raise ValueError("low above high")
        if not (self.low_krw <= self.open_krw <= self.high_krw):
            raise ValueError("open outside range")
        if not (self.low_krw <= self.close_krw <= self.high_krw):
            raise ValueError("close outside range")
        return self


class HybridOutcomeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    idempotency_key: str = Field(min_length=1, max_length=200)
    observation_cutoff_at: str
    bars: list[HybridOutcomeBarIn]

    @field_validator("observation_cutoff_at")
    @classmethod
    def _cutoff(cls, value: str) -> str:
        return parse_kst(value).isoformat()

    @model_validator(mode="after")
    def _non_empty(self):
        if not self.bars:
            raise ValueError("bars must not be empty")
        return self


class HybridCalibrationPlanIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    idempotency_key: str = Field(min_length=1, max_length=128)
    scenario_set_id: int
    policy_identity: str
    policy_version: int
    holdout_key: str = Field(min_length=1, max_length=128)
    is_window: HybridWindowIn
    oos_window: HybridWindowIn
    cutoff_at: str

    @field_validator("policy_identity")
    @classmethod
    def _identity(cls, value: str) -> str:
        if value != HYBRID_POLICY_IDENTITY:
            raise ValueError("unsupported policy identity")
        return value

    @field_validator("policy_version")
    @classmethod
    def _version(cls, value: int) -> int:
        if value != HYBRID_POLICY_VERSION:
            raise ValueError("unsupported policy version")
        return value

    @field_validator("cutoff_at")
    @classmethod
    def _cutoff(cls, value: str) -> str:
        return parse_kst(value).isoformat()

    @model_validator(mode="after")
    def _windows(self):
        if parse_kst(self.is_window.end) > parse_kst(self.oos_window.start):
            raise ValueError("chronological IS/OOS windows required")
        if parse_kst(self.oos_window.end) > parse_kst(self.cutoff_at):
            raise ValueError("cutoff must cover OOS window")
        return self


class HybridCalibrationSnapshotIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plan_id: int
    idempotency_key: str = Field(min_length=1, max_length=128)


class HybridEvaluationIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    policy_identity: str
    policy_version: int
    calibration_snapshot_id: int
    recommendation_only: bool = True

    @field_validator("policy_identity")
    @classmethod
    def _identity(cls, value: str) -> str:
        if value != HYBRID_POLICY_IDENTITY:
            raise ValueError("unsupported policy identity")
        return value

    @field_validator("policy_version")
    @classmethod
    def _version(cls, value: int) -> int:
        if value != HYBRID_POLICY_VERSION:
            raise ValueError("unsupported policy version")
        return value

    @model_validator(mode="after")
    def _recommendation_only(self):
        if self.recommendation_only is not True:
            raise ValueError("recommendation_only must be true")
        return self


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _decimal(value: float | int) -> Decimal:
    return Decimal(str(value))


def _policy_spec() -> dict[str, Any]:
    return {
        "identity": HYBRID_POLICY_IDENTITY,
        "version": HYBRID_POLICY_VERSION,
        "evaluation_horizon": {"type": "deadline_contract", "deadline": "observation_cutoff_at"},
        "transaction_cost_bps": HYBRID_TRANSACTION_COST_BPS,
        "round_trip_cost_bps": HYBRID_ROUND_TRIP_COST_BPS,
        "good_lower_bound_threshold": float(HYBRID_GOOD_LCB_THRESHOLD),
        "minimum_samples": {
            "overall": HYBRID_MIN_OVERALL_SAMPLES,
            "oos": HYBRID_MIN_OOS_SAMPLES,
        },
        "market_context_requirements": {
            "status": "VERIFIED",
            "max_spread_pct": float(HYBRID_MAX_SPREAD_PCT),
            "min_top_of_book_qty": float(HYBRID_MIN_TOP_BOOK_QTY),
            "same_time_baseline_status": "READY",
            "previous_close_gap_status": "VERIFIED",
        },
        "sector_gate": {"status": "UNAVAILABLE", "reason": "not_part_of_policy_v1"},
        "price_guardrail": "entry price must remain inside the GOOD band",
        "same_bar_ambiguity_policy": "BAD",
    }


def ensure_policy(db: sqlite3.Connection) -> dict[str, Any]:
    spec = _policy_spec()
    spec_json = canon(spec)
    policy_hash = _sha256(spec_json)
    row = db.execute(
        "SELECT * FROM hybrid_policy_specs WHERE policy_identity=? AND policy_version=?",
        (HYBRID_POLICY_IDENTITY, HYBRID_POLICY_VERSION),
    ).fetchone()
    if row is None:
        row = db.execute(
            """INSERT INTO hybrid_policy_specs(
              policy_identity, policy_version, policy_hash, policy_json, created_at
            ) VALUES(?,?,?,?,?) RETURNING *""",
            (HYBRID_POLICY_IDENTITY, HYBRID_POLICY_VERSION, policy_hash, spec_json, now()),
        ).fetchone()
        db.commit()
    elif row["policy_hash"] != policy_hash:
        raise HTTPException(409, "policy hash drift detected")
    out = dict(row)
    out["policy"] = json.loads(out["policy_json"])
    return out


def _policy_detail(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    out = dict(row)
    out["policy"] = json.loads(out["policy_json"])
    return out


def _snapshot_detail(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    out = dict(row)
    out["snapshot"] = json.loads(out["snapshot_json"])
    return out


def _evaluation_detail(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    out = dict(row)
    out["evaluation"] = json.loads(out["evaluation_json"])
    return out


def _cohort_key(scenario: dict[str, Any]) -> str:
    scenario_kind = scenario.get("scenario_kind") or "QUANTITATIVE"
    return canon(
        {
            "event_type": scenario["event_type"],
            "profile_id": scenario["profile_id"],
            "profile_version": scenario["profile_version"],
            "scenario_kind": scenario_kind,
        }
    )


def _scenario_baseline_price(scenario: dict[str, Any]) -> Decimal:
    baseline = scenario["scenario_set"]["baseline"]
    return _decimal(baseline["price_krw"])


def _band_for_label(scenario: dict[str, Any], label: str) -> dict[str, Decimal]:
    item = next(item for item in scenario["scenarios"] if item["label"] == label)
    band = item["per_share_value_range_krw"]
    return {"low": _decimal(band["low"]), "high": _decimal(band["high"])}


def _label_for_price(price: Decimal, scenario: dict[str, Any]) -> str:
    for label in ("GOOD", "BASE", "BAD"):
        band = _band_for_label(scenario, label)
        if band["low"] <= price <= band["high"]:
            return label
    return "BAD"


def _net_return_bps(entry_price: Decimal, exit_price: Decimal) -> Decimal:
    if entry_price <= 0:
        return Decimal("0")
    return ((exit_price - entry_price) / entry_price * Decimal(10000)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _pricing_case(
    *,
    entry_price: Decimal,
    gross_price: Decimal,
    net_price: Decimal,
    gross_label: str,
    net_label: str,
    costs_bps: int,
) -> dict[str, Any]:
    return {
        "entry_price_krw": float(entry_price),
        "gross_exit_price_krw": float(gross_price),
        "net_exit_price_krw": float(net_price),
        "gross_return_bps": float(_net_return_bps(entry_price, gross_price)),
        "net_return_bps": float(_net_return_bps(entry_price, net_price)),
        "gross_label": gross_label,
        "net_label": net_label,
        "round_trip_cost_bps": costs_bps,
        "formula": "net_exit_price = gross_exit_price - round_trip_cost_krw; label = band(net_exit_price)",
    }


def _same_bar_touch(row: dict[str, Any], band: dict[str, Decimal]) -> bool:
    low = _decimal(row["low_krw"])
    high = _decimal(row["high_krw"])
    return not (high < band["low"] or low > band["high"])


def _realize_case(rows: list[dict[str, Any]], scenario: dict[str, Any]) -> tuple[str, str, dict[str, Any]]:
    entry_price = _scenario_baseline_price(scenario)
    round_trip_cost = (entry_price * Decimal(HYBRID_ROUND_TRIP_COST_BPS) / Decimal(10000)).quantize(Decimal("0.00000001"), rounding=ROUND_HALF_UP)
    good_band = _band_for_label(scenario, "GOOD")
    base_band = _band_for_label(scenario, "BASE")
    bad_band = _band_for_label(scenario, "BAD")
    for row in rows:
        low = _decimal(row["low_krw"])
        high = _decimal(row["high_krw"])
        close = _decimal(row["close_krw"])
        good_touch = _same_bar_touch(row, good_band)
        base_touch = _same_bar_touch(row, base_band)
        bad_touch = _same_bar_touch(row, bad_band)
        if good_touch and bad_touch:
            gross_label = "BAD"
            gross_price = low
            gross_reason = "same_bar_ambiguity_closed_bad"
        elif bad_touch:
            gross_label = "BAD"
            gross_price = low
            gross_reason = "first_touch_bad"
        elif good_touch:
            gross_label = "GOOD"
            gross_price = high
            gross_reason = "first_touch_good"
        elif base_touch:
            gross_label = "BASE"
            gross_price = close
            gross_reason = "first_touch_base"
        else:
            continue
        net_price = gross_price - round_trip_cost
        net_label = _label_for_price(net_price, scenario)
        realized_label = "GOOD" if net_label == "GOOD" else ("BASE" if net_label == "BASE" else "BAD")
        if gross_label == "BASE" and net_label == "BASE":
            realized_reason = "base_expiry_no_touch" if gross_reason == "first_touch_base" else "base_expiry_no_touch"
        elif gross_label == net_label:
            realized_reason = gross_reason
        else:
            realized_reason = f"cost_adjusted_{gross_label.lower()}_to_{net_label.lower()}"
        pricing = _pricing_case(
            entry_price=entry_price,
            gross_price=gross_price,
            net_price=net_price,
            gross_label=gross_label,
            net_label=net_label,
            costs_bps=HYBRID_ROUND_TRIP_COST_BPS,
        )
        return realized_label, realized_reason, pricing
    pricing = _pricing_case(
        entry_price=entry_price,
        gross_price=entry_price,
        net_price=entry_price,
        gross_label="BASE",
        net_label="BASE",
        costs_bps=HYBRID_ROUND_TRIP_COST_BPS,
    )
    return "BASE", "base_expiry_no_touch", pricing


def _validate_outcome_rows(rows: list[HybridOutcomeBarIn], *, frozen_at: datetime, cutoff_at: datetime, symbol: str) -> list[dict[str, Any]]:
    validated: list[dict[str, Any]] = []
    previous_exchange: datetime | None = None
    previous_known: datetime | None = None
    seen_exchange: set[str] = set()
    for row in rows:
        exchange_at = parse_kst(row.exchange_at)
        known_at = parse_kst(row.known_at)
        if row.symbol != symbol:
            raise HTTPException(422, "symbol mismatch")
        if known_at <= frozen_at or known_at > cutoff_at:
            raise HTTPException(422, "outcome known_at outside observation window")
        if exchange_at > known_at:
            raise HTTPException(422, "outcome exchange_at must be at or before known_at")
        if previous_exchange is not None and exchange_at <= previous_exchange:
            raise HTTPException(422, "outcome rows must be strictly ordered")
        if previous_known is not None and known_at <= previous_known:
            raise HTTPException(422, "outcome rows must be strictly ordered")
        if row.exchange_at in seen_exchange:
            raise HTTPException(422, "duplicate outcome row")
        seen_exchange.add(row.exchange_at)
        validated.append(
            {
                "symbol": row.symbol,
                "exchange_at": exchange_at.isoformat(),
                "known_at": known_at.isoformat(),
                "open_krw": float(row.open_krw),
                "high_krw": float(row.high_krw),
                "low_krw": float(row.low_krw),
                "close_krw": float(row.close_krw),
                "volume": float(row.volume),
            }
        )
        previous_exchange = exchange_at
        previous_known = known_at
    return validated


def _band(row: dict[str, Any], scenario: dict[str, Any]) -> bool:
    band = scenario["per_share_value_range_krw"]
    return not (row["high_krw"] < band["low"] or row["low_krw"] > band["high"])


def _realize_outcome(rows: list[dict[str, Any]], scenario: dict[str, Any]) -> tuple[str, str]:
    bad = next(item for item in scenario["scenarios"] if item["label"] == "BAD")
    good = next(item for item in scenario["scenarios"] if item["label"] == "GOOD")
    bad_band = bad["per_share_value_range_krw"]
    good_band = good["per_share_value_range_krw"]
    for row in rows:
        good_touch = not (row["high_krw"] < good_band["low"] or row["low_krw"] > good_band["high"])
        bad_touch = not (row["high_krw"] < bad_band["low"] or row["low_krw"] > bad_band["high"])
        if good_touch and bad_touch:
            return "BAD", "same_bar_ambiguity_closed_bad"
        if bad_touch:
            return "BAD", "first_touch_bad"
        if good_touch:
            return "GOOD", "first_touch_good"
    return "BASE", "base_expiry_no_touch"


def record_outcome(db: sqlite3.Connection, scenario_identity: str, data: dict[str, Any]) -> dict[str, Any]:
    if any(key in data for key in DERIVED_FIELDS):
        raise HTTPException(422, "derived fields are server-owned")
    if not scenario_identity:
        raise HTTPException(404, "scenario set not found")
    scenario_row = db.execute("SELECT id FROM event_scenario_sets WHERE event_identity=? ORDER BY id DESC LIMIT 1", (scenario_identity,)).fetchone()
    if scenario_row is None:
        raise HTTPException(404, "scenario set not found")
    scenario = detail_by_id(db, scenario_row["id"])
    policy = ensure_policy(db)
    try:
        payload = HybridOutcomeIn.model_validate(data)
        cutoff_at = parse_kst(payload.observation_cutoff_at)
    except (KeyError, TypeError, ValueError):
        raise HTTPException(422, "observation_cutoff_at must be KST ISO-8601")
    except Exception as exc:
        if isinstance(exc, HTTPException):
            raise
        raise HTTPException(422, "invalid hybrid outcome payload") from exc
    frozen_at = parse_kst(scenario["frozen_at"])
    if cutoff_at <= frozen_at:
        raise HTTPException(422, "observation cutoff must follow scenario freeze")
    bars = payload.bars
    validated_bars = _validate_outcome_rows(bars, frozen_at=frozen_at, cutoff_at=cutoff_at, symbol=scenario["symbol"])
    payload = canon({"observation_cutoff_at": cutoff_at.isoformat(), "bars": validated_bars})
    input_sha256 = _sha256(payload)
    existing = db.execute(
        "SELECT * FROM hybrid_outcome_ledger WHERE scenario_set_id=? AND idempotency_key=?",
        (scenario["id"], data["idempotency_key"]),
    ).fetchone()
    if existing:
        if existing["input_sha256"] != input_sha256:
            raise HTTPException(409, "outcome idempotency collision")
        result = _outcome_detail(existing)
        result["idempotent"] = True
        return result
    case_existing = db.execute(
        "SELECT id FROM hybrid_outcome_ledger WHERE scenario_set_id=? AND policy_id=?",
        (scenario["id"], policy["id"]),
    ).fetchone()
    if case_existing:
        raise HTTPException(409, "terminal case outcome already recorded")
    realized_label, realized_reason, pricing = _realize_case(validated_bars, scenario)
    cohort_key = _cohort_key(scenario)
    row = db.execute(
        """INSERT INTO hybrid_outcome_ledger(
          scenario_set_id,policy_id,idempotency_key,cohort_key,case_id,event_type,profile_id,profile_version,scenario_kind,
          scenario_set_version,observation_cutoff_at,observed_at,realized_label,realized_reason,gross_label,gross_return_bps,
          net_return_bps,input_sha256,pricing_json,outcome_json,bars_json,created_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) RETURNING *""",
        (
            scenario["id"],
            policy["id"],
            data["idempotency_key"],
            cohort_key,
            scenario["id"],
            scenario["event_type"],
            scenario["profile_id"],
            scenario["profile_version"],
            scenario.get("scenario_kind") or "QUANTITATIVE",
            scenario["version"],
            cutoff_at.isoformat(),
            validated_bars[-1]["known_at"],
            realized_label,
            realized_reason,
            pricing["gross_label"],
            pricing["gross_return_bps"],
            pricing["net_return_bps"],
            input_sha256,
            canon(pricing),
            payload,
            canon(validated_bars),
            now(),
        ),
    ).fetchone()
    db.commit()
    result = _outcome_detail(row)
    result["idempotent"] = False
    return result


def _outcome_detail(row: sqlite3.Row | None) -> dict[str, Any]:
    if row is None:
        raise HTTPException(404, "hybrid outcome not found")
    out = dict(row)
    out["outcome"] = json.loads(out["outcome_json"])
    out["bars"] = json.loads(out["bars_json"])
    out["pricing"] = json.loads(out["pricing_json"]) if out.get("pricing_json") else {}
    out["policy"] = _policy_detail(
        {
            "id": out["policy_id"],
            "policy_identity": HYBRID_POLICY_IDENTITY,
            "policy_version": HYBRID_POLICY_VERSION,
            "policy_hash": _sha256(canon(_policy_spec())),
            "policy_json": canon(_policy_spec()),
            "created_at": out["created_at"],
        }
    )
    return out


def _wilson_lower_bound(successes: int, total: int, *, z: float = 1.96) -> float:
    if total <= 0:
        return 0.0
    p = successes / total
    z2 = z * z
    denom = 1 + z2 / total
    center = p + z2 / (2 * total)
    adj = z * math.sqrt((p * (1 - p) + z2 / (4 * total)) / total)
    return max(0.0, (center - adj) / denom)


def _plan_detail(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    out = dict(row)
    out["plan"] = json.loads(out["plan_json"])
    return out


def create_calibration_plan(db: sqlite3.Connection, data: dict[str, Any]) -> dict[str, Any]:
    if any(key in data for key in DERIVED_FIELDS):
        raise HTTPException(422, "derived fields are server-owned")
    try:
        payload = HybridCalibrationPlanIn.model_validate(data)
    except Exception as exc:
        if isinstance(exc, HTTPException):
            raise
        raise HTTPException(422, "invalid calibration plan request") from exc
    policy = ensure_policy(db)
    scenario = detail_by_id(db, payload.scenario_set_id)
    if scenario["card_id"] is None or scenario["evidence_id"] is None:
        raise HTTPException(409, "calibration requires frozen scenario lineage")
    cohort_key = _cohort_key(scenario)
    frozen_at = now()
    existing = db.execute(
        "SELECT * FROM hybrid_calibration_plans WHERE scenario_set_id=? AND policy_id=? AND idempotency_key=?",
        (scenario["id"], policy["id"], payload.idempotency_key),
    ).fetchone()
    body = canon(
        {
            "scenario_set_id": scenario["id"],
            "policy_id": policy["id"],
            "idempotency_key": payload.idempotency_key,
            "cohort_key": cohort_key,
            "is_window": {"start": payload.is_window.start, "end": payload.is_window.end},
            "oos_window": {"start": payload.oos_window.start, "end": payload.oos_window.end},
            "cutoff_at": payload.cutoff_at,
            "holdout_key": payload.holdout_key,
        }
    )
    input_sha256 = _sha256(body)
    if existing:
        if existing["input_sha256"] != input_sha256:
            raise HTTPException(409, "calibration plan collision")
        detail = _plan_detail(existing)
        detail["plan"]["id"] = detail["id"]
        detail["idempotent"] = True
        return detail
    row = db.execute(
        """INSERT INTO hybrid_calibration_plans(
          scenario_set_id,policy_id,idempotency_key,input_sha256,cohort_key,frozen_at,holdout_key,
          is_window_start,is_window_end,oos_window_start,oos_window_end,cutoff_at,plan_json,created_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?) RETURNING *""",
        (
            scenario["id"],
            policy["id"],
            payload.idempotency_key,
            input_sha256,
            cohort_key,
            frozen_at,
            payload.holdout_key,
            payload.is_window.start,
            payload.is_window.end,
            payload.oos_window.start,
            payload.oos_window.end,
            payload.cutoff_at,
            body,
            now(),
        ),
    ).fetchone()
    db.commit()
    detail = _plan_detail(row)
    detail["plan"]["id"] = detail["id"]
    detail["scenario_set_id"] = detail["scenario_set_id"]
    detail["policy_id"] = detail["policy_id"]
    detail["idempotent"] = False
    return detail


def create_calibration_snapshot(db: sqlite3.Connection, data: dict[str, Any]) -> dict[str, Any]:
    if any(key in data for key in DERIVED_FIELDS):
        raise HTTPException(422, "derived fields are server-owned")
    try:
        payload = HybridCalibrationSnapshotIn.model_validate(data)
    except Exception as exc:
        if isinstance(exc, HTTPException):
            raise
        raise HTTPException(422, "invalid calibration snapshot request") from exc
    plan_row = db.execute("SELECT * FROM hybrid_calibration_plans WHERE id=?", (payload.plan_id,)).fetchone()
    if plan_row is None:
        raise HTTPException(404, "calibration plan not found")
    plan = _plan_detail(plan_row)
    policy = ensure_policy(db)
    if plan["policy_id"] != policy["id"]:
        raise HTTPException(409, "calibration plan policy mismatch")
    existing = db.execute(
        "SELECT * FROM hybrid_calibration_snapshots WHERE plan_id=? AND idempotency_key=?",
        (plan["id"], payload.idempotency_key),
    ).fetchone()
    body = canon({"plan_id": plan["id"], "idempotency_key": payload.idempotency_key})
    input_sha256 = _sha256(body)
    if existing:
        if existing["input_sha256"] != input_sha256:
            raise HTTPException(409, "calibration snapshot collision")
        detail = _snapshot_detail(existing)
        snapshot = detail["snapshot"]
        snapshot["eligible"] = bool(detail["eligible"])
        snapshot["failure_reasons"] = json.loads(detail["failure_reasons"])
        snapshot["id"] = detail["id"]
        snapshot["idempotent"] = True
        return {
            **snapshot,
            "id": detail["id"],
            "plan_id": detail["plan_id"],
            "scenario_set_id": detail["scenario_set_id"],
            "policy_id": detail["policy_id"],
            "idempotency_key": detail["idempotency_key"],
            "holdout_key": detail["holdout_key"],
            "cutoff_at": detail["cutoff_at"],
            "eligible": bool(detail["eligible"]),
            "failure_reasons": json.loads(detail["failure_reasons"]) if isinstance(detail["failure_reasons"], str) else detail["failure_reasons"],
            "created_at": detail["created_at"],
            "snapshot": snapshot,
            "idempotent": True,
        }
    scenario = detail_by_id(db, plan["scenario_set_id"])
    if _cohort_key(scenario) != plan["cohort_key"]:
        raise HTTPException(409, "calibration plan cohort mismatch")
    cutoff_at = parse_kst(plan["cutoff_at"])
    frozen_at = parse_kst(plan["frozen_at"])
    is_start, is_end = parse_kst(plan["is_window_start"]), parse_kst(plan["is_window_end"])
    oos_start, oos_end = parse_kst(plan["oos_window_start"]), parse_kst(plan["oos_window_end"])
    if is_end > oos_start:
        raise HTTPException(422, "chronological IS/OOS windows required")
    if oos_end > cutoff_at:
        raise HTTPException(422, "cutoff must cover OOS window")
    latest_rows = [
        dict(row)
        for row in db.execute(
            """
            SELECT o.*
            FROM hybrid_outcome_ledger o
            JOIN (
              SELECT scenario_set_id, MAX(id) AS id
              FROM hybrid_outcome_ledger
              WHERE policy_id=? AND cohort_key=? AND scenario_set_id != ? AND observation_cutoff_at <= ?
              GROUP BY scenario_set_id
            ) latest ON latest.id=o.id
            ORDER BY o.observed_at, o.id
            """,
            (plan["policy_id"], plan["cohort_key"], plan["scenario_set_id"], plan["cutoff_at"]),
        )
    ]
    if any(parse_kst(row["observed_at"]) <= frozen_at for row in latest_rows):
        raise HTTPException(422, "calibration plan was frozen after eligible outcome evidence")
    selected_rows = [row for row in latest_rows if is_start <= parse_kst(row["observed_at"]) < oos_end]
    is_rows = [row for row in selected_rows if is_start <= parse_kst(row["observed_at"]) < is_end]
    oos_rows = [row for row in selected_rows if oos_start <= parse_kst(row["observed_at"]) < oos_end]
    overall_total = len(selected_rows)
    is_total = len(is_rows)
    oos_total = len(oos_rows)
    counts = {label: sum(1 for row in selected_rows if row["realized_label"] == label) for label in ("GOOD", "BASE", "BAD")}
    is_counts = {label: sum(1 for row in is_rows if row["realized_label"] == label) for label in ("GOOD", "BASE", "BAD")}
    oos_counts = {label: sum(1 for row in oos_rows if row["realized_label"] == label) for label in ("GOOD", "BASE", "BAD")}
    good_probability = 0.0 if oos_total == 0 else oos_counts["GOOD"] / oos_total
    good_lower_bound = _wilson_lower_bound(oos_counts["GOOD"], oos_total) if oos_total else 0.0
    reuse_row = db.execute(
        "SELECT count(*) n FROM hybrid_calibration_snapshots WHERE plan_id=? AND idempotency_key=?",
        (plan["id"], payload.idempotency_key),
    ).fetchone()
    holdout_reuse_count = int(reuse_row["n"]) + 1
    case_ids = [row["scenario_set_id"] for row in selected_rows]
    oos_case_ids = [row["scenario_set_id"] for row in oos_rows]
    is_case_ids = [row["scenario_set_id"] for row in is_rows]
    concentration_label = max(counts, key=counts.get) if selected_rows else "BASE"
    concentration_pct = 0.0 if overall_total == 0 else round(counts[concentration_label] / overall_total * 100, 8)
    prior_label = max((item for item in scenario["scenarios"]), key=lambda item: item["probability"])["label"]
    prior_predictions = [{"case_id": row["scenario_set_id"], "prediction": prior_label, "realized_label": row["realized_label"]} for row in oos_rows]
    prior_accuracy = 0.0 if not prior_predictions else sum(1 for item in prior_predictions if item["prediction"] == item["realized_label"]) / len(prior_predictions)
    failure_reasons: list[str] = []
    if overall_total < HYBRID_MIN_OVERALL_SAMPLES:
        failure_reasons.append("insufficient overall sample")
    if oos_total < HYBRID_MIN_OOS_SAMPLES:
        failure_reasons.append("insufficient oos sample")
    if holdout_reuse_count > 1:
        failure_reasons.append("holdout reused")
    if Decimal(str(good_lower_bound)) < HYBRID_GOOD_LCB_THRESHOLD:
        failure_reasons.append("good lower bound below threshold")
    snapshot = {
        "policy": {
            "identity": policy["policy_identity"],
            "version": policy["policy_version"],
            "hash": policy["policy_hash"],
        },
        "plan": {
            "id": plan["id"],
            "frozen_at": plan["frozen_at"],
            "idempotency_key": plan["idempotency_key"],
            "input_sha256": plan["input_sha256"],
        },
        "scenario": {
            "id": scenario["id"],
            "event_identity": scenario["event_identity"],
            "version": scenario["version"],
            "symbol": scenario["symbol"],
            "card_id": scenario["card_id"],
            "evidence_id": scenario["evidence_id"],
        },
        "cohort": {
            "key": plan["cohort_key"],
            "target_case_id": plan["scenario_set_id"],
            "case_ids": case_ids,
            "is_case_ids": is_case_ids,
            "oos_case_ids": oos_case_ids,
        },
        "windows": {
            "is": {"start": is_start.isoformat(), "end": is_end.isoformat(), "count": is_total},
            "oos": {"start": oos_start.isoformat(), "end": oos_end.isoformat(), "count": oos_total},
            "cutoff_at": cutoff_at.isoformat(),
        },
        "counts": {
            "overall": overall_total,
            "is": is_counts,
            "oos": oos_counts,
        },
        "good": {
            "count": oos_counts["GOOD"],
            "probability": round(good_probability, 8),
            "lower_bound": round(good_lower_bound, 8),
        },
        "controls": {
            "hold": {"case_ids": oos_case_ids, "net_return_bps": 0.0, "denominator": oos_total, "result": "HOLD"},
            "structural_prior_only": {
                "prediction_rule": f"argmax_prior:{prior_label}",
                "predicted_label": prior_label,
                "accuracy": round(prior_accuracy, 8),
                "case_ids": oos_case_ids,
                "net_return_bps": round(sum(float(row["net_return_bps"]) for row in oos_rows if row["realized_label"] == prior_label), 8),
            },
        },
        "concentration": {
            "label": concentration_label,
            "share_pct": concentration_pct,
        },
        "costs": {
            "transaction_cost_bps": HYBRID_TRANSACTION_COST_BPS,
            "round_trip_cost_bps": HYBRID_ROUND_TRIP_COST_BPS,
            "round_trip_cost_rate": float(Decimal(HYBRID_ROUND_TRIP_COST_BPS) / Decimal(10000)),
        },
        "known_at_cutoff": cutoff_at.isoformat(),
        "holdout_reuse_count": holdout_reuse_count,
        "holdout_key": plan["holdout_key"],
        "denominator": oos_total,
        "eligibility_verdict": "ELIGIBLE" if not failure_reasons else "INELIGIBLE",
        "failure_reasons": failure_reasons,
        "lineage": {
            "scenario_set_id": scenario["id"],
            "policy_id": policy["id"],
            "policy_hash": policy["policy_hash"],
            "plan_id": plan["id"],
            "case_ids": case_ids,
            "oos_case_ids": oos_case_ids,
            "is_case_ids": is_case_ids,
            "outcome_ids": [row["id"] for row in selected_rows],
            "cohort_key": plan["cohort_key"],
            "frozen_at": plan["frozen_at"],
        },
    }
    snapshot["eligible"] = not failure_reasons
    snapshot["is"] = snapshot["windows"]["is"]
    snapshot["oos"] = snapshot["windows"]["oos"]
    row = db.execute(
        """INSERT INTO hybrid_calibration_snapshots(
          plan_id,scenario_set_id,policy_id,idempotency_key,cohort_key,holdout_key,cutoff_at,input_sha256,snapshot_json,eligible,failure_reasons,created_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?) RETURNING *""",
        (
            plan["id"],
            scenario["id"],
            policy["id"],
            payload.idempotency_key,
            plan["cohort_key"],
            plan["holdout_key"],
            cutoff_at.isoformat(),
            input_sha256,
            canon(snapshot),
            int(not failure_reasons),
            canon(failure_reasons),
            now(),
        ),
    ).fetchone()
    db.commit()
    detail = _snapshot_detail(row)
    snapshot["id"] = row["id"]
    snapshot["scenario_set_id"] = row["scenario_set_id"]
    snapshot["policy_id"] = row["policy_id"]
    snapshot["holdout_key"] = row["holdout_key"]
    snapshot["cutoff_at"] = row["cutoff_at"]
    snapshot["eligible"] = bool(row["eligible"])
    snapshot["failure_reasons"] = json.loads(row["failure_reasons"])
    snapshot["created_at"] = row["created_at"]
    snapshot["idempotent"] = False
    return {
        **snapshot,
        "id": row["id"],
        "plan_id": row["plan_id"],
        "scenario_set_id": row["scenario_set_id"],
        "policy_id": row["policy_id"],
        "idempotency_key": row["idempotency_key"],
        "holdout_key": row["holdout_key"],
        "cutoff_at": row["cutoff_at"],
        "eligible": bool(row["eligible"]),
        "failure_reasons": json.loads(row["failure_reasons"]),
        "created_at": row["created_at"],
        "snapshot": snapshot,
        "idempotent": False,
    }


def read_calibration_snapshot(db: sqlite3.Connection, snapshot_id: int) -> dict[str, Any]:
    row = db.execute("SELECT * FROM hybrid_calibration_snapshots WHERE id=?", (snapshot_id,)).fetchone()
    if not row:
        raise HTTPException(404, "calibration snapshot not found")
    detail = _snapshot_detail(row)
    snapshot = detail["snapshot"]
    snapshot["eligible"] = bool(detail["eligible"])
    snapshot["failure_reasons"] = json.loads(detail["failure_reasons"]) if isinstance(detail["failure_reasons"], str) else detail["failure_reasons"]
    snapshot["holdout_reuse_count"] = snapshot.get("holdout_reuse_count", detail.get("holdout_reuse_count"))
    snapshot["is"] = snapshot["windows"]["is"]
    snapshot["oos"] = snapshot["windows"]["oos"]
    snapshot["id"] = detail["id"]
    return {
        **snapshot,
        "id": detail["id"],
        "plan_id": detail["plan_id"],
        "scenario_set_id": detail["scenario_set_id"],
        "policy_id": detail["policy_id"],
        "idempotency_key": detail["idempotency_key"],
        "holdout_key": detail["holdout_key"],
        "cutoff_at": detail["cutoff_at"],
        "eligible": bool(detail["eligible"]),
        "failure_reasons": json.loads(detail["failure_reasons"]) if isinstance(detail["failure_reasons"], str) else detail["failure_reasons"],
        "created_at": detail["created_at"],
        "snapshot": snapshot,
    }


def _good_band(scenario: dict[str, Any]) -> dict[str, float]:
    return next(item["per_share_value_range_krw"] for item in scenario["scenarios"] if item["label"] == "GOOD")


def _policy_costs(price_krw: float) -> dict[str, float]:
    price = _decimal(price_krw)
    per_side = price * Decimal(HYBRID_TRANSACTION_COST_BPS) / Decimal(10000)
    round_trip = price * Decimal(HYBRID_ROUND_TRIP_COST_BPS) / Decimal(10000)
    return {
        "transaction_cost_bps": HYBRID_TRANSACTION_COST_BPS,
        "round_trip_cost_bps": HYBRID_ROUND_TRIP_COST_BPS,
        "entry_cost_krw": float(per_side.quantize(Decimal("0.00000001"), rounding=ROUND_HALF_UP)),
        "exit_cost_krw": float(per_side.quantize(Decimal("0.00000001"), rounding=ROUND_HALF_UP)),
        "round_trip_cost_krw": float(round_trip.quantize(Decimal("0.00000001"), rounding=ROUND_HALF_UP)),
    }


def create_evaluation(db: sqlite3.Connection, card_id: int, data: dict[str, Any]) -> dict[str, Any]:
    if any(key in data for key in DERIVED_FIELDS):
        raise HTTPException(422, "derived fields are server-owned")
    try:
        request = HybridEvaluationIn.model_validate(data)
    except Exception as exc:
        if isinstance(exc, HTTPException):
            raise
        raise HTTPException(422, "invalid evaluation request") from exc
    card = db.execute("SELECT * FROM decision_cards WHERE id=?", (card_id,)).fetchone()
    if not card:
        raise HTTPException(404, "decision card not found")
    evidence = db.execute("SELECT * FROM material_evidence WHERE id=?", (card["evidence_id"],)).fetchone()
    if not evidence:
        raise HTTPException(404, "evidence not found")
    scenario = db.execute("SELECT * FROM event_scenario_sets WHERE card_id=? ORDER BY version DESC, id DESC LIMIT 1", (card_id,)).fetchone()
    if not scenario:
        raise HTTPException(409, "hybrid evaluation requires frozen scenario set")
    scenario_detail = detail_by_id(db, scenario["id"])
    calibration = read_calibration_snapshot(db, request.calibration_snapshot_id)
    if calibration["scenario_set_id"] != scenario["id"]:
        raise HTTPException(409, "calibration snapshot does not match scenario set")
    if calibration["policy"]["identity"] != request.policy_identity or calibration["policy"]["version"] != request.policy_version:
        raise HTTPException(409, "policy mismatch")
    market_context = latest_market_context_for_card(db, card_id)
    if not market_context:
        raise HTTPException(409, "verified 09:05 market context required")
    latest_market = market_context["result"]["metrics"]
    policy = ensure_policy(db)
    good_band = _band_for_label(scenario_detail, "GOOD")
    price_krw = latest_market.get("last_price_krw")
    spread_pct = latest_market.get("spread_pct")
    top_imbalance = latest_market.get("top_of_book_imbalance")
    volume_ratio = latest_market.get("same_time_baseline_volume_ratio")
    top_bid_qty = latest_market.get("top_bid_qty")
    top_ask_qty = latest_market.get("top_ask_qty")
    top_book_ok = (
        top_bid_qty is not None
        and top_ask_qty is not None
        and Decimal(str(top_bid_qty)) >= HYBRID_MIN_TOP_BOOK_QTY
        and Decimal(str(top_ask_qty)) >= HYBRID_MIN_TOP_BOOK_QTY
    )
    market_verified = (
        market_context["status"] == "VERIFIED"
        and latest_market.get("same_time_baseline_status") == "READY"
        and latest_market.get("previous_close_gap_status") == "VERIFIED"
        and spread_pct is not None
        and Decimal(str(spread_pct)) <= HYBRID_MAX_SPREAD_PCT
        and top_imbalance is not None
        and abs(Decimal(str(top_imbalance))) <= Decimal("0.5")
        and volume_ratio is not None
        and top_book_ok
    )
    invalidated = bool(card["invalidated_at"] or evidence["invalidated_at"] or evidence["status"] == "invalidated")
    failure_reasons = list(calibration["snapshot"]["failure_reasons"])
    within_guardrail = price_krw is not None and good_band["low"] <= Decimal(str(price_krw)) <= good_band["high"]
    costs = _policy_costs(float(price_krw or 0.0))
    cost_adjusted_entry_price = None
    within_cost_guardrail = False
    if price_krw is not None:
        cost_adjusted_entry_price = (Decimal(str(price_krw)) - Decimal(str(costs["entry_cost_krw"]))).quantize(Decimal("0.00000001"), rounding=ROUND_HALF_UP)
        within_cost_guardrail = good_band["low"] <= cost_adjusted_entry_price <= good_band["high"]
    structural_good_compatibility = {
        "status": "UNAVAILABLE",
        "inputs": {
            "scenario_band": {"low": float(good_band["low"]), "high": float(good_band["high"])},
            "price_krw": price_krw,
            "round_trip_cost_bps": HYBRID_ROUND_TRIP_COST_BPS,
            "entry_cost_krw": costs["entry_cost_krw"],
            "cost_adjusted_entry_price_krw": None if cost_adjusted_entry_price is None else float(cost_adjusted_entry_price),
            "top_bid_qty": top_bid_qty,
            "top_ask_qty": top_ask_qty,
            "same_time_baseline_volume_ratio": volume_ratio,
        },
        "reasons": [],
    }
    if invalidated:
        structural_good_compatibility["reasons"].append("business_invalidated")
    elif within_cost_guardrail and top_book_ok and volume_ratio is not None:
        structural_good_compatibility["status"] = "GOOD_COMPATIBLE"
    else:
        if not within_guardrail:
            structural_good_compatibility["reasons"].append("price_outside_frozen_good_band")
        if price_krw is not None and not within_cost_guardrail:
            structural_good_compatibility["reasons"].append("cost_adjusted_entry_outside_frozen_good_band")
        if not top_book_ok:
            structural_good_compatibility["reasons"].append("min_top_of_book_qty_not_satisfied")
        if volume_ratio is None:
            structural_good_compatibility["reasons"].append("same_time_baseline_volume_ratio_missing")
    final_state = "HOLD"
    recommendation = "HOLD_INSUFFICIENT_EVIDENCE"
    if invalidated:
        final_state = "BAD"
        recommendation = "REDUCE_REVIEW"
    elif not calibration["eligible"]:
        final_state = "HOLD"
        recommendation = "HOLD_INSUFFICIENT_EVIDENCE"
    elif market_verified and within_guardrail:
        if Decimal(str(calibration["snapshot"]["good"]["lower_bound"])) >= HYBRID_GOOD_LCB_THRESHOLD:
            final_state = "GOOD"
            recommendation = "BUY_REVIEW"
        else:
            final_state = "BASE"
            recommendation = "WATCH"
    else:
        final_state = "BASE"
        recommendation = "NO_ACTION"
    lineage_hash = _sha256(
        canon(
            {
                "card_id": card_id,
                "scenario_set_id": scenario["id"],
                "market_context_run_id": market_context["id"],
                "calibration_snapshot_id": calibration["id"],
                "policy_id": policy["id"],
                "invalidated": invalidated,
            }
        )
    )
    existing = db.execute(
        "SELECT * FROM hybrid_second_stage_evaluations WHERE lineage_hash=?",
        (lineage_hash,),
    ).fetchone()
    if existing:
        result = _evaluation_detail(existing)
        evaluation = result["evaluation"]
        evaluation["id"] = result["id"]
        evaluation["idempotent"] = True
        return evaluation
    probability = calibration["snapshot"]["good"]["probability"]
    lower_bound = calibration["snapshot"]["good"]["lower_bound"]
    costs = _policy_costs(float(price_krw or 0.0))
    evaluation = {
        "card_id": card_id,
        "scenario_set_id": scenario["id"],
        "scenario_set_version": scenario["version"],
        "market_context_run_id": market_context["id"],
        "market_context": {
            "id": market_context["id"],
            "run_key": market_context["run_key"],
            "market_context_status": market_context["status"],
            "known_at": market_context["known_at"],
            "retrieved_at": market_context["retrieved_at"],
            "metrics": market_context["result"]["metrics"],
        },
        "calibration_snapshot": calibration,
        "policy": {
            "identity": policy["policy_identity"],
            "version": policy["policy_version"],
            "hash": policy["policy_hash"],
        },
        "policy_identity": policy["policy_identity"],
        "policy_version": policy["policy_version"],
        "policy_hash": policy["policy_hash"],
        "probability": probability,
        "lower_bound": lower_bound,
        "denominator": calibration["snapshot"]["denominator"],
        "final_state": final_state,
        "recommendation": recommendation,
        "recommendation_only": True,
        "reason_codes": failure_reasons if failure_reasons else ([recommendation] if recommendation != "BUY_REVIEW" else []),
        "structural_good_compatibility": structural_good_compatibility,
        "lineage_ids": {
            "card_id": card_id,
            "scenario_set_id": scenario["id"],
            "market_context_run_id": market_context["id"],
            "calibration_snapshot_id": calibration["id"],
        },
        "lineage_hash": lineage_hash,
        "known_at": market_context["known_at"],
        "costs": costs,
        "quality": {
            "oos_count": calibration["snapshot"]["windows"]["oos"]["count"],
            "overall_count": calibration["snapshot"]["counts"]["overall"],
            "holdout_reuse_count": calibration["snapshot"]["holdout_reuse_count"],
            "top_bid_qty": top_bid_qty,
            "top_ask_qty": top_ask_qty,
        },
    }
    row = db.execute(
        """INSERT INTO hybrid_second_stage_evaluations(
          card_id,scenario_set_id,scenario_set_version,market_context_run_id,calibration_snapshot_id,policy_id,
          policy_identity,policy_version,policy_hash,probability,lower_bound,denominator,final_state,recommendation,
          recommendation_only,reason_codes_json,lineage_hash,known_at,costs_json,evaluation_json,created_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) RETURNING *""",
        (
            card_id,
            scenario["id"],
            scenario["version"],
            market_context["id"],
            calibration["id"],
            policy["id"],
            policy["policy_identity"],
            policy["policy_version"],
            policy["policy_hash"],
            probability,
            lower_bound,
            calibration["snapshot"]["denominator"],
            final_state,
            recommendation,
            1,
            canon(evaluation["reason_codes"]),
            lineage_hash,
            market_context["known_at"],
            canon(costs),
            canon(evaluation),
            now(),
        ),
    ).fetchone()
    db.commit()
    result = _evaluation_detail(row)
    evaluation = result["evaluation"]
    evaluation["id"] = result["id"]
    evaluation["idempotent"] = False
    return evaluation


def latest_evaluation_for_card(db: sqlite3.Connection, card_id: int) -> dict[str, Any] | None:
    row = db.execute("SELECT * FROM hybrid_second_stage_evaluations WHERE card_id=? ORDER BY id DESC LIMIT 1", (card_id,)).fetchone()
    return _evaluation_detail(row) if row else None
