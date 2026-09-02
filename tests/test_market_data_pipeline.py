from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import tempfile
import pytest

KST = ZoneInfo("Asia/Seoul")


def bar(day, close, volume=10, value=1100, cap=25):
    return {"stck_bsop_date": day.strftime("%Y%m%d"), "stck_clpr": str(close), "acml_vol": str(volume), "acml_tr_pbmn": str(value), "hts_avls": str(cap)}


def history():
    day, out = datetime(2026, 9, 1).date(), []
    while len(out) < 30:
        if day.weekday() < 5:
            index = len(out)
            out.append(bar(day, 130 - index))
        day -= timedelta(days=1)
    return out


def thresholds(monkeypatch):
    for name, value in {"GIRAFFE_MIN_TRADING_VALUE_KRW":"1000", "GIRAFFE_MIN_MARKET_CAP_KRW":"2000000000", "GIRAFFE_MAX_MARKET_CAP_KRW":"3000000000", "GIRAFFE_MAX_RECENT_RISE_PCT":"30", "GIRAFFE_MAX_GAP_PCT":"10", "GIRAFFE_MAX_PRE_RETURN_PCT":"30"}.items():
        monkeypatch.setenv(name, value)


def test_real_producer_output_passes_0800_filter_after_close(monkeypatch):
    from kr_stock_autotrader.market_data import build_premarket_snapshot, filter_inputs_from_snapshot
    from kr_stock_autotrader.decision_cards import run_filter
    thresholds(monkeypatch)
    as_of = datetime(2026, 9, 2, 8, 0, tzinfo=KST)
    snapshot = build_premarket_snapshot("005930", as_of, lambda *_: history(), "2026-08-31T17:15:00+09:00")
    assert snapshot["status"] == "ok"
    assert snapshot["trading_status"] == "premarket_unverified"
    assert snapshot["pre_announcement_return_pct"] is not None
    assert snapshot["retrieved_at"] != snapshot["market_data_known_at"]
    assert snapshot["market_data_known_at"] == "2026-09-01T15:30:00+09:00"
    inputs = filter_inputs_from_snapshot(snapshot) | {"source":"dart", "announcement_at":"2026-08-31T17:15:00+09:00", "economic_terms":"verified"}
    result = run_filter(inputs, as_of.isoformat(), as_of.isoformat())
    assert result["verdict"] == "PASS", result["reasons"]


def test_before_market_announcement_excludes_announcement_date():
    from kr_stock_autotrader.market_data import build_premarket_snapshot
    as_of = datetime(2026, 9, 2, 8, tzinfo=KST)
    after = build_premarket_snapshot("005930", as_of, lambda *_: history(), "2026-08-31T17:15:00+09:00")
    before = build_premarket_snapshot("005930", as_of, lambda *_: history(), "2026-08-31T08:00:00+09:00")
    assert after["status"] == before["status"] == "ok"
    assert after["pre_announcement_return_pct"] != before["pre_announcement_return_pct"]
    assert after["horizons"]["stock_return_pct"] == "20 completed aligned sessions"


def test_historical_recovery_known_at_uses_latest_close_not_retrieval(monkeypatch):
    from kr_stock_autotrader import market_data
    from kr_stock_autotrader.decision_cards import run_filter
    thresholds(monkeypatch)
    monkeypatch.setattr(market_data, "now_kst", lambda: datetime(2026, 9, 3, 9, tzinfo=KST))
    as_of = datetime(2026, 9, 2, 8, tzinfo=KST)
    snapshot = market_data.build_premarket_snapshot("005930", as_of, lambda *_: history(), "2026-08-31T17:15:00+09:00")
    assert snapshot["retrieved_at"] == "2026-09-03T09:00:00+09:00"
    assert snapshot["market_data_known_at"] == "2026-09-01T15:30:00+09:00" <= as_of.isoformat()
    inputs = market_data.filter_inputs_from_snapshot(snapshot) | {"source":"dart", "announcement_at":"2026-08-31T17:15:00+09:00", "economic_terms":"verified"}
    assert run_filter(inputs, as_of.isoformat(), as_of.isoformat())["verdict"] == "PASS"


def test_only_explicit_premarket_status_bypasses_tradability_gate(monkeypatch):
    from kr_stock_autotrader.decision_cards import run_filter
    thresholds(monkeypatch)
    base = {"market_data_known_at":"2026-09-01T15:30:00+09:00", "trading_value":1100., "market_cap":2_500_000_000., "stock_return_pct":1., "benchmark_return_pct":1., "recent_rise_pct":1., "pre_announcement_return_pct":1., "gap_pct":None, "current_volume":None, "baseline_volume":None, "sector_return_pct":None, "source":"dart", "announcement_at":"2026-08-31T17:15:00+09:00", "economic_terms":"verified", "min_trading_value":1000., "min_market_cap":2_000_000_000., "max_market_cap":3_000_000_000., "max_recent_rise_pct":30., "max_gap_pct":10., "max_pre_return_pct":30., "observability":{"trading_status":"premarket_unverified", "gap_pct":"not_yet_observable", "current_volume":"not_yet_observable", "baseline_volume":"not_yet_observable", "sector_return_pct":"unknown"}}
    as_of = "2026-09-02T08:00:00+09:00"
    assert run_filter(base | {"trading_status":"premarket_unverified"}, as_of, as_of)["verdict"] == "PASS"
    for status in ("halted", "unknown"):
        result = run_filter(base | {"trading_status":status}, as_of, as_of)
        assert result["verdict"] == "FAIL" and "not tradable" in result["reasons"]


@pytest.mark.parametrize("provider", [lambda *_: (_ for _ in ()).throw(RuntimeError("credential secret")), lambda *_: {"bad":"response"}])
def test_snapshot_boundary_sanitizes_provider_failures(monkeypatch, provider):
    from kr_stock_autotrader.market_data import build_premarket_snapshot
    result = build_premarket_snapshot("005930", datetime(2026, 9, 2, 8, tzinfo=KST), provider)
    assert result["status"] == "unavailable" and result["reason"] == "daily_bars_unavailable_or_invalid"
    assert "secret" not in str(result)


def test_snapshot_api_missing_credentials_is_safe_unavailable(monkeypatch):
    from fastapi.testclient import TestClient
    from kr_stock_autotrader import api
    monkeypatch.setenv("INTERNAL_API_KEY", "test-key")
    monkeypatch.delenv("KIS_APP_KEY", raising=False); monkeypatch.delenv("KIS_APP_SECRET", raising=False)
    api._default_kis_client = None
    response = TestClient(api.app).post("/api/internal/market-snapshots/005930", headers={"X-Internal-API-Key":"test-key"}, json={"as_of":"2026-09-02T08:00:00+09:00"})
    assert response.status_code == 200 and response.json()["snapshot"]["status"] == "unavailable"
    assert "credentials" not in response.text.lower()


def test_material_3_after_close_0800_snapshot_filter_card_readback_persists(monkeypatch):
    from fastapi.testclient import TestClient
    from kr_stock_autotrader import api, db as dbmod
    thresholds(monkeypatch); monkeypatch.setenv("INTERNAL_API_KEY", "test-key")
    path = tempfile.mktemp(suffix=".db"); monkeypatch.setattr(dbmod, "DATABASE_PATH", path)
    api.app.state.kis_daily_bars_provider = lambda *_: history()
    client = TestClient(api.app); headers={"X-Internal-API-Key":"test-key"}
    evidence = client.post("/api/internal/evidence", headers=headers, json={"symbol":"005930", "kind":"disclosure", "title":"Material 3 after close", "summary":"x", "source":"dart", "source_url":"https://dart.fss.or.kr", "announcement_at":"2026-09-01T17:15:00+09:00", "collected_at":"2026-09-02T07:00:00+09:00", "known_at":"2026-09-01T17:15:00+09:00", "snapshot":{}, "dedupe_key":"material-3-after-close"}).json()
    as_of="2026-09-02T08:00:00+09:00"
    snapshot_response = client.post("/api/internal/market-snapshots/005930", headers=headers, json={"as_of":as_of, "announcement_at":evidence["announcement_at"]})
    assert snapshot_response.status_code == 200
    market = snapshot_response.json(); inputs = market["filter_inputs"] | {"source":evidence["source"], "announcement_at":evidence["announcement_at"], "economic_terms":"verified"}
    filtered = client.post("/api/internal/filters", headers=headers, json={"evidence_id":evidence["id"], "inputs":inputs, "as_of":as_of, "known_at":as_of})
    assert filtered.status_code == 200 and filtered.json()["verdict"] == "PASS"
    f = filtered.json()
    card = {"schema_version":1,"symbol":"005930","headline":"관찰","conclusion":"판단 보류","change":"after close disclosure","source_evidence":[{"id":str(evidence["id"]),"source":"dart","url":"https://dart.fss.or.kr"}],"source_urls":["https://dart.fss.or.kr"],"business_value":"unknown","certainty":"unknown","priced_in":"unknown","filter_verdict":"PASS","price_cap":None,"window":None,"max_amount":None,"max_qty":None,"stop_loss":None,"take_profit":None,"evidence_invalidation":None,"holding_until":None,"review_at":None,"false_positive":"unknown","unknowns":"execution price unknown","verdict":"판단 보류","confidence":0.5,"valid_until":None,"order_type":None,"split":[],"expires":None}
    saved = client.post("/api/internal/cards/results", headers=headers, json={"evidence_id":evidence["id"], "filter_id":f["id"], "model":"deterministic-test", "provider":"none", "card":card})
    assert saved.status_code == 200
    readback = client.get("/api/internal/cards/"+str(saved.json()["id"]), headers=headers)
    assert readback.status_code == 200 and readback.json()["filter_id"] == f["id"]
    db = dbmod.connect()
    try:
        counts = {table: db.execute(f"SELECT COUNT(*) n FROM {table}").fetchone()["n"] for table in ("order_plans", "order_fills", "positions", "order_events")}
        assert counts == {"order_plans":0,"order_fills":0,"positions":0,"order_events":0}
        assert db.execute("SELECT COUNT(*) n FROM sqlite_master WHERE type='table' AND name LIKE '%allocation%'").fetchone()["n"] == 0
    finally:
        db.close()
