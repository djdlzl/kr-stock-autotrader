import tempfile
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from kr_stock_autotrader import db as dbmod
from kr_stock_autotrader.decision_cards import (
    create_evidence, evaluate_order_plan, run_filter, save_card, save_filter,
    user_decision,
)

KST = ZoneInfo("Asia/Seoul")
AS_OF = "2026-08-31T10:00:00+09:00"

@pytest.fixture
def db(monkeypatch):
    path = tempfile.mktemp(suffix=".db")
    monkeypatch.setattr(dbmod, "DATABASE_PATH", path)
    d = dbmod.connect()
    yield d
    d.close()

def raw(**override):
    result = {
        "trading_status": "tradable", "trading_value": 100_000_000,
        "market_cap": 1_000_000_000, "stock_return_pct": 12.0,
        "benchmark_return_pct": 2.0, "sector_return_pct": 4.0,
        "current_volume": 200, "baseline_volume": 100,
        "recent_rise_pct": 10, "gap_pct": 2, "pre_announcement_return_pct": 3,
        "source": "dart", "announcement_at": "2026-08-31T09:00:00+09:00",
        "economic_terms": "contract value KRW 100m", "market_data_known_at": "2026-08-31T09:00:00+09:00",
        "min_trading_value": 10_000_000, "min_market_cap": 100_000_000,
        "max_market_cap": 10_000_000_000, "max_recent_rise_pct": 20,
        "max_gap_pct": 10, "max_pre_return_pct": 20,
    }
    result.update(override)
    return result

def evidence(d, key="e1"):
    return create_evidence(d, {"symbol":"005930", "kind":"disclosure", "title":"contract", "summary":"x", "source":"dart", "snapshot":{"url":"x"}, "dedupe_key":key, "known_at":"2026-08-31T09:00:00+09:00"})

def card(eid, fid, **card_override):
    payload = {"schema_version":1, "symbol":"005930", "headline":"h", "conclusion":"c", "change":"c", "source_evidence":[{"id":eid,"source":"dart","url":"https://dart.fss.or.kr"}], "source_urls":["https://dart.fss.or.kr"], "business_value":"x", "certainty":"high", "priced_in":"low", "filter_verdict":"PASS", "price_cap":100, "window":{"start":"2026-08-31T09:00:00+09:00", "end":"2026-08-31T15:00:00+09:00"}, "max_amount":250, "max_qty":3, "stop_loss":80, "take_profit":[{"price":110,"qty":1}], "evidence_invalidation":{"rule":"disclosure_retracted"}, "holding_until":"2026-09-01T10:00:00+09:00", "review_at":"2026-09-01T09:00:00+09:00", "false_positive":"x", "unknowns":"x", "verdict":"매수 검토 가능", "confidence":0.5}
    payload.update(card_override)
    return {"evidence_id":eid, "filter_id":fid, "model":"m", "provider":"p", "card":payload}

def make_plan(d):
    e=evidence(d); f=save_filter(d,e['id'],raw(),AS_OF,"2026-08-31T09:00:00+09:00"); c=save_card(d,card(e['id'],f['id'])); d.execute("INSERT INTO users(email,password) VALUES('u','p')"); d.commit(); user_decision(d,c['id'],1,'approve'); return c, d.execute("SELECT id FROM order_plans ORDER BY id DESC").fetchone()['id']

def tick(key, **override):
    x={"tick_key":key,"known_at":"2026-08-31T10:00:00+09:00","server_now":"2026-08-31T10:03:00+09:00","price":90,"liquidity":1,"trading_value":100_000_000,"gap_pct":1,"fill_qty":1}
    x.update(override); return x

def test_filter_requires_raw_inputs_and_computes_correct_signed_relatives():
    ok=run_filter(raw(),AS_OF,"2026-08-31T09:00:00+09:00")
    assert ok['verdict']=="PASS"
    assert ok['computed']['stock_vs_benchmark_pct']==10
    assert ok['computed']['stock_vs_sector_pct']==8
    assert ok['computed']['volume_ratio']==2
    assert "stock_return_pct - benchmark_return_pct" in ok['units']['stock_vs_benchmark_pct']
    for missing in ("stock_return_pct","benchmark_return_pct","sector_return_pct","current_volume","baseline_volume"):
        assert run_filter(raw(**{missing:None}),AS_OF,"2026-08-31T09:00:00+09:00")['verdict']=="FAIL"
    assert run_filter(raw(volume_ratio=999, baseline_volume=0),AS_OF,"2026-08-31T09:00:00+09:00")['verdict']=="FAIL"
    assert run_filter(raw(low_certainty_terms="MOU 검토 신청"),AS_OF,"2026-08-31T09:00:00+09:00")['verdict']=="FAIL"
    assert run_filter(raw(conflicting_financing=True),AS_OF,"2026-08-31T09:00:00+09:00")['verdict']=="FAIL"

def test_filter_rejects_future_and_stale_as_of_and_known_at():
    assert run_filter(raw(),"2026-08-31T09:00:00+09:00","2026-08-31T10:00:00+09:00")['verdict']=="FAIL"
    assert run_filter(raw(),"2026-09-02T10:00:00+09:00","2026-08-31T09:00:00+09:00")['verdict']=="FAIL"

def test_card_refuses_unlinked_or_fail_filter_and_new_version_invalidates_approval(db):
    e=evidence(db); f=save_filter(db,e['id'],raw(),AS_OF,"2026-08-31T09:00:00+09:00")
    with pytest.raises(HTTPException): save_card(db,card(e['id'],999))
    fail=save_filter(db,e['id'],raw(baseline_volume=0),"2026-08-31T10:01:00+09:00","2026-08-31T09:00:00+09:00")
    with pytest.raises(HTTPException): save_card(db,card(e['id'],fail['id']))
    c=save_card(db,card(e['id'],f['id'])); db.execute("INSERT INTO users(email,password) VALUES('u','p')"); db.commit(); user_decision(db,c['id'],1,'approve')
    c2=save_card(db,card(e['id'],f['id']))
    assert c2['version']==2
    assert db.execute("SELECT status FROM order_plans").fetchone()['status']=="entry_invalidated"

def test_approval_requires_frozen_nonempty_valid_window_and_exact_duplicate_only(db):
    e=evidence(db); f=save_filter(db,e['id'],raw(),AS_OF,"2026-08-31T09:00:00+09:00"); db.execute("INSERT INTO users(email,password) VALUES('u','p')"); db.commit()
    with pytest.raises(HTTPException): save_card(db,card(e['id'],f['id'],window={}))
    with pytest.raises(HTTPException): save_card(db,card(e['id'],f['id'],window={"start":"2026-08-31T10:00:00+09:00","end":"2026-08-31T09:00:00+09:00"}))

def test_evaluator_uses_server_now_caps_and_frozen_exit_rules(db):
    _, plan=make_plan(db)
    for name, bad in [("stale",tick("s",server_now="2026-08-31T10:06:00+09:00")),("future",tick("f",known_at="2026-08-31T10:04:00+09:00",server_now="2026-08-31T10:03:00+09:00")),("price",tick("p",price=101)),("gap",tick("g",gap_pct=11)),("liquidity",tick("l",liquidity=0)),("conflict",tick("c",conflicting_disclosure=True))]:
        assert evaluate_order_plan(db,plan,bad)['fills']==[], name
    first=evaluate_order_plan(db,plan,tick("b1",fill_qty=1)); assert first['fills'][0]['side']=="buy"
    second=evaluate_order_plan(db,plan,tick("b2",fill_qty=2)); assert second['fills'][0]['side']=="buy" and second['fills'][0]['qty']==1
    assert evaluate_order_plan(db,plan,tick("x",exit=True,fill_qty=3))['fills']==[]
    sold=evaluate_order_plan(db,plan,tick("sell",price=79,fill_qty=9)); assert sold['fills'][0]=={'side':'sell','qty':2,'price':79}
    assert evaluate_order_plan(db,plan,tick("sell",price=79))['idempotent']
    assert db.execute("SELECT count(*) n FROM exit_lineage").fetchone()['n']==1

def test_scheduler_start_is_idempotent_and_missing_finish_is_404(monkeypatch, tmp_path):
    monkeypatch.setenv("INTERNAL_API_KEY", "test-key")
    monkeypatch.setattr(dbmod, "DATABASE_PATH", str(tmp_path / "scheduler.db"))
    from app import app
    client=TestClient(app); headers={"X-Internal-API-Key":"test-key"}
    start=client.post("/api/internal/scheduler-runs/0800-2026-08-31/start",json={"kind":"card","count":0,"detail":{}},headers=headers)
    assert start.status_code==200 and start.json()["idempotent"] is False
    same=client.post("/api/internal/scheduler-runs/0800-2026-08-31/start",json={"kind":"card"},headers=headers)
    assert same.status_code==200 and same.json()["idempotent"] is True
    assert client.post("/api/internal/scheduler-runs/missing/finish",json={"status":"done"},headers=headers).status_code==404
    done=client.post("/api/internal/scheduler-runs/0800-2026-08-31/finish",json={"status":"done","count":2,"detail":{"skipped":1}},headers=headers)
    assert done.status_code==200 and done.json()["count"]==2
