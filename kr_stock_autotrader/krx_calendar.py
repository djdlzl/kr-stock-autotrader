"""Versioned, source-bound, fail-closed KRX market-calendar authority."""
from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path


class CalendarError(ValueError):
    pass


DATA_PATH = Path(__file__).with_name("data") / "krx_market_calendar_v1.json"
_REQUIRED_SOURCE_KEYS = {"closure_set", "government_2026", "government_2026_crosscheck", "krx_special_2026_07_17", "closures"}


def _calendar() -> dict[int, frozenset[date]]:
    try:
        raw = json.loads(DATA_PATH.read_text(encoding="utf-8"))
        if set(raw) != {"schema_version", "source", "years"} or raw["schema_version"] != "krx-market-calendar-v1":
            raise CalendarError("invalid KRX calendar schema")
        source = raw["source"]
        if not isinstance(source, dict) or set(source) != _REQUIRED_SOURCE_KEYS:
            raise CalendarError("invalid KRX calendar provenance")
        provenance = {key: value for key, value in source.items() if key != "closures"}
        for key, value in provenance.items():
            required = {"publisher", "url", "claim", "observed_at"} if key == "closure_set" else {"publisher", "url", "claim"}
            if (not isinstance(value, dict) or set(value) != required
                    or any(not isinstance(item, str) or not item for item in value.values())):
                raise CalendarError("invalid KRX calendar provenance")
        closures = source["closures"]
        if not isinstance(closures, dict):
            raise CalendarError("invalid KRX calendar provenance")
        years = raw["years"]
        if not isinstance(years, dict) or not years:
            raise CalendarError("invalid KRX calendar years")
        result: dict[int, frozenset[date]] = {}
        all_dates: set[str] = set()
        for year_text, entries in years.items():
            if not (isinstance(year_text, str) and len(year_text) == 4 and year_text.isdigit() and isinstance(entries, list)):
                raise CalendarError("invalid KRX calendar year")
            parsed = [date.fromisoformat(value) for value in entries]
            if (any(item.year != int(year_text) or item.weekday() >= 5 for item in parsed)
                    or len(parsed) != len(set(parsed))):
                raise CalendarError("invalid, weekend, or duplicate KRX calendar date")
            result[int(year_text)] = frozenset(parsed)
            all_dates.update(entries)
        if set(closures) != all_dates:
            raise CalendarError("incomplete KRX calendar provenance")
        for day_text, authority_ids in closures.items():
            if (not isinstance(day_text, str) or not isinstance(authority_ids, list) or not authority_ids
                    or len(authority_ids) != len(set(authority_ids))
                    or any(item not in provenance for item in authority_ids)):
                raise CalendarError("invalid KRX calendar provenance")
        return result
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        if isinstance(exc, CalendarError):
            raise
        raise CalendarError("KRX calendar unavailable or malformed") from exc


def is_krx_business_date(day: date) -> bool:
    """Fail closed unless the date year is covered by versioned authority."""
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
    raise CalendarError("KRX calendar coverage unavailable for previous business date")


def admitted_backlog_dates(run_date: date) -> list[str]:
    """Return every calendar date after the prior trading session through run date."""
    if not is_krx_business_date(run_date):
        raise CalendarError("KRX market closed")
    previous = previous_krx_business_date(run_date)
    values = []
    cursor = previous + timedelta(days=1)
    while cursor <= run_date:
        values.append(cursor.strftime("%Y%m%d"))
        cursor += timedelta(days=1)
    return values
