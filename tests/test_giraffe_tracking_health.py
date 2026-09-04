"""Endpoint and client-state regression tests for safe scenario tracking."""
import json
import os
import re
import subprocess

from fastapi.testclient import TestClient

from kr_stock_autotrader import db as dbmod
from tests.test_event_scenarios import _lineage, payload


def _fresh_book(symbol):
    from kr_stock_autotrader.domain import now_kst
    timestamp = now_kst().isoformat()
    return {"status": "ok", "symbol": symbol, "last_price": 100.0, "best_bid": 99.0,
            "best_ask": 101.0, "top_bid_qty": 10.0, "top_ask_qty": 8.0,
            "quote_known_at": timestamp, "retrieved_at": timestamp,
            "timestamp_source": "network_retrieved_at", "provider_secret": "never-project"}


def _authenticated_scenario_client(monkeypatch, tmp_path):
    monkeypatch.setattr(dbmod, "DATABASE_PATH", str(tmp_path / "tracking.db"))
    monkeypatch.setenv("INTERNAL_API_KEY", "tracking-key")
    from app import app
    client = TestClient(app)
    assert client.post("/api/signup", json={"email": "tracking@test.com", "password": "long-password"}).status_code == 200
    db = dbmod.connect(); evidence_id, card_id = _lineage(db); db.close()
    created = client.post("/api/internal/scenario-sets", json=payload(evidence_id, card_id), headers={"X-Internal-API-Key": "tracking-key"})
    assert created.status_code == 200, created.text
    return app, client, card_id


def test_authenticated_tracking_health_get_is_closed_and_read_only(monkeypatch, tmp_path):
    app, client, card_id = _authenticated_scenario_client(monkeypatch, tmp_path)
    app.state.kis_orderbook_provider = _fresh_book
    db = dbmod.connect()
    before = db.execute("SELECT COUNT(*) FROM event_scenario_observations").fetchone()[0]
    db.close()
    responses = [client.get(f"/api/cards/{card_id}/tracking-health") for _ in range(2)]
    assert all(response.status_code == 200 for response in responses)
    body = responses[0].json()
    assert set(body) == {"tracking_health"}
    health = body["tracking_health"]
    assert set(health) == {"quote_health", "context_readiness", "status", "last_attempted_at", "last_success_at", "quote_age_seconds", "freshness", "source", "timestamp_source", "quote_known_at", "price_krw", "best_bid", "best_ask", "top_bid_qty", "top_ask_qty", "spread_pct", "imbalance", "market_context_status", "match", "action", "active_scenario_label", "gate_results"}
    assert {key: health[key] for key in ("quote_health", "context_readiness", "match", "action", "active_scenario_label")} == {"quote_health": "HEALTHY", "context_readiness": "BLOCKED", "match": "OUT_OF_RANGE", "action": "NO_ACTION", "active_scenario_label": None}
    db = dbmod.connect()
    after = db.execute("SELECT COUNT(*) FROM event_scenario_observations").fetchone()[0]
    db.close()
    assert (before, after) == (0, 0)


def test_tracking_health_get_failure_is_sanitized_and_read_only(monkeypatch, tmp_path):
    app, client, card_id = _authenticated_scenario_client(monkeypatch, tmp_path)
    app.state.kis_orderbook_provider = lambda _: (_ for _ in ()).throw(RuntimeError("KIS-SECRET-token"))
    response = client.get(f"/api/cards/{card_id}/tracking-health")
    assert response.status_code == 503
    assert response.json() == {"detail": {"code": "QUOTE_UNAVAILABLE", "message": "호가를 안전하게 확인하지 못했습니다"}}
    assert "SECRET" not in response.text and "token" not in response.text
    db = dbmod.connect()
    assert db.execute("SELECT COUNT(*) FROM event_scenario_observations").fetchone()[0] == 0
    db.close()


def test_tracking_health_get_stopped_scenario_is_sanitized_conflict(monkeypatch, tmp_path):
    app, client, card_id = _authenticated_scenario_client(monkeypatch, tmp_path)
    app.state.kis_orderbook_provider = _fresh_book
    db = dbmod.connect()
    db.execute("UPDATE decision_cards SET invalidated_at=? WHERE id=?", ("2026-09-03T12:00:00+09:00", card_id))
    db.commit()
    db.close()
    response = client.get(f"/api/cards/{card_id}/tracking-health")
    assert response.status_code == 409
    assert response.json() == {"detail": {"code": "TRACKING_STOPPED", "message": "추적할 현재 시나리오가 없습니다"}}
    db = dbmod.connect()
    assert db.execute("SELECT COUNT(*) FROM event_scenario_observations").fetchone()[0] == 0
    db.close()


def test_authenticated_kis_poll_exposes_closed_dual_health_and_safe_card(monkeypatch, tmp_path):
    app, client, card_id = _authenticated_scenario_client(monkeypatch, tmp_path)
    app.state.kis_orderbook_provider = _fresh_book
    response = client.post(f"/api/cards/{card_id}/scenario-observations", json={})
    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == {"tracking_health", "observation"}
    health = body["tracking_health"]
    assert set(health) == {"quote_health", "context_readiness", "status", "last_attempted_at", "last_success_at", "quote_age_seconds", "freshness", "source", "timestamp_source", "quote_known_at", "price_krw", "best_bid", "best_ask", "top_bid_qty", "top_ask_qty", "spread_pct", "imbalance", "market_context_status", "match", "action", "active_scenario_label", "gate_results"}
    assert health["quote_health"] == "HEALTHY"
    assert health["top_bid_qty"] == 10.0 and health["top_ask_qty"] == 8.0
    assert health["context_readiness"] == "BLOCKED"
    assert health["status"] == "QUOTE_OK_CONTEXT_MISSING"
    assert health["active_scenario_label"] is None and health["action"] == "NO_ACTION"
    gates = {item["gate"]: item for item in health["gate_results"]}
    assert gates["quote_validity_freshness"]["label"] == "호가 유효성"
    assert gates["market_context"]["label"] == "시장 자료"
    assert gates["market_context"]["display_status"] == "차단"
    detail = client.get(f"/api/cards/{card_id}")
    assert detail.status_code == 200
    current = detail.json()["event_scenarios"][0]["current"]
    assert set(current) == {"known_at", "retrieved_at", "provider", "source", "timestamp_source", "price_krw", "best_bid", "best_ask", "volume_ratio", "benchmark_excess_pct", "sector_excess_pct", "market_context_status", "spread_pct", "imbalance", "match", "action", "active_scenario_label"}
    assert "source_receipt" not in current and "material_hash" not in json.dumps(current)


def test_authenticated_poll_provider_exception_is_fixed_sanitized_503(monkeypatch, tmp_path):
    app, client, card_id = _authenticated_scenario_client(monkeypatch, tmp_path)
    app.state.kis_orderbook_provider = lambda _: (_ for _ in ()).throw(RuntimeError("KIS-SECRET-token"))
    response = client.post(f"/api/cards/{card_id}/scenario-observations", json={})
    assert response.status_code == 503
    assert response.json() == {"detail": {"code": "QUOTE_UNAVAILABLE", "message": "호가를 안전하게 확인하지 못했습니다"}}
    assert "SECRET" not in response.text and "token" not in response.text


def test_tracking_health_malformed_timestamp_is_closed_not_raised():
    from kr_stock_autotrader.api import _tracking_health
    health = _tracking_health({"retrieved_at": "not-a-timestamp"}, {"match": "OUT_OF_RANGE", "action": "NO_ACTION", "active_scenario_label": None})
    assert health["quote_health"] == "UNAVAILABLE"
    assert health["context_readiness"] == "BLOCKED"
    assert health["status"] == "QUOTE_UNAVAILABLE"


def test_release0_scenario_highlight_requires_explicit_server_trust_projection():
    from kr_stock_autotrader.ui import APP_HTML
    assert "trust.status==='READY'&&trust.freshness==='FRESH'&&trust.context_readiness==='READY'" in APP_HTML
    assert "current.market_context_status==='VERIFIED'" in APP_HTML
    assert "['GOOD_MATCH','BASE_MATCH','BAD_MATCH'].includes(current.match)" in APP_HTML
