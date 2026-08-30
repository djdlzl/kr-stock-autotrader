from datetime import datetime
from zoneinfo import ZoneInfo

from app import Condition, Quote, evaluate_conditions, market_open

KST = ZoneInfo("Asia/Seoul")
NOW = datetime(2026, 8, 31, 10, 0, tzinfo=KST)


def q(**kw):
    values = dict(symbol="005930", price=70000, volume=1200, baseline_volume=1000, known_at=NOW)
    values.update(kw)
    return Quote(**values)


def test_market_boundary_weekday_only():
    assert market_open(datetime(2026, 8, 31, 9, 0, tzinfo=KST))
    assert market_open(datetime(2026, 8, 31, 15, 30, tzinfo=KST))
    assert not market_open(datetime(2026, 8, 31, 8, 59, tzinfo=KST))
    assert not market_open(datetime(2026, 8, 8, 10, 0, tzinfo=KST))


def test_conditions_units_and_fail_closed():
    assert evaluate_conditions([Condition("relative_pct", ">=", 5)], "OR", q(price=73500), 70000, NOW)[0]
    assert evaluate_conditions([Condition("relative_pct", "<=", -5)], "OR", q(price=66000), 70000, NOW)[0]
    assert evaluate_conditions([Condition("relative_volume", ">=", 1.1)], "OR", q(), 70000, NOW)[0]
    assert not evaluate_conditions([Condition("relative_volume", ">=", 1)], "OR", q(baseline_volume=0), 70000, NOW)[0]


def test_and_neutral_control_and_stale_quote():
    conditions = [Condition("absolute_price", ">=", 80000), Condition("volume", ">=", 1000)]
    assert not evaluate_conditions(conditions, "AND", q(), 70000, NOW)[0]
    assert not evaluate_conditions([Condition("absolute_price", ">=", 60000)], "OR", q(known_at=datetime(2026, 8, 31, 9, 0, tzinfo=KST)), 70000, now=datetime(2026, 8, 31, 10, 10, tzinfo=KST))[0]
