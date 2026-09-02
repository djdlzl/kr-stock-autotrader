"""Fail-closed, pre-market KIS daily-bar projection and filter inputs.

The daily-chart projection uses only completed sessions.  Daily bars are known
at the completed 15:30 KST close, while KIS output1's market-cap summary is
known only at network retrieval.  Since both are used, the overall snapshot is
known no earlier than that retrieval and historical late replays fail closed.
"""
from __future__ import annotations
import math
import os
from datetime import datetime, time, timedelta
from typing import Callable
from .domain import KST, now_kst, parse_kst
from .kis_readonly import DailySnapshot

BENCHMARK_SYMBOL = "229200"
# KIS's official daily-chart sample has hts_avls=815363 for SK hynix alongside
# listed shares and price consistent with 81,536,300,000,000 KRW (100m KRW
# units); see DAILY_CHART_OFFICIAL_REFERENCE in kis_readonly.py and its fixture.
MARKET_CAP_UNIT_KRW = 100_000_000.0
RETURN_HORIZON_SESSIONS = 20
KST_CASH_CLOSE = time(15, 30)


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
    as_of_day = as_of.astimezone(KST).date()
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("malformed daily bar")
        day = _date(row.get("stck_bsop_date"))
        # This producer is strictly the 08:00 KST path.  A current-date row is
        # partial/ambiguous there, so its presence is a bad provider selection,
        # not a row we may silently discard.
        if day >= as_of_day or day in seen:
            raise ValueError("same-day, future, or duplicate daily bar")
        seen.add(day)
        result.append({"day": day, "close": _number(row.get("stck_clpr"), positive=True),
                       "volume": _number(row.get("acml_vol")), "value": _number(row.get("acml_tr_pbmn"))})
    result.sort(key=lambda item: item["day"], reverse=True)
    if len(result) < RETURN_HORIZON_SESSIONS + 1:
        raise ValueError("insufficient completed daily bars for 20-session horizon")
    return result


def _aligned(stock: list[dict], benchmark: list[dict]) -> list[tuple[dict, dict]]:
    benchmark_by_day = {item["day"]: item for item in benchmark}
    shared = [(item, benchmark_by_day[item["day"]]) for item in stock if item["day"] in benchmark_by_day]
    if len(shared) < RETURN_HORIZON_SESSIONS + 1:
        raise ValueError("insufficient aligned 20-session history")
    return shared


def _return(pair_rows: list[tuple[dict, dict]]) -> tuple[float, float]:
    latest, prior = pair_rows[0], pair_rows[RETURN_HORIZON_SESSIONS]
    return (round((latest[0]["close"] / prior[0]["close"] - 1) * 100, 8),
            round((latest[1]["close"] / prior[1]["close"] - 1) * 100, 8))


def _announcement_end(rows: list[tuple[dict, dict]], announcement_at: datetime) -> list[tuple[dict, dict]]:
    announcement = announcement_at.astimezone(KST)
    # After the 15:30 completed close, that date is permitted.  Before/during
    # market announcement data must stop at the preceding completed session.
    include_date = announcement.time() >= KST_CASH_CLOSE
    eligible = [row for row in rows if row[0]["day"] <= announcement.date()] if include_date else [row for row in rows if row[0]["day"] < announcement.date()]
    if len(eligible) < RETURN_HORIZON_SESSIONS + 1:
        raise ValueError("insufficient announcement-boundary history")
    return eligible


def _known_at(day) -> datetime:
    return datetime.combine(day, KST_CASH_CLOSE, tzinfo=KST)


def _unavailable(symbol: str, reason: str, retrieved: datetime | None = None) -> dict:
    out = {"symbol": symbol, "status": "unavailable", "source": "KIS", "environment": "production", "reason": reason}
    if retrieved:
        out["retrieved_at"] = retrieved.isoformat()
    return out


def build_premarket_snapshot(symbol: str, as_of: datetime, daily_snapshot: Callable[..., object], announcement_at: str | None = None) -> dict:
    """Build a 20-session, aligned snapshot from completed sessions only."""
    retrieved = now_kst()
    try:
        if not isinstance(symbol, str) or not (symbol.isdigit() and len(symbol) == 6) or as_of.tzinfo is None:
            raise ValueError("invalid request")
        announcement = parse_kst(announcement_at) if announcement_at is not None else None
        stock_source = daily_snapshot(symbol, as_of)
        benchmark_source = daily_snapshot(BENCHMARK_SYMBOL, as_of)
        if not isinstance(stock_source, DailySnapshot) or not isinstance(benchmark_source, DailySnapshot):
            raise ValueError("daily snapshot contract invalid")
        # Summary hts_avls is a retrieval-time observation, never a bar value.
        retrieved = max(stock_source.retrieved_at, benchmark_source.retrieved_at)
        if retrieved.tzinfo is None or retrieved > as_of.astimezone(KST):
            raise ValueError("summary retrieval is after requested as_of")
        market_cap = _number(stock_source.summary_market_cap_100m, positive=True) * MARKET_CAP_UNIT_KRW
        stock = _closed_bars(list(stock_source.bars), as_of)
        benchmark = _closed_bars(list(benchmark_source.bars), as_of)
        aligned = _aligned(stock, benchmark)
        bars_known = _known_at(aligned[0][0]["day"])
        if bars_known > as_of.astimezone(KST):
            raise ValueError("completed market data is not known at as_of")
        stock_return, benchmark_return = _return(aligned)
        pre_return = _return(_announcement_end(aligned, announcement))[0] if announcement else None
        observability = {"gap_pct": "not_yet_observable", "current_volume": "not_yet_observable",
                         "baseline_volume": "not_yet_observable", "sector_return_pct": "unknown",
                         "trading_status": "premarket_unverified"}
        if announcement is None:
            observability["pre_announcement_return_pct"] = "unknown"
        return {"symbol": symbol, "status": "ok", "source": "KIS", "environment": "production",
                "retrieved_at": retrieved.isoformat(), "known_at": retrieved.isoformat(),
                "daily_bars_known_at": bars_known.isoformat(), "summary_known_at": retrieved.isoformat(),
                "market_data_known_at": retrieved.isoformat(), "trading_day": aligned[0][0]["day"].isoformat(),
                "stock_return_pct": stock_return, "benchmark_symbol": BENCHMARK_SYMBOL,
                "benchmark_return_pct": benchmark_return, "trading_value_krw": aligned[0][0]["value"],
                "market_cap_krw": market_cap, "recent_rise_pct": stock_return,
                "pre_announcement_return_pct": pre_return, "trading_status": "premarket_unverified",
                "gap_pct": None, "current_volume": None, "baseline_volume": None, "sector_return_pct": None,
                "observability": observability,
                "horizons": {"stock_return_pct": "20 completed aligned sessions", "benchmark_return_pct": "20 completed aligned sessions", "recent_rise_pct": "20 completed aligned sessions", "pre_announcement_return_pct": "20 completed aligned sessions ending at announcement boundary"},
                "units": {"returns": "percent", "trading_value_krw": "KRW", "market_cap_krw": "KRW (hts_avls x 100,000,000)"}}
    except Exception:
        # Provider/OAuth/HTTP/malformed responses are all intentionally collapsed.
        return _unavailable(symbol, "daily_bars_unavailable_or_invalid", retrieved)


def _threshold(name: str) -> float:
    value = os.getenv(name)
    if value is None:
        raise ValueError(f"missing {name}")
    return _number(value, positive=True)


def filter_inputs_from_snapshot(snapshot: object) -> dict:
    """No defaults: strategy thresholds are mandatory canonical environment config."""
    if not isinstance(snapshot, dict) or snapshot.get("status") != "ok":
        # This is failure-attempt provenance, never a claim that market data was
        # observed.  The snapshot owns retrieved_at, so provider errors cannot
        # smuggle arbitrary timestamps into a persisted filter.
        try:
            attempted_at = parse_kst(snapshot.get("retrieved_at"))
        except (ValueError, TypeError):
            return {"market_data_status": "unavailable"}
        return {"market_data_status": "unavailable", "market_data_attempted_at": attempted_at.isoformat()}
    try:
        thresholds = {"min_trading_value": _threshold("GIRAFFE_MIN_TRADING_VALUE_KRW"), "min_market_cap": _threshold("GIRAFFE_MIN_MARKET_CAP_KRW"), "max_market_cap": _threshold("GIRAFFE_MAX_MARKET_CAP_KRW"), "max_recent_rise_pct": _threshold("GIRAFFE_MAX_RECENT_RISE_PCT"), "max_gap_pct": _threshold("GIRAFFE_MAX_GAP_PCT"), "max_pre_return_pct": _threshold("GIRAFFE_MAX_PRE_RETURN_PCT")}
        if thresholds["min_market_cap"] > thresholds["max_market_cap"]:
            raise ValueError("invalid market cap range")
        return {"market_data_known_at": snapshot["market_data_known_at"], "trading_status": snapshot["trading_status"], "trading_value": snapshot["trading_value_krw"], "market_cap": snapshot["market_cap_krw"], "stock_return_pct": snapshot["stock_return_pct"], "benchmark_return_pct": snapshot["benchmark_return_pct"], "recent_rise_pct": snapshot["recent_rise_pct"], "pre_announcement_return_pct": snapshot["pre_announcement_return_pct"], "gap_pct": snapshot.get("gap_pct"), "current_volume": snapshot.get("current_volume"), "baseline_volume": snapshot.get("baseline_volume"), "sector_return_pct": snapshot.get("sector_return_pct"), "observability": snapshot.get("observability", {}), **thresholds}
    except (KeyError, TypeError, ValueError):
        try:
            attempted_at = parse_kst(snapshot.get("retrieved_at"))
        except (ValueError, TypeError):
            return {"market_data_status": "unavailable"}
        return {"market_data_status": "unavailable", "market_data_attempted_at": attempted_at.isoformat()}
