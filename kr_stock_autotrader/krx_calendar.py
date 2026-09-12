"""Versioned fail-closed KRX market-calendar authority."""
from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path


class CalendarError(ValueError):
    pass


DATA_PATH = Path(__file__).with_name("data") / "krx_market_calendar_v1.json"
_REQUIRED_SOURCE_KEYS = {"rule_source", "dated_closure_source"}


def _calendar() -> dict[int, frozenset[date]]:
    try:
        raw = json.loads(DATA_PATH.read_text(encoding="utf-8"))
        if set(raw) != {"schema_version", "source", "years"} or raw["schema_version"] != "krx-market-calendar-v1":
            raise CalendarError("invalid KRX calendar schema")
        source = raw["source"]
        if not isinstance(source, dict) or set(source) != _REQUIRED_SOURCE_KEYS:
            raise CalendarError("invalid KRX calendar provenance")
        for value in source.values():
            if not isinstance(value, dict) or not all(isinstance(item, str) and item for item in value.values()):
                raise CalendarError("invalid KRX calendar provenance")
        years = raw["years"]
        if not isinstance(years, dict) or not years:
            raise CalendarError("invalid KRX calendar years")
        result = {}
        for year_text, entries in years.items():
            if not (isinstance(year_text, str) and len(year_text) == 4 and year_text.isdigit() and isinstance(entries, list)):
                raise CalendarError("invalid KRX calendar year")
            parsed = [date.fromisoformat(value) for value in entries]
            if (any(item.year != int(year_text) or item.weekday() >= 5 for item in parsed)
                    or len(parsed) != len(set(parsed))):
                raise CalendarError("invalid, weekend, or duplicate KRX calendar date")
            result[int(year_text)] = frozenset(parsed)
        return result
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        if isinstance(exc, CalendarError):
            raise
        raise CalendarError("KRX calendar unavailable or malformed") from exc


def is_krx_business_date(day: date) -> bool:
    """Fail closed unless the date year is covered by the versioned authority."""
    holidays = _calendar().get(day.year)
    if holidays is None:
        raise CalendarError("KRX calendar coverage unavailable")
    return day.weekday() < 5 and day not in holidays


def previous_krx_business_date(day: date) -> date:
    """Return the prior session without searching beyond calendar coverage."""
    candidate = day - timedelta(days=1)
    while candidate.year == day.year:
        if is_krx_business_date(candidate):
            return candidate
        candidate -= timedelta(days=1)
    # Do not infer an unlisted prior year or continue an unbounded search.
    raise CalendarError("KRX calendar coverage unavailable for previous business date")


def admitted_backlog_dates(run_date: date) -> list[str]:
    if not is_krx_business_date(run_date):
        raise CalendarError("KRX market closed")
    previous = previous_krx_business_date(run_date)
    values = []
    cursor = previous + timedelta(days=1)
    while cursor <= run_date:
        values.append(cursor.strftime("%Y%m%d"))
        cursor += timedelta(days=1)
    return values
