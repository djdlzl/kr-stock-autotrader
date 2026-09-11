"""Append-only 09:05 intraday market-context capture and evaluation."""
from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from dataclasses import dataclass
from datetime import datetime, time
from typing import Iterable

from fastapi import HTTPException

from .domain import KST, is_krx_business_date, now_kst, parse_kst

INTRADAY_SCHEMA_VERSION = 1
INTRADAY_SOURCE_TOPIC = "mac:7923"
INTRADAY_BENCHMARK_SYMBOL = "229200"
INTRADAY_MINUTE_PATH = "/uapi/domestic-stock/v1/quotations/inquire-time-itemchartprice"
INTRADAY_MINUTE_TR_ID = "FHKST03010200"
INTRADAY_CAPTURE_MINUTE = time(9, 5)
INTRADAY_CAPTURE_WINDOW_END = time(9, 25)
INTRADAY_SAME_TIME_BASELINE_MIN_COUNT = 3
INTRADAY_MARKET_CONTEXT_HOLD = "MARKET_CONTEXT_HOLD"
INTRADAY_MARKET_CONTEXT_UNAVAILABLE = "MARKET_CONTEXT_UNAVAILABLE"
INTRADAY_MARKET_CONTEXT_VERIFIED = "VERIFIED"
INTRADAY_PREVIOUS_CLOSE_STATUS_INSUFFICIENT = "INSUFFICIENT_LINEAGE"
INTRADAY_PREVIOUS_CLOSE_STATUS_VERIFIED = "VERIFIED"
INTRADAY_BASELINE_STATUS_READY = "READY"
INTRADAY_BASELINE_STATUS_INSUFFICIENT = "INSUFFICIENT_HISTORY"


@dataclass(frozen=True, repr=False)
class IntradayMinuteSnapshot:
    symbol: str
    provider: str
    source: str
    tr_id: str
    requested_as_of: datetime
    retrieved_at: datetime
    bars: tuple[dict, ...]


def _canon(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _positive_number(value: object, *, minimum: float = 0.0) -> float:
    if isinstance(value, bool):
        raise ValueError("invalid intraday numeric field")
    number = float(value)
    if not math.isfinite(number) or number < minimum:
        raise ValueError("invalid intraday numeric field")
    return number


def _requested_minute(as_of: datetime) -> datetime:
    return as_of.astimezone(KST).replace(second=0, microsecond=0)


def _capture_window_is_valid(requested_as_of: datetime, retrieved_at: datetime) -> bool:
    requested = requested_as_of.astimezone(KST)
    retrieved = retrieved_at.astimezone(KST)
    if not is_krx_business_date(requested.date()):
        return False
    if requested.time() < INTRADAY_CAPTURE_MINUTE or requested.time() >= INTRADAY_CAPTURE_WINDOW_END:
        return False
    if retrieved.date() != requested.date():
        return False
    return INTRADAY_CAPTURE_MINUTE <= retrieved.time() < INTRADAY_CAPTURE_WINDOW_END


def _minute_timestamp(row: dict) -> datetime:
    stamp = f"{row['stck_bsop_date']}{row['stck_cntg_hour']}"
    return datetime.strptime(stamp, "%Y%m%d%H%M%S").replace(tzinfo=KST)


def _normalize_intraday_row(row: dict, *, requested_as_of: datetime, retrieved_at: datetime) -> dict:
    stamp = _minute_timestamp(row)
    if stamp > retrieved_at:
        raise ValueError("intraday snapshot unavailable")
    requested_minute = _requested_minute(requested_as_of)
    if stamp.date() != requested_minute.date():
        return {}
    if stamp.time() < time(9, 0) or stamp.time() > requested_minute.time():
        return {}
    open_price = _positive_number(row["stck_oprc"], minimum=0.0)
    high_price = _positive_number(row["stck_hgpr"], minimum=0.0)
    low_price = _positive_number(row["stck_lwpr"], minimum=0.0)
    last_price = _positive_number(row["stck_prpr"], minimum=0.0)
    minute_volume = _positive_number(row["cntg_vol"], minimum=0.0)
    cumulative_value = _positive_number(row["acml_tr_pbmn"], minimum=0.0)
    if low_price > high_price:
        raise ValueError("intraday snapshot unavailable")
    if not (low_price <= last_price <= high_price):
        raise ValueError("intraday snapshot unavailable")
    return {
        "exchange_at": stamp.isoformat(),
        "known_at": stamp.isoformat(),
        "retrieved_at": retrieved_at.isoformat(),
        "open_price_krw": open_price,
        "high_price_krw": high_price,
        "low_price_krw": low_price,
        "last_price_krw": last_price,
        "minute_volume": minute_volume,
        "cumulative_trade_value": cumulative_value,
        "completion_status": "in_progress" if stamp == requested_minute else "completed",
        "source": "KIS",
    }


def project_intraday_minute_snapshot(payload: object, *, symbol: str, requested_as_of: datetime, retrieved_at: datetime, provider: str, tr_id: str) -> IntradayMinuteSnapshot:
    if not isinstance(payload, dict) or payload.get("rt_cd") != "0":
        raise ValueError("intraday snapshot unavailable")
    output2 = payload.get("output2")
    if not isinstance(output2, list) or not output2:
        raise ValueError("intraday snapshot unavailable")
    if requested_as_of > retrieved_at:
        raise ValueError("intraday snapshot unavailable")
    filtered: list[dict] = []
    seen: dict[str, dict] = {}
    for row in output2:
        if not isinstance(row, dict):
            raise ValueError("intraday snapshot unavailable")
        if not {"stck_bsop_date", "stck_cntg_hour", "stck_prpr", "stck_oprc", "stck_hgpr", "stck_lwpr", "cntg_vol", "acml_tr_pbmn"} <= set(row):
            raise ValueError("intraday snapshot unavailable")
        normalized = _normalize_intraday_row(row, requested_as_of=requested_as_of, retrieved_at=retrieved_at)
        if not normalized:
            continue
        signature = _canon({key: normalized[key] for key in ("exchange_at", "open_price_krw", "high_price_krw", "low_price_krw", "last_price_krw", "minute_volume", "cumulative_trade_value", "completion_status")})
        existing = seen.get(normalized["exchange_at"])
        if existing is not None:
            existing_signature = _canon({key: existing[key] for key in ("exchange_at", "open_price_krw", "high_price_krw", "low_price_krw", "last_price_krw", "minute_volume", "cumulative_trade_value", "completion_status")})
            if existing_signature != signature:
                raise ValueError("intraday snapshot unavailable")
            continue
        seen[normalized["exchange_at"]] = normalized
        filtered.append(normalized)
    if not filtered:
        raise ValueError("intraday snapshot unavailable")
    filtered.sort(key=lambda item: item["exchange_at"])
    return IntradayMinuteSnapshot(
        symbol=symbol,
        provider=provider,
        source="KIS",
        tr_id=tr_id,
        requested_as_of=requested_as_of.astimezone(KST),
        retrieved_at=retrieved_at,
        bars=tuple(filtered),
    )


def _safe_div(numerator: float | None, denominator: float | None) -> float | None:
    if numerator is None or denominator in (None, 0):
        return None
    return numerator / denominator


def _return_pct(start: float | None, end: float | None) -> float | None:
    ratio = _safe_div(end, start)
    return None if ratio is None else round((ratio - 1) * 100, 8)


def _volume_velocity(volumes: list[float]) -> float | None:
    if len(volumes) < 2:
        return None
    return round(volumes[-1] - volumes[-2], 8)


def _volume_acceleration(volumes: list[float]) -> float | None:
    if len(volumes) < 3:
        return None
    return round((volumes[-1] - volumes[-2]) - (volumes[-2] - volumes[-3]), 8)


def evaluate_intraday_market_context(*, stock_snapshot: IntradayMinuteSnapshot | None, benchmark_snapshot: IntradayMinuteSnapshot | None, orderbook: dict | None, same_time_history: Iterable[dict] | None = None, previous_close_krw: float | None = None) -> dict:
    same_time_history = list(same_time_history or [])
    metrics = {
        "open_price_krw": None,
        "last_price_krw": None,
        "stock_return_pct": None,
        "open_to_current_return_pct": None,
        "benchmark_open_price_krw": None,
        "benchmark_last_price_krw": None,
        "benchmark_return_pct": None,
        "benchmark_excess_pct": None,
        "previous_close_gap_pct": None,
        "previous_close_gap_status": INTRADAY_PREVIOUS_CLOSE_STATUS_INSUFFICIENT,
        "same_time_baseline_cumulative_volume_0900_to_as_of": None,
        "same_time_baseline_volume_ratio": None,
        "minute_volumes": [],
        "cumulative_volume_0900_to_as_of": None,
        "volume_window_status": "UNAVAILABLE",
        "latest_completed_interval_velocity": None,
        "latest_completed_interval_acceleration": None,
        "spread_pct": None,
        "top_of_book_imbalance": None,
        "same_time_baseline_status": "INSUFFICIENT_HISTORY",
        "same_time_baseline_sample_count": 0,
        "benchmark_context_status": "UNAVAILABLE",
        "current_minute_status": "UNAVAILABLE",
    }
    reason = "market_context_unavailable"
    if stock_snapshot is None or benchmark_snapshot is None or not isinstance(orderbook, dict) or orderbook.get("status") != "ok":
        if stock_snapshot is None:
            reason = "stock_intraday_unavailable"
        elif benchmark_snapshot is None:
            reason = "benchmark_intraday_unavailable"
        elif not isinstance(orderbook, dict) or orderbook.get("status") != "ok":
            reason = "orderbook_unavailable"
        return {"market_context_status": "MARKET_CONTEXT_UNAVAILABLE", "reason": reason, "metrics": metrics, "units": _units(), "formulas": _formulas()}
    try:
        last_price = _positive_number(orderbook["last_price"], minimum=0.0)
        best_bid = _positive_number(orderbook["best_bid"], minimum=0.0)
        best_ask = _positive_number(orderbook["best_ask"], minimum=0.0)
        bid_qty = _positive_number(orderbook["top_bid_qty"], minimum=0.0)
        ask_qty = _positive_number(orderbook["top_ask_qty"], minimum=0.0)
    except (KeyError, TypeError, ValueError):
        return {"market_context_status": "MARKET_CONTEXT_UNAVAILABLE", "reason": reason, "metrics": metrics, "units": _units(), "formulas": _formulas()}
    if best_bid > best_ask or last_price <= 0:
        return {"market_context_status": "MARKET_CONTEXT_UNAVAILABLE", "reason": reason, "metrics": metrics, "units": _units(), "formulas": _formulas()}

    stock_rows = list(stock_snapshot.bars)
    benchmark_rows = list(benchmark_snapshot.bars)
    if not stock_rows or not benchmark_rows:
        return {"market_context_status": "MARKET_CONTEXT_UNAVAILABLE", "reason": "benchmark_or_stock_rows_missing", "metrics": metrics, "units": _units(), "formulas": _formulas()}

    stock_times = [row["exchange_at"] for row in stock_rows]
    benchmark_times = [row["exchange_at"] for row in benchmark_rows]
    if stock_times != benchmark_times:
        return {"market_context_status": "MARKET_CONTEXT_UNAVAILABLE", "reason": "benchmark_timestamp_alignment_missing", "metrics": metrics, "units": _units(), "formulas": _formulas()}

    stock_open = stock_rows[0]["open_price_krw"]
    benchmark_open = benchmark_rows[0]["open_price_krw"]
    benchmark_last = benchmark_rows[-1]["last_price_krw"]

    metrics["open_price_krw"] = stock_open
    metrics["last_price_krw"] = last_price
    metrics["stock_return_pct"] = _return_pct(stock_open, last_price)
    metrics["open_to_current_return_pct"] = metrics["stock_return_pct"]
    metrics["benchmark_open_price_krw"] = benchmark_open
    metrics["benchmark_last_price_krw"] = benchmark_last
    metrics["benchmark_return_pct"] = _return_pct(benchmark_open, benchmark_last)
    metrics["benchmark_excess_pct"] = None if metrics["stock_return_pct"] is None or metrics["benchmark_return_pct"] is None else round(metrics["stock_return_pct"] - metrics["benchmark_return_pct"], 8)
    metrics["spread_pct"] = round((best_ask - best_bid) / last_price * 100, 8)
    metrics["top_of_book_imbalance"] = 0.0 if bid_qty + ask_qty == 0 else round((bid_qty - ask_qty) / (bid_qty + ask_qty), 8)
    metrics["current_minute_status"] = stock_rows[-1]["completion_status"]
    completed_rows = [row for row in stock_rows if row["completion_status"] == "completed"]
    metrics["minute_volumes"] = [row["minute_volume"] for row in stock_rows]
    metrics["cumulative_volume_0900_to_as_of"] = round(sum(row["minute_volume"] for row in completed_rows), 8) if completed_rows else None
    metrics["volume_window_status"] = "IN_PROGRESS_CURRENT_MINUTE" if stock_rows[-1]["completion_status"] != "completed" else "COMPLETE"
    completed_volumes = [row["minute_volume"] for row in completed_rows]
    metrics["latest_completed_interval_velocity"] = _volume_velocity(completed_volumes)
    metrics["latest_completed_interval_acceleration"] = _volume_acceleration(completed_volumes)
    if previous_close_krw is not None and previous_close_krw > 0:
        metrics["previous_close_gap_pct"] = round((last_price - previous_close_krw) / previous_close_krw * 100, 8)
        metrics["previous_close_gap_status"] = INTRADAY_PREVIOUS_CLOSE_STATUS_VERIFIED
    current_completed_count = len(completed_rows)
    baseline_volumes: list[float] = []
    for item in same_time_history:
        if not isinstance(item, dict):
            continue
        try:
            completed_count = int(item["completed_interval_count"])
            cumulative_volume = _positive_number(item["cumulative_volume_0900_to_as_of"], minimum=0.0)
        except (KeyError, TypeError, ValueError):
            continue
        if completed_count != current_completed_count:
            continue
        baseline_volumes.append(cumulative_volume)
    metrics["same_time_baseline_sample_count"] = len(baseline_volumes)
    if len(baseline_volumes) >= INTRADAY_SAME_TIME_BASELINE_MIN_COUNT:
        baseline_cumulative_volume = round(sum(baseline_volumes) / len(baseline_volumes), 8)
        metrics["same_time_baseline_cumulative_volume_0900_to_as_of"] = baseline_cumulative_volume
        if baseline_cumulative_volume > 0 and metrics["cumulative_volume_0900_to_as_of"] is not None:
            metrics["same_time_baseline_volume_ratio"] = round(metrics["cumulative_volume_0900_to_as_of"] / baseline_cumulative_volume, 8)
            metrics["same_time_baseline_status"] = INTRADAY_BASELINE_STATUS_READY
        else:
            metrics["same_time_baseline_status"] = INTRADAY_BASELINE_STATUS_INSUFFICIENT
    else:
        metrics["same_time_baseline_status"] = INTRADAY_BASELINE_STATUS_INSUFFICIENT
    market_context_status = INTRADAY_MARKET_CONTEXT_HOLD
    reason = "same_time_baseline_insufficient"
    if metrics["benchmark_return_pct"] is None or metrics["benchmark_excess_pct"] is None:
        market_context_status = INTRADAY_MARKET_CONTEXT_UNAVAILABLE
        reason = "benchmark_context_unavailable"
    elif metrics["current_minute_status"] != "in_progress":
        market_context_status = INTRADAY_MARKET_CONTEXT_HOLD
        reason = "current_minute_unusable"
    elif metrics["same_time_baseline_status"] != INTRADAY_BASELINE_STATUS_READY:
        market_context_status = INTRADAY_MARKET_CONTEXT_HOLD
        reason = "same_time_baseline_insufficient"
    elif metrics["previous_close_gap_status"] != INTRADAY_PREVIOUS_CLOSE_STATUS_VERIFIED:
        market_context_status = INTRADAY_MARKET_CONTEXT_HOLD
        reason = "previous_close_lineage_unavailable"
    else:
        market_context_status = INTRADAY_MARKET_CONTEXT_VERIFIED
        reason = "ok"
    return {"market_context_status": market_context_status, "reason": reason, "metrics": metrics, "units": _units(), "formulas": _formulas()}


def _units() -> dict:
    return {
        "open_price_krw": "KRW",
        "last_price_krw": "KRW",
        "stock_return_pct": "percent",
        "open_to_current_return_pct": "percent; (last_price_krw / open_price_krw - 1) * 100",
        "benchmark_return_pct": "percent; (benchmark_last_price_krw / benchmark_open_price_krw - 1) * 100",
        "benchmark_excess_pct": "percent; stock_return_pct - benchmark_return_pct",
        "previous_close_gap_pct": "percent; (last_price_krw / previous_close_krw - 1) * 100",
        "same_time_baseline_cumulative_volume_0900_to_as_of": "shares",
        "same_time_baseline_volume_ratio": "ratio; cumulative_volume_0900_to_as_of / same_time_baseline_cumulative_volume_0900_to_as_of",
        "minute_volumes": "shares",
        "cumulative_volume_0900_to_as_of": "shares",
        "latest_completed_interval_velocity": "shares per minute; last_completed_minute_volume - prior_completed_minute_volume",
        "latest_completed_interval_acceleration": "shares per minute per minute; latest_velocity - prior_velocity",
        "spread_pct": "percent; (best_ask - best_bid) / last_price_krw * 100",
        "top_of_book_imbalance": "ratio; (top_bid_qty - top_ask_qty) / (top_bid_qty + top_ask_qty)",
        "same_time_baseline_status": "enum",
        "previous_close_gap_status": "enum",
        "volume_window_status": "enum",
        "current_minute_status": "enum",
        "benchmark_context_status": "enum",
    }


def _formulas() -> dict:
    return {
        "stock_return_pct": "(current_last_price / open_price - 1) * 100",
        "benchmark_return_pct": "(benchmark_last_price / benchmark_open_price - 1) * 100",
        "benchmark_excess_pct": "stock_return_pct - benchmark_return_pct",
        "spread_pct": "(best_ask - best_bid) / last_price * 100",
        "top_of_book_imbalance": "(top_bid_qty - top_ask_qty) / (top_bid_qty + top_ask_qty)",
        "cumulative_volume_0900_to_as_of": "sum(minute_volume for completed rows between 09:00 and latest completed minute at or before as_of)",
        "same_time_baseline_cumulative_volume_0900_to_as_of": "average cumulative volume from prior same-minute completed observations",
        "same_time_baseline_volume_ratio": "current cumulative volume / same-time baseline cumulative volume",
        "latest_completed_interval_velocity": "completed_volume[n] - completed_volume[n-1]",
        "latest_completed_interval_acceleration": "(completed_volume[n] - completed_volume[n-1]) - (completed_volume[n-1] - completed_volume[n-2])",
        "previous_close_gap_pct": "(current_last_price / previous_close_krw - 1) * 100",
    }


def _computed_field(filter_result: dict, key: str) -> object | None:
    if not isinstance(filter_result, dict):
        return None
    computed = filter_result.get("computed_outputs")
    if isinstance(computed, dict):
        payload = computed.get("computed")
        if isinstance(payload, dict) and key in payload:
            return payload.get(key)
        if key in computed:
            return computed.get(key)
    return None


def resolve_intraday_lineage_context(*, card: dict, evidence: dict, filter_result: dict) -> tuple[str, float | None]:
    benchmark_symbol = _computed_field(filter_result, "benchmark_symbol")
    if benchmark_symbol is None and isinstance(card, dict):
        card_payload = card.get("card")
        if isinstance(card_payload, dict):
            benchmark_symbol = card_payload.get("benchmark_symbol")
    if benchmark_symbol is None and isinstance(evidence, dict):
        snapshot = evidence.get("snapshot")
        if isinstance(snapshot, dict):
            benchmark_symbol = snapshot.get("benchmark_symbol")
    if benchmark_symbol != INTRADAY_BENCHMARK_SYMBOL:
        raise HTTPException(409, "market context benchmark identity unavailable")

    previous_close = _computed_field(filter_result, "previous_close_krw")
    if previous_close is None and isinstance(card, dict):
        card_payload = card.get("card")
        if isinstance(card_payload, dict):
            previous_close = card_payload.get("previous_close_krw")
            if previous_close is None and isinstance(card_payload.get("market_context"), dict):
                previous_close = card_payload["market_context"].get("previous_close_krw")
    if previous_close is None and isinstance(evidence, dict):
        snapshot = evidence.get("snapshot")
        if isinstance(snapshot, dict):
            previous_close = snapshot.get("previous_close_krw")
    try:
        previous_close_value = None if previous_close is None else _positive_number(previous_close, minimum=0.0)
    except (TypeError, ValueError):
        previous_close_value = None
    return benchmark_symbol, previous_close_value


def _completed_stock_observation_count(detail: dict) -> int:
    observations = detail.get("observations") if isinstance(detail, dict) else []
    if not isinstance(observations, list):
        return 0
    count = 0
    for observation in observations:
        if not isinstance(observation, dict) or observation.get("series") != "stock" or observation.get("kind") != "BAR" or observation.get("completed") != 1:
            continue
        count += 1
    return count


def _cumulative_volume_from_detail(detail: dict) -> float:
    observations = detail.get("observations") if isinstance(detail, dict) else []
    if not isinstance(observations, list):
        raise ValueError("market context history unavailable")
    volume = 0.0
    for observation in observations:
        if not isinstance(observation, dict) or observation.get("series") != "stock" or observation.get("kind") != "BAR" or observation.get("completed") != 1:
            continue
        payload = observation.get("observation")
        if not isinstance(payload, dict):
            raise ValueError("market context history unavailable")
        volume += _positive_number(payload.get("minute_volume"), minimum=0.0)
    return round(volume, 8)


def same_time_history_for_symbol(db: sqlite3.Connection, *, symbol: str, requested_as_of: str, completed_interval_count: int) -> list[dict]:
    requested_minute = _requested_minute(parse_kst(requested_as_of))
    requested_date = requested_minute.date().isoformat()
    requested_time = requested_minute.time()
    history: list[dict] = []
    for row in db.execute(
        "SELECT id, requested_as_of, session_date, status FROM intraday_market_context_runs WHERE symbol=? AND session_date < ? ORDER BY session_date DESC, id DESC",
        (symbol, requested_date),
    ):
        try:
            candidate_minute = _requested_minute(parse_kst(row["requested_as_of"]))
        except (TypeError, ValueError):
            continue
        if candidate_minute.time() != requested_time:
            continue
        detail = market_context_run_detail(db, run_id=row["id"])
        if _completed_stock_observation_count(detail) != completed_interval_count:
            continue
        history.append(
            {
                "run_id": row["id"],
                "session_date": row["session_date"],
                "requested_as_of": row["requested_as_of"],
                "status": row["status"],
                "completed_interval_count": completed_interval_count,
                "cumulative_volume_0900_to_as_of": _cumulative_volume_from_detail(detail),
            }
        )
    return history


def _result_signature(card_id: int, evidence_id: int, filter_id: int, run_key: str, requested_as_of: str, source_topic: str) -> str:
    return hashlib.sha256(_canon({"card_id": card_id, "evidence_id": evidence_id, "filter_id": filter_id, "run_key": run_key, "requested_as_of": requested_as_of, "source_topic": source_topic}).encode()).hexdigest()


def persist_intraday_market_context_run(db: sqlite3.Connection, *, run_key: str, card: dict, evidence: dict, filter_result: dict, requested_as_of: str, stock_snapshot: IntradayMinuteSnapshot | None, benchmark_snapshot: IntradayMinuteSnapshot | None, orderbook: dict | None, result: dict, source_topic: str = INTRADAY_SOURCE_TOPIC) -> dict:
    signature = _result_signature(card["id"], evidence["id"], filter_result["id"], run_key, requested_as_of, source_topic)
    if db.in_transaction:
        db.rollback()
    db.execute("BEGIN IMMEDIATE")
    try:
        existing = db.execute("SELECT id,input_sha256 FROM intraday_market_context_runs WHERE run_key=?", (run_key,)).fetchone()
        if existing:
            if existing["input_sha256"] != signature:
                raise HTTPException(409, "market context run key conflict")
            return render_market_context_response(market_context_run_detail(db, run_id=existing["id"]), idempotent=True)
        stock_tr_id = stock_snapshot.tr_id if isinstance(stock_snapshot, IntradayMinuteSnapshot) else INTRADAY_MINUTE_TR_ID
        benchmark_tr_id = benchmark_snapshot.tr_id if isinstance(benchmark_snapshot, IntradayMinuteSnapshot) else INTRADAY_MINUTE_TR_ID
        row = db.execute(
            """INSERT INTO intraday_market_context_runs(
              run_key,source_topic,card_id,card_version,evidence_id,evidence_version,filter_id,filter_lineage_version,
              symbol,benchmark_symbol,requested_as_of,session_date,known_at,retrieved_at,provider,tr_id,benchmark_tr_id,
              schema_version,status,reason,input_sha256,result_json,created_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) RETURNING id""",
            (
                run_key,
                source_topic,
                card["id"],
                card["version"],
                evidence["id"],
                evidence.get("version", 1),
                filter_result["id"],
                filter_result.get("lineage_version", 1),
                evidence["symbol"],
                INTRADAY_BENCHMARK_SYMBOL,
                requested_as_of,
                requested_as_of[:10],
                result.get("known_at", requested_as_of),
                result.get("retrieved_at", requested_as_of),
                "KIS",
                stock_tr_id,
                benchmark_tr_id,
                INTRADAY_SCHEMA_VERSION,
                result["market_context_status"],
                result["reason"],
                signature,
                _canon(result),
                now_kst().isoformat(),
            ),
        ).fetchone()
        run_id = row["id"]
        _persist_observations(db, run_id, stock_snapshot=stock_snapshot, benchmark_snapshot=benchmark_snapshot, orderbook=orderbook)
        db.commit()
        return render_market_context_response(market_context_run_detail(db, run_id=run_id), idempotent=False)
    except Exception:
        if db.in_transaction:
            db.rollback()
        raise


def _persist_observations(db: sqlite3.Connection, run_id: int, *, stock_snapshot: IntradayMinuteSnapshot | None, benchmark_snapshot: IntradayMinuteSnapshot | None, orderbook: dict | None) -> None:
    if isinstance(orderbook, dict) and orderbook.get("status") == "ok":
        orderbook_observation = {
            "provider": "KIS",
            "source": "KIS",
            "source_kind": "orderbook",
            "symbol": orderbook["symbol"],
            "known_at": orderbook["quote_known_at"],
            "retrieved_at": orderbook["retrieved_at"],
            "last_price_krw": orderbook["last_price"],
            "best_bid_krw": orderbook["best_bid"],
            "best_ask_krw": orderbook["best_ask"],
            "top_bid_qty": orderbook["top_bid_qty"],
            "top_ask_qty": orderbook["top_ask_qty"],
            "spread_pct": round((orderbook["best_ask"] - orderbook["best_bid"]) / orderbook["last_price"] * 100, 8),
            "top_of_book_imbalance": 0.0 if orderbook["top_bid_qty"] + orderbook["top_ask_qty"] == 0 else round((orderbook["top_bid_qty"] - orderbook["top_ask_qty"]) / (orderbook["top_bid_qty"] + orderbook["top_ask_qty"]), 8),
        }
        db.execute(
            """INSERT INTO intraday_market_context_observations(
              run_id,series,kind,symbol,exchange_at,known_at,retrieved_at,provider,source,tr_id,schema_version,sequence,completed,observation_json
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (run_id, "orderbook", "ORDERBOOK", orderbook["symbol"], None, orderbook["quote_known_at"], orderbook["retrieved_at"], "KIS", "KIS", "FHKST01010200", INTRADAY_SCHEMA_VERSION, 0, 1, _canon(orderbook_observation)),
        )
    if isinstance(stock_snapshot, IntradayMinuteSnapshot):
        for index, row in enumerate(stock_snapshot.bars, start=1):
            db.execute(
                """INSERT INTO intraday_market_context_observations(
                  run_id,series,kind,symbol,exchange_at,known_at,retrieved_at,provider,source,tr_id,schema_version,sequence,completed,observation_json
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (run_id, "stock", "BAR", stock_snapshot.symbol, row["exchange_at"], row["known_at"], row["retrieved_at"], stock_snapshot.provider, stock_snapshot.source, stock_snapshot.tr_id, INTRADAY_SCHEMA_VERSION, index, int(row["completion_status"] == "completed"), _canon(row)),
            )
    if isinstance(benchmark_snapshot, IntradayMinuteSnapshot):
        for index, row in enumerate(benchmark_snapshot.bars, start=1):
            db.execute(
                """INSERT INTO intraday_market_context_observations(
                  run_id,series,kind,symbol,exchange_at,known_at,retrieved_at,provider,source,tr_id,schema_version,sequence,completed,observation_json
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (run_id, "benchmark", "BAR", benchmark_snapshot.symbol, row["exchange_at"], row["known_at"], row["retrieved_at"], benchmark_snapshot.provider, benchmark_snapshot.source, benchmark_snapshot.tr_id, INTRADAY_SCHEMA_VERSION, index, int(row["completion_status"] == "completed"), _canon(row)),
            )


def market_context_run_detail(db: sqlite3.Connection, *, run_key: str | None = None, run_id: int | None = None) -> dict:
    if run_key is None and run_id is None:
        raise ValueError("run_key or run_id required")
    row = db.execute(
        "SELECT * FROM intraday_market_context_runs WHERE " + ("run_key=?" if run_key is not None else "id=?") + " ORDER BY id DESC LIMIT 1",
        (run_key if run_key is not None else run_id,),
    ).fetchone()
    if not row:
        raise HTTPException(404, "market context run not found")
    result = dict(row)
    result["result"] = json.loads(result.pop("result_json"))
    result["observations"] = [
        dict(item)
        for item in db.execute(
            "SELECT series,kind,symbol,exchange_at,known_at,retrieved_at,provider,source,tr_id,schema_version,sequence,completed,observation_json FROM intraday_market_context_observations WHERE run_id=? ORDER BY id",
            (result["id"],),
        )
    ]
    for observation in result["observations"]:
        observation["observation"] = json.loads(observation.pop("observation_json"))
    result_payload = result.get("result")
    metrics = result_payload.get("metrics") if isinstance(result_payload, dict) else None
    if isinstance(metrics, dict):
        orderbook = next(
            (
                observation.get("observation")
                for observation in result["observations"]
                if observation.get("series") == "orderbook" and observation.get("kind") == "ORDERBOOK" and isinstance(observation.get("observation"), dict)
            ),
            None,
        )
        if isinstance(orderbook, dict):
            metrics["top_bid_qty"] = orderbook.get("top_bid_qty")
            metrics["top_ask_qty"] = orderbook.get("top_ask_qty")
            metrics["best_bid_krw"] = orderbook.get("best_bid_krw")
            metrics["best_ask_krw"] = orderbook.get("best_ask_krw")
            metrics["last_price_krw"] = orderbook.get("last_price_krw")
    return result


def render_market_context_response(detail: dict, *, idempotent: bool) -> dict:
    result = detail["result"]
    return {
        "id": detail["id"],
        "run_key": detail["run_key"],
        "card_id": detail["card_id"],
        "evidence_id": detail["evidence_id"],
        "filter_id": detail["filter_id"],
        "source_topic": detail["source_topic"],
        "symbol": detail["symbol"],
        "benchmark_symbol": detail["benchmark_symbol"],
        "requested_as_of": detail["requested_as_of"],
        "known_at": detail["known_at"],
        "retrieved_at": detail["retrieved_at"],
        "market_context_status": detail["status"],
        "reason": detail["reason"],
        "metrics": result["metrics"],
        "units": result["units"],
        "formulas": result["formulas"],
        "observations_count": len(detail["observations"]),
        "observations": detail["observations"],
        "result": result,
        "idempotent": idempotent,
    }


def latest_market_context_for_card(db: sqlite3.Connection, card_id: int) -> dict | None:
    row = db.execute("SELECT id FROM intraday_market_context_runs WHERE card_id=? ORDER BY id DESC LIMIT 1", (card_id,)).fetchone()
    if not row:
        return None
    return market_context_run_detail(db, run_id=row["id"])
