"""Closed, pre-market KIS daily-bar projection and deterministic filter inputs.

KIS contracts: domestic daily item chart (FHKST03010100) returns `output2`
with `stck_bsop_date`, `stck_clpr`, `acml_vol`, `acml_tr_pbmn`, and `hts_avls`.
`hts_avls` is in 100-million KRW units (KIS developer portal: 국내주식 일봉조회).
"""
from __future__ import annotations
import math
import os
from datetime import datetime
from typing import Callable
from .domain import KST, now_kst

BENCHMARK_SYMBOL = "229200"
MARKET_CAP_UNIT_KRW = 100_000_000.0


def _number(value: object, *, positive: bool = False) -> float:
    if isinstance(value, bool):
        raise ValueError("boolean number")
    value = float(value)
    if not math.isfinite(value) or (positive and value <= 0):
        raise ValueError("invalid number")
    return value


def _date(value: object):
    if not isinstance(value, str) or len(value) != 8 or not value.isdigit():
        raise ValueError("invalid daily bar date")
    return datetime.strptime(value, "%Y%m%d").date()


def _closed_bars(rows: object, as_of: datetime) -> list[dict]:
    if not isinstance(rows, list):
        raise ValueError("daily bars unavailable")
    result, seen = [], set()
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("malformed daily bar")
        day = _date(row.get("stck_bsop_date"))
        if day > as_of.astimezone(KST).date() or day in seen:
            raise ValueError("future or duplicate daily bar")
        # A same-calendar-day bar can be a live/partial provider row; it is not
        # usable at 08:00 and is deliberately ignored rather than back-filled.
        if day == as_of.astimezone(KST).date():
            continue
        seen.add(day)
        result.append({"day": day, "close": _number(row.get("stck_clpr"), positive=True), "volume": _number(row.get("acml_vol")), "value": _number(row.get("acml_tr_pbmn")), "market_cap": _number(row.get("hts_avls"), positive=True) * MARKET_CAP_UNIT_KRW})
    result.sort(key=lambda item: item["day"], reverse=True)
    if len(result) < 2:
        raise ValueError("two completed bars required")
    return result


def _unavailable(symbol: str, reason: str, retrieved: datetime | None = None) -> dict:
    out = {"symbol": symbol, "status": "unavailable", "source": "KIS", "environment": "production", "reason": reason}
    if retrieved:
        out["retrieved_at"] = retrieved.isoformat()
    return out


def build_premarket_snapshot(symbol: str, as_of: datetime, daily_bars: Callable[..., object]) -> dict:
    """Build only from completed sessions strictly before `as_of` KST date."""
    retrieved = now_kst()
    try:
        if not isinstance(symbol, str) or not (symbol.isdigit() and len(symbol) == 6) or as_of.tzinfo is None:
            raise ValueError("invalid request")
        stock, benchmark = _closed_bars(daily_bars(symbol, as_of), as_of), _closed_bars(daily_bars(BENCHMARK_SYMBOL, as_of), as_of)
        # Both latest completed bars must refer to the same trading session; otherwise do not compare returns.
        if stock[0]["day"] != benchmark[0]["day"]:
            raise ValueError("benchmark trading day mismatch")
        stock_return = round((stock[0]["close"] / stock[1]["close"] - 1) * 100, 8)
        benchmark_return = round((benchmark[0]["close"] / benchmark[1]["close"] - 1) * 100, 8)
        known_at = retrieved.isoformat()
        return {"symbol": symbol, "status": "ok", "source": "KIS", "environment": "production", "retrieved_at": known_at, "known_at": known_at,
                "trading_day": stock[0]["day"].isoformat(), "stock_return_pct": stock_return, "benchmark_symbol": BENCHMARK_SYMBOL,
                "benchmark_return_pct": benchmark_return, "trading_value_krw": stock[0]["value"], "market_cap_krw": stock[0]["market_cap"],
                "recent_rise_pct": stock_return, "pre_announcement_return_pct": None, "trading_status": "unknown",
                "gap_pct": None, "current_volume": None, "baseline_volume": None, "sector_return_pct": None,
                "observability": {"gap_pct": "not_yet_observable", "current_volume": "not_yet_observable", "baseline_volume": "not_yet_observable", "sector_return_pct": "unknown", "pre_announcement_return_pct": "unknown", "trading_status": "unknown"},
                "units": {"returns": "percent", "trading_value_krw": "KRW", "market_cap_krw": "KRW (hts_avls x 100,000,000)"}}
    except (TypeError, ValueError, KeyError):
        return _unavailable(symbol, "daily_bars_unavailable_or_invalid", retrieved)


def _threshold(name: str) -> float:
    value = os.getenv(name)
    if value is None:
        raise ValueError(f"missing {name}")
    return _number(value, positive=True)


def filter_inputs_from_snapshot(snapshot: object) -> dict:
    """No defaults: strategy thresholds are mandatory canonical environment config."""
    if not isinstance(snapshot, dict) or snapshot.get("status") != "ok":
        return {"market_data_status": "unavailable"}
    try:
        thresholds = {"min_trading_value": _threshold("GIRAFFE_MIN_TRADING_VALUE_KRW"), "min_market_cap": _threshold("GIRAFFE_MIN_MARKET_CAP_KRW"), "max_market_cap": _threshold("GIRAFFE_MAX_MARKET_CAP_KRW"),
                      "max_recent_rise_pct": _threshold("GIRAFFE_MAX_RECENT_RISE_PCT"), "max_gap_pct": _threshold("GIRAFFE_MAX_GAP_PCT"), "max_pre_return_pct": _threshold("GIRAFFE_MAX_PRE_RETURN_PCT")}
        if thresholds["min_market_cap"] > thresholds["max_market_cap"]:
            raise ValueError("invalid market cap range")
        out = {"market_data_known_at": snapshot["known_at"], "trading_status": snapshot["trading_status"], "trading_value": snapshot["trading_value_krw"], "market_cap": snapshot["market_cap_krw"],
               "stock_return_pct": snapshot["stock_return_pct"], "benchmark_return_pct": snapshot["benchmark_return_pct"], "recent_rise_pct": snapshot["recent_rise_pct"], "pre_announcement_return_pct": snapshot["pre_announcement_return_pct"],
               "gap_pct": snapshot.get("gap_pct"), "current_volume": snapshot.get("current_volume"), "baseline_volume": snapshot.get("baseline_volume"), "sector_return_pct": snapshot.get("sector_return_pct"),
               "observability": snapshot.get("observability", {}), **thresholds}
        return out
    except (KeyError, TypeError, ValueError):
        return {"market_data_status": "unavailable"}
