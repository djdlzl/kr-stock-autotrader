"""Focused contracts for the safe scenario tracking health projection."""

from kr_stock_autotrader.api import _tracking_health
from kr_stock_autotrader.domain import now_kst


def test_kis_only_tracking_health_is_closed_and_honest():
    timestamp = now_kst().isoformat()
    quote = {
        "source": "KIS", "timestamp_source": "network_retrieved_at",
        "retrieved_at": timestamp, "quote_known_at": timestamp,
        "last_price": 100.0, "best_bid": 99.0, "best_ask": 101.0,
        "top_bid_qty": 10.0, "top_ask_qty": 8.0, "spread_pct": 2.0,
        "imbalance": 1 / 9, "market_context_status": "UNAVAILABLE",
    }
    result = {"match": "OUT_OF_RANGE", "action": "NO_ACTION", "active_scenario_label": None}
    health = _tracking_health(quote, result)
    assert set(health) == {
        "status", "last_attempted_at", "last_success_at", "quote_age_seconds", "freshness",
        "source", "timestamp_source", "quote_known_at", "price_krw", "best_bid", "best_ask",
        "top_bid_qty", "top_ask_qty", "spread_pct", "imbalance", "market_context_status",
        "match", "action", "active_scenario_label", "gate_results",
    }
    assert health["status"] == "QUOTE_OK_CONTEXT_MISSING"
    gates = {item["gate"]: item for item in health["gate_results"]}
    assert gates["quote_validity_freshness"]["status"] == "PASS"
    assert gates["market_context"]["status"] == "BLOCKED"
    assert gates["price_range"]["status"] == "NOT_EVALUATED"
    assert gates["market_sector_volume"]["status"] == "NOT_EVALUATED"
    assert gates["spread_imbalance"]["role"] == "integrity_context_not_label_selector"


def test_tracking_health_business_invalidation_is_authoritative():
    timestamp = now_kst().isoformat()
    quote = {"source": "KIS", "timestamp_source": "network_retrieved_at", "retrieved_at": timestamp,
             "quote_known_at": timestamp, "last_price": 100, "best_bid": 99, "best_ask": 101,
             "top_bid_qty": 1, "top_ask_qty": 1, "spread_pct": 2, "imbalance": 0,
             "market_context_status": "VERIFIED"}
    health = _tracking_health(quote, {"match": "OUT_OF_RANGE", "action": "EXIT_REVIEW", "active_scenario_label": None}, invalidated=True)
    assert health["status"] == "TRACKING_STOPPED"
    assert next(x for x in health["gate_results"] if x["gate"] == "business_invalidation")["status"] == "BLOCKED"
