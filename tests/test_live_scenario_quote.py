"""Read-only current quote projection and compact BAD/BASE/GOOD UI contracts."""
from datetime import timedelta

from fastapi.testclient import TestClient

from kr_stock_autotrader import api, db as dbmod
from kr_stock_autotrader.decision_cards import create_evidence, save_card, save_filter
from tests.test_decision_card_invariants import AS_OF, card, raw


def _card_with_levels(monkeypatch, tmp_path):
    monkeypatch.setattr(dbmod, "DATABASE_PATH", str(tmp_path / "quote.db"))
    db = dbmod.connect()
    evidence = create_evidence(db, {"symbol":"005930","name":"삼성전자","kind":"공시","title":"실적","summary":"호재","source":"DART","source_url":"https://example.test/e","snapshot":{"safe":True},"dedupe_key":"quote-levels","known_at":"2026-08-31T09:00:00+09:00"})
    filt = save_filter(db, evidence["id"], raw(post_close_market={"pre_event_low":90,"pre_event_close":100,"event_window_high":110}), AS_OF, "2026-08-31T09:00:00+09:00")
    payload = card(evidence["id"], filt["id"], filter_verdict="PASS", verdict="관찰")
    payload["card"]["observation_scenarios"] = [
        {"label":"BAD","source_field":"market.pre_event_low","level_krw":90,"meaning":"하방 확인","action":"새 판단 보류","checks":"공시 재확인"},
        {"label":"BASE","source_field":"market.pre_event_close","level_krw":100,"meaning":"기준 확인","action":"기록 유지","checks":"가격 확인"},
        {"label":"GOOD","source_field":"market.event_window_high","level_krw":110,"meaning":"상방 확인","action":"근거 재검토","checks":"거래량 확인"},
    ]
    saved = save_card(db, payload)
    db.close()
    return saved["id"]


def _counts():
    db = dbmod.connect()
    try:
        return {table: db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in ("decision_cards", "event_scenario_observations", "order_plans", "order_fills", "positions", "audit_logs")}
    finally:
        db.close()


def test_current_quote_projection_compares_immutable_levels_and_never_writes(monkeypatch, tmp_path):
    card_id = _card_with_levels(monkeypatch, tmp_path)
    now = api.now_kst()
    monkeypatch.setattr(api.app.state, "kis_orderbook_provider", lambda symbol: {"status":"ok", "symbol":symbol, "last_price":105, "best_bid":104, "best_ask":106, "top_bid_qty":10, "top_ask_qty":10, "quote_known_at":now.isoformat(), "retrieved_at":now.isoformat(), "timestamp_source":"network_retrieved_at", "raw_secret":"never project"}, raising=False)
    api._quote_cache.clear()
    client = TestClient(api.app)
    assert client.post("/api/signup", json={"email":"quote-levels@example.test","password":"long-password"}).status_code == 200
    before = _counts()
    response = client.get(f"/api/cards/{card_id}/current-quote")
    assert response.status_code == 200
    payload = response.json()
    assert set(payload) == {"status", "symbol", "market_state", "freshness", "last_price_krw", "best_bid_krw", "best_ask_krw", "retrieved_at", "source", "comparisons"}
    assert payload["status"] == "ok" and payload["last_price_krw"] == 105.0 and payload["best_bid_krw"] == 104.0 and payload["best_ask_krw"] == 106.0
    assert payload["source"] == "KIS" and "raw_secret" not in str(payload)
    assert payload["comparisons"] == [
        {"label":"BAD", "level_krw":90.0, "difference_krw":15.0, "difference_pct":15/90*100, "comparison":"ABOVE"},
        {"label":"BASE", "level_krw":100.0, "difference_krw":5.0, "difference_pct":5.0, "comparison":"ABOVE"},
        {"label":"GOOD", "level_krw":110.0, "difference_krw":-5.0, "difference_pct":-5/110*100, "comparison":"BELOW"},
    ]
    assert _counts() == before


def test_current_quote_fails_closed_for_missing_levels_and_stale_quote(monkeypatch, tmp_path):
    card_id = _card_with_levels(monkeypatch, tmp_path)
    now = api.now_kst()
    monkeypatch.setattr(api.app.state, "kis_orderbook_provider", lambda symbol: {"status":"ok", "symbol":symbol, "last_price":105, "best_bid":104, "best_ask":106, "top_bid_qty":10, "top_ask_qty":10, "quote_known_at":(now - timedelta(minutes=6)).isoformat(), "retrieved_at":(now - timedelta(minutes=6)).isoformat(), "timestamp_source":"network_retrieved_at"}, raising=False)
    api._quote_cache.clear()
    client = TestClient(api.app)
    assert client.post("/api/signup", json={"email":"quote-stale@example.test","password":"long-password"}).status_code == 200
    stale = client.get(f"/api/cards/{card_id}/current-quote").json()
    assert stale["status"] == "unavailable" and stale["freshness"] == "UNAVAILABLE" and stale["comparisons"] == []
    db = dbmod.connect()
    db.execute("UPDATE decision_cards SET card_json='{}' WHERE id=?", (card_id,)); db.commit(); db.close()
    missing = client.get(f"/api/cards/{card_id}/current-quote").json()
    assert missing["status"] == "unavailable" and missing["comparisons"] == []


def test_dashboard_has_stacked_safe_scenario_blocks_and_bounded_quote_polling():
    html = open("kr_stock_autotrader/decision_card_app.html", encoding="utf-8").read()
    for marker in ("scenario-level-bad", "scenario-level-base", "scenario-level-good", "<strong>", "current-quote", "current-quote", "CURRENT_QUOTE_POLL_MS", "setTimeout", "clearTimeout", "AbortController", "current-quote"):
        assert marker in html
    assert "current-quote" in html and "마지막 조회" in html and "현재가 확인 불가" in html
    assert "scenario-observations" not in html.split("function beginQuoteTracking", 1)[-1]
