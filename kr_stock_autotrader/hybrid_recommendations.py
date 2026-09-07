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
HYBRID_MIN_OVERALL_SAMPLES = 3
HYBRID_MIN_OOS_SAMPLES = 2
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


class HybridCalibrationIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

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
    scenario = detail_by_id(db, db.execute("SELECT id FROM event_scenario_sets WHERE event_identity=? ORDER BY id DESC LIMIT 1", (scenario_identity,)).fetchone()["id"])
    policy = ensure_policy(db)
    try:
        cutoff_at = parse_kst(data["observation_cutoff_at"])
    except (KeyError, TypeError, ValueError):
        raise HTTPException(422, "observation_cutoff_at must be KST ISO-8601")
    frozen_at = parse_kst(scenario["frozen_at"])
    if cutoff_at <= frozen_at:
        raise HTTPException(422, "observation cutoff must follow scenario freeze")
    try:
        bars = HybridOutcomeIn.model_validate(data).bars
    except Exception as exc:
        if isinstance(exc, HTTPException):
            raise
        raise HTTPException(422, "invalid hybrid outcome payload") from exc
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
    realized_label, realized_reason = _realize_outcome(validated_bars, scenario)
    row = db.execute(
        """INSERT INTO hybrid_outcome_ledger(
          scenario_set_id,policy_id,idempotency_key,observation_cutoff_at,observed_at,realized_label,realized_reason,
          input_sha256,outcome_json,bars_json,created_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?) RETURNING *""",
        (
            scenario["id"],
            policy["id"],
            data["idempotency_key"],
            cutoff_at.isoformat(),
            validated_bars[-1]["known_at"],
            realized_label,
            realized_reason,
            input_sha256,
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


def create_calibration_snapshot(db: sqlite3.Connection, data: dict[str, Any]) -> dict[str, Any]:
    if any(key in data for key in DERIVED_FIELDS):
        raise HTTPException(422, "derived fields are server-owned")
    try:
        payload = HybridCalibrationIn.model_validate(data)
    except Exception as exc:
        if isinstance(exc, HTTPException):
            raise
        raise HTTPException(422, "invalid calibration request") from exc
    policy = ensure_policy(db)
    scenario = detail_by_id(db, payload.scenario_set_id)
    if scenario["card_id"] is None or scenario["evidence_id"] is None:
        raise HTTPException(409, "calibration requires frozen scenario lineage")
    cutoff_at = parse_kst(payload.cutoff_at)
    is_start, is_end = parse_kst(payload.is_window.start), parse_kst(payload.is_window.end)
    oos_start, oos_end = parse_kst(payload.oos_window.start), parse_kst(payload.oos_window.end)
    if is_end > oos_start:
        raise HTTPException(422, "chronological IS/OOS windows required")
    if oos_end > cutoff_at:
        raise HTTPException(422, "cutoff must cover OOS window")
    rows = [
        dict(row)
        for row in db.execute(
            "SELECT * FROM hybrid_outcome_ledger WHERE scenario_set_id=? AND policy_id=? AND observation_cutoff_at<=? ORDER BY observed_at, id",
            (scenario["id"], policy["id"], cutoff_at.isoformat()),
        )
    ]
    if not rows:
        raise HTTPException(422, "no calibrated outcomes available")
    overall_total = len(rows)
    is_rows = [row for row in rows if is_start <= parse_kst(row["observed_at"]) < is_end]
    oos_rows = [row for row in rows if oos_start <= parse_kst(row["observed_at"]) < oos_end]
    counts = {label: sum(1 for row in rows if row["realized_label"] == label) for label in ("GOOD", "BASE", "BAD")}
    is_counts = {label: sum(1 for row in is_rows if row["realized_label"] == label) for label in ("GOOD", "BASE", "BAD")}
    oos_counts = {label: sum(1 for row in oos_rows if row["realized_label"] == label) for label in ("GOOD", "BASE", "BAD")}
    oos_total = len(oos_rows)
    good_probability = 0.0 if oos_total == 0 else oos_counts["GOOD"] / oos_total
    good_lower_bound = _wilson_lower_bound(oos_counts["GOOD"], oos_total) if oos_total else 0.0
    reuse_row = db.execute(
        "SELECT count(*) n FROM hybrid_calibration_snapshots WHERE scenario_set_id=? AND policy_id=? AND holdout_key=?",
        (scenario["id"], policy["id"], payload.holdout_key),
    ).fetchone()
    holdout_reuse_count = int(reuse_row["n"]) + 1
    concentration_label = max(counts, key=counts.get)
    concentration_pct = 0.0 if overall_total == 0 else round(counts[concentration_label] / overall_total * 100, 8)
    failure_reasons = []
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
        "scenario": {
            "id": scenario["id"],
            "event_identity": scenario["event_identity"],
            "version": scenario["version"],
            "symbol": scenario["symbol"],
            "card_id": scenario["card_id"],
            "evidence_id": scenario["evidence_id"],
        },
        "windows": {
            "is": {"start": is_start.isoformat(), "end": is_end.isoformat(), "count": len(is_rows)},
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
            "hold": {"count": oos_counts["BASE"], "result": "HOLD"},
            "structural_prior_only": {"count": overall_total, "result": concentration_label},
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
        "holdout_key": payload.holdout_key,
        "denominator": oos_total,
        "eligibility_verdict": "ELIGIBLE" if not failure_reasons else "INELIGIBLE",
        "failure_reasons": failure_reasons,
        "lineage": {
            "scenario_set_id": scenario["id"],
            "policy_id": policy["id"],
            "policy_hash": policy["policy_hash"],
            "outcome_ids": [row["id"] for row in rows],
        },
    }
    snapshot["eligible"] = not failure_reasons
    snapshot["failure_reasons"] = failure_reasons
    snapshot["holdout_reuse_count"] = holdout_reuse_count
    snapshot["is"] = snapshot["windows"]["is"]
    snapshot["oos"] = snapshot["windows"]["oos"]
    body = canon(
        {
            "scenario_set_id": scenario["id"],
            "policy_id": policy["id"],
            "holdout_key": payload.holdout_key,
            "cutoff_at": cutoff_at.isoformat(),
            "snapshot": snapshot,
        }
    )
    row = db.execute(
        """INSERT INTO hybrid_calibration_snapshots(
          scenario_set_id,policy_id,holdout_key,cutoff_at,input_sha256,snapshot_json,eligible,failure_reasons,created_at
        ) VALUES(?,?,?,?,?,?,?,?,?) RETURNING *""",
        (
            scenario["id"],
            policy["id"],
            payload.holdout_key,
            cutoff_at.isoformat(),
            _sha256(body),
            canon(snapshot),
            int(not failure_reasons),
            canon(failure_reasons),
            now(),
        ),
    ).fetchone()
    db.commit()
    return {
        **snapshot,
        "id": row["id"],
        "scenario_set_id": row["scenario_set_id"],
        "policy_id": row["policy_id"],
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
    return {
        **snapshot,
        "id": detail["id"],
        "scenario_set_id": detail["scenario_set_id"],
        "policy_id": detail["policy_id"],
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
    good_band = _good_band(scenario_detail)
    price_krw = latest_market.get("last_price_krw")
    spread_pct = latest_market.get("spread_pct")
    top_imbalance = latest_market.get("top_of_book_imbalance")
    market_verified = (
        market_context["status"] == "VERIFIED"
        and latest_market.get("same_time_baseline_status") == "READY"
        and latest_market.get("previous_close_gap_status") == "VERIFIED"
        and spread_pct is not None
        and Decimal(str(spread_pct)) <= HYBRID_MAX_SPREAD_PCT
        and top_imbalance is not None
        and abs(Decimal(str(top_imbalance))) <= Decimal("0.5")
    )
    invalidated = bool(card["invalidated_at"] or evidence["invalidated_at"] or evidence["status"] == "invalidated")
    failure_reasons = list(calibration["snapshot"]["failure_reasons"])
    within_guardrail = price_krw is not None and good_band["low"] <= float(price_krw) <= good_band["high"]
    final_state = "HOLD"
    recommendation = "HOLD_INSUFFICIENT_EVIDENCE"
    if invalidated:
        final_state = "BAD"
        recommendation = "REDUCE_REVIEW"
    elif calibration["eligible"] and market_verified and within_guardrail:
        if Decimal(str(calibration["snapshot"]["good"]["lower_bound"])) >= HYBRID_GOOD_LCB_THRESHOLD:
            final_state = "GOOD"
            recommendation = "BUY_REVIEW"
        else:
            final_state = "BASE"
            recommendation = "WATCH"
    elif calibration["eligible"]:
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
        "recommendation_only": bool(request.recommendation_only),
        "reason_codes": failure_reasons if failure_reasons else ([recommendation] if recommendation != "BUY_REVIEW" else []),
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
            int(bool(request.recommendation_only)),
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
