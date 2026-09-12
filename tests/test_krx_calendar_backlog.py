import json
from datetime import date

import pytest

from kr_stock_autotrader import krx_calendar
from kr_stock_autotrader.krx_calendar import CalendarError, admitted_backlog_dates, is_krx_business_date, previous_krx_business_date


def test_monday_backlog_contains_weekend_and_run_date():
    assert admitted_backlog_dates(date(2026, 9, 7)) == ["20260905", "20260906", "20260907"]


def test_weekday_holiday_extends_backlog_through_first_business_day():
    assert admitted_backlog_dates(date(2026, 9, 28)) == [
        "20260924", "20260925", "20260926", "20260927", "20260928",
    ]


@pytest.mark.parametrize("closed", [date(2026, 5, 1), date(2026, 7, 17), date(2026, 12, 31)])
def test_krx_rule_and_special_closures_are_not_admitted(closed):
    assert not is_krx_business_date(closed)
    with pytest.raises(CalendarError, match="KRX market closed"):
        admitted_backlog_dates(closed)


def test_july_special_closure_extends_next_business_backlog():
    assert admitted_backlog_dates(date(2026, 7, 20)) == ["20260717", "20260718", "20260719", "20260720"]


def test_weekend_is_not_admitted_and_unsupported_year_fails_closed():
    with pytest.raises(CalendarError, match="KRX market closed"):
        admitted_backlog_dates(date(2026, 9, 6))
    with pytest.raises(CalendarError, match="coverage unavailable"):
        admitted_backlog_dates(date(2027, 1, 4))


def test_previous_business_date_crossing_unsupported_year_fails_closed_boundedly():
    with pytest.raises(CalendarError, match="previous business date"):
        previous_krx_business_date(date(2026, 1, 1))


def test_calendar_rejects_malformed_duplicate_year_mismatch_missing_year_and_weekend_entries(monkeypatch, tmp_path):
    valid = json.loads(krx_calendar.DATA_PATH.read_text())
    cases = {
        "malformed": {"schema_version": "wrong", "source": valid["source"], "years": valid["years"]},
        "duplicate": {**valid, "years": {"2026": ["2026-05-01", "2026-05-01"]}},
        "year_mismatch": {**valid, "years": {"2026": ["2025-12-31"]}},
        "weekend": {**valid, "years": {"2026": ["2026-09-26"]}},
        "missing_year": {**valid, "years": {"2025": ["2025-01-01"]}},
    }
    for name, payload in cases.items():
        path = tmp_path / f"{name}.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        monkeypatch.setattr(krx_calendar, "DATA_PATH", path)
        with pytest.raises(CalendarError):
            is_krx_business_date(date(2026, 9, 7))


def test_calendar_requires_authoritative_per_date_provenance():
    raw = json.loads(krx_calendar.DATA_PATH.read_text())
    assert raw["source"]["closure_set"]["publisher"] == "Korea Exchange (KRX)"
    assert "krx_special_2026_07_17" in raw["source"]["closures"]["2026-07-17"]
    assert set(raw["source"]["closures"]) == set(raw["years"]["2026"])
