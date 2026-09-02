"""Typed domain rules, all evaluated fail-closed."""
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from math import isclose
from zoneinfo import ZoneInfo

from .config import KR_HOLIDAYS

KST = ZoneInfo("Asia/Seoul")
KRX_REGULAR_OPEN = time(9, 0)
KRX_REGULAR_CLOSE = time(15, 30)
CONDITION_KINDS = {"deadline", "absolute_price", "relative_pct", "volume", "relative_volume"}
OPERATORS = {">=", "<="}


@dataclass(frozen=True)
class Quote:
    symbol: str
    price: float
    volume: float
    baseline_volume: float
    known_at: datetime


@dataclass(frozen=True)
class Condition:
    kind: str
    operator: str
    value: object


def now_kst() -> datetime:
    return datetime.now(KST)


def parse_kst(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed.replace(tzinfo=KST) if parsed.tzinfo is None else parsed.astimezone(KST)


def is_krx_business_date(day: date) -> bool:
    """KRX sessions configured for this runtime: weekdays excluding holidays."""
    return day.weekday() < 5 and day.isoformat() not in KR_HOLIDAYS


def previous_krx_business_date(day: date) -> date:
    """Return the strictly preceding configured KRX business date."""
    candidate = day - timedelta(days=1)
    while not is_krx_business_date(candidate):
        candidate -= timedelta(days=1)
    return candidate


def previous_krx_business_dates(day: date, count: int) -> list[date]:
    """Return ``count`` KRX sessions ending strictly before ``day``, newest first."""
    if count < 1:
        raise ValueError("business-date count must be positive")
    dates = []
    for _ in range(count):
        day = previous_krx_business_date(day)
        dates.append(day)
    return dates


def market_open(at: datetime) -> bool:
    local = at.astimezone(KST)
    minute = (local.hour, local.minute)
    return is_krx_business_date(local.date()) and (KRX_REGULAR_OPEN.hour, KRX_REGULAR_OPEN.minute) <= minute <= (KRX_REGULAR_CLOSE.hour, KRX_REGULAR_CLOSE.minute)


def fresh_quote(quote: Quote, server_now: datetime) -> bool:
    age = server_now - quote.known_at
    return timedelta(0) <= age <= timedelta(minutes=5)


def _compare(actual: float, operator: str, expected: float) -> bool:
    if operator == ">=":
        return actual > expected or isclose(actual, expected, rel_tol=1e-12, abs_tol=1e-9)
    return actual < expected or isclose(actual, expected, rel_tol=1e-12, abs_tol=1e-9)


def evaluate_conditions(conditions, combine: str, quote: Quote, buy_price: float, now: datetime | None = None):
    """Return hit and human-readable reasons; empty rules deliberately never sell."""
    server_now = now or now_kst()
    if not conditions or combine not in {"OR", "AND"} or not fresh_quote(quote, server_now):
        return False, []
    hits = []
    for condition in conditions:
        actual = None
        expected = condition.value
        if condition.kind == "absolute_price":
            actual = quote.price
        elif condition.kind == "relative_pct" and buy_price > 0:
            actual = (quote.price - buy_price) * 100 / buy_price
        elif condition.kind == "volume":
            actual = quote.volume
        elif condition.kind == "relative_volume" and quote.baseline_volume > 0:
            actual = quote.volume / quote.baseline_volume
        elif condition.kind == "deadline":
            try:
                hit = _compare(server_now.timestamp(), condition.operator, parse_kst(str(expected)).timestamp())
            except (TypeError, ValueError):
                hit = False
            if hit:
                hits.append(f"마감시각 {condition.operator} {expected}")
            continue
        try:
            hit = actual is not None and _compare(float(actual), condition.operator, float(expected))
        except (TypeError, ValueError):
            hit = False
        if hit:
            hits.append(f"{condition.kind} {condition.operator} {expected}")
    return (len(hits) == len(conditions) if combine == "AND" else bool(hits)), hits
