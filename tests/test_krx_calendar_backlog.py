from datetime import date
import pytest
from kr_stock_autotrader.krx_calendar import CalendarError, admitted_backlog_dates, is_krx_business_date

def test_monday_backlog_contains_weekend_and_run_date():
    assert admitted_backlog_dates(date(2026, 9, 7)) == ["20260905", "20260906", "20260907"]

def test_weekday_holiday_and_unsupported_year_fail_closed():
    assert not is_krx_business_date(date(2026, 10, 9))
    with pytest.raises(CalendarError): admitted_backlog_dates(date(2027, 1, 4))

def test_weekend_is_not_admitted():
    with pytest.raises(CalendarError): admitted_backlog_dates(date(2026, 9, 6))
