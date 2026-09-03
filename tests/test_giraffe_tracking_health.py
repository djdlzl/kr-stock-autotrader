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


def test_scenario_client_state_keeps_quote_healthy_but_blocks_decision_and_tears_down():
    from kr_stock_autotrader.ui import APP_HTML
    helpers = re.search(r"let scenarioPoll=.*?(?=async function showDetail)", APP_HTML, re.S).group(0)
    script = """
let timers=[], cleared=[], rendered=[]; let document={hidden:false,addEventListener:()=>{}};
let detail={hidden:false}; const $=selector=>selector==='#detail'?detail:null; const clearInterval=x=>cleared.push(x); const setInterval=(f,n)=>{const x={f,n};timers.push(x);return x};
const AbortController=class{constructor(){this.signal={};this.abort=()=>{}}};
const esc=x=>String(x??''); const details=(a,b)=>a+':'+b; const kst=x=>x||'';
""" + helpers + """
(async()=>{
scenarioHealth={state:'QUOTE_OK_CONTEXT_MISSING',failures:0,health:{quote_health:'HEALTHY',context_readiness:'BLOCKED',active_scenario_label:null},next:15,items:[{current:{},scenarios:[]}]};
const healthy=healthView()+gatesView(scenarioHealth.health);
const blockedGlow=scenarioView(scenarioHealth.items).includes('active-scenario');
openScenarioCard='7'; const api=async()=>{throw Error('QUOTE_UNAVAILABLE')}; await refreshScenario(7);
const failedGlow=scenarioView(scenarioHealth.items).includes('active-scenario');
scenarioPoll='poll';scenarioCountdown='countdown';stopScenarioPolling(true);
console.log(JSON.stringify({healthy,blockedGlow,failedGlow,failures:scenarioHealth.failures,state:scenarioHealth.state,poll:scenarioPoll,countdown:scenarioCountdown,cleared}));
})().catch(e=>{console.error(e);process.exit(1)});
"""
    output = subprocess.check_output(["node", "-e", script], text=True, env=os.environ).strip()
    state = json.loads(output)
    assert "호가 정상 추적" in state["healthy"]
    assert "시나리오 판단자료 부족" in state["healthy"]
    assert state["blockedGlow"] is False and state["failedGlow"] is False
    assert state["failures"] == 1
    assert state["state"] == "STOPPED" and state["poll"] is None and state["countdown"] is None
    assert state["cleared"] == ["poll", "countdown"]
