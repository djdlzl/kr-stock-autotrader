"""Topic 7923 invariant and local TestClient workflow regressions."""
from concurrent.futures import ThreadPoolExecutor
import threading
from pathlib import Path
import sqlite3
from fastapi.testclient import TestClient
import pytest
from kr_stock_autotrader import db as dbmod
from kr_stock_autotrader.decision_cards import canon, save_filter
from tests.test_decision_card_invariants import card, raw

INTERNAL={"X-Internal-API-Key":"topic-7923-test-key"}
KNOWN="2026-09-03T17:15:00+09:00"; TIME="2026-09-04T08:00:03+09:00"
V2="giraffe-premarket-filter-v2-short-term-priced-in"

def inputs(**overrides):
    x=raw(source="dart",announcement_at=KNOWN,economic_terms="contract",market_data_known_at=TIME,filter_config_version=V2,short_term_rise_sessions=2,max_short_term_excess_rise_pct=10,short_term_stock_return_pct=2.,short_term_benchmark_return_pct=1.,short_term_excess_return_pct=1.,short_term_window={"sessions":2,"start":"2026-09-01","end":"2026-09-03","ended_known_at":"2026-09-03T15:30:00+09:00"},short_term_provenance={"source":"KIS","daily_bars_known_at":"2026-09-03T15:30:00+09:00","market_data_known_at":TIME})
    x.update(overrides); return x

def incomplete():
    x=inputs()
    for k in ("short_term_benchmark_return_pct","short_term_excess_return_pct","short_term_window","short_term_provenance","max_short_term_excess_rise_pct"): x.pop(k)
    return x

def evidence(key):
    return {"symbol":"005930","name":"삼성전자","kind":"disclosure","title":key,"summary":"계약","source":"dart","source_url":"https://example.test/e","announcement_at":KNOWN,"collected_at":"2026-09-03T22:00:00+00:00","known_at":KNOWN,"snapshot":{},"dedupe_key":key}
def nonbuy(eid,fid):
    x=card(eid,fid,filter_verdict="PASS",verdict="관찰",price_cap=None,window=None,max_amount=None,max_qty=None,stop_loss=None,take_profit=None,evidence_invalidation=None,holding_until=None,review_at=None,valid_until=None,expires=None,order_type=None); x["lineage_key"]=f"005930:{eid}"; return x
@pytest.fixture
def client(monkeypatch,tmp_path):
    monkeypatch.setenv("INTERNAL_API_KEY",INTERNAL["X-Internal-API-Key"]); monkeypatch.setattr(dbmod,"DATABASE_PATH",str(tmp_path/"t.db"))
    from app import app
    return TestClient(app),tmp_path/"t.db"
def post_ev(c,key):
    r=c.post("/api/internal/evidence",headers=INTERNAL,json=evidence(key)); assert r.status_code==200,r.text; return r.json()
def post_f(c,eid,x,parent=None):
    p={"evidence_id":eid,"inputs":x,"as_of":TIME,"known_at":TIME}
    if parent is not None:p["parent_filter_id"]=parent
    return c.post("/api/internal/filters",headers=INTERNAL,json=p)

def test_exact_canonical_retry_has_one_id_and_current_metadata(client):
    c,path=client; e=post_ev(c,"retry"); a=post_f(c,e["id"],incomplete()); b=post_f(c,e["id"],dict(reversed(list(incomplete().items()))))
    assert a.status_code==b.status_code==200 and a.json()["id"]==b.json()["id"]
    assert not a.json()["idempotent"] and b.json()["idempotent"] and b.json()["is_current"]
    assert {"lineage_version","parent_filter_id","input_sha256","evaluator_version"} <= b.json().keys()
    assert sqlite3.connect(path).execute("select count(*) from deterministic_filter_results").fetchone()[0]==1

def test_correction_is_append_only_and_stale_parent_is_409(client):
    c,path=client; e=post_ev(c,"append"); parent=post_f(c,e["id"],incomplete()).json(); before=sqlite3.connect(path).execute("select raw_inputs from deterministic_filter_results where id=?",(parent["id"],)).fetchone()[0]
    successor=post_f(c,e["id"],inputs(),parent["id"]); assert successor.status_code==200; child=successor.json(); assert child["parent_filter_id"]==parent["id"] and child["lineage_version"]==2 and child["is_current"]
    stale=post_f(c,e["id"],inputs(economic_terms="different"),parent["id"]); assert stale.status_code==409
    assert sqlite3.connect(path).execute("select raw_inputs from deterministic_filter_results where id=?",(parent["id"],)).fetchone()[0]==before

def test_stale_exact_retry_is_readable_but_not_current(client):
    c,_=client; e=post_ev(c,"stale-retry"); parent=post_f(c,e["id"],incomplete()).json(); post_f(c,e["id"],inputs(),parent["id"])
    retry=post_f(c,e["id"],incomplete()); assert retry.status_code==200 and retry.json()["id"]==parent["id"] and not retry.json()["is_current"]

def test_card_rejects_stale_filter(client):
    c,_=client; e=post_ev(c,"card-stale"); parent=post_f(c,e["id"],incomplete()).json(); post_f(c,e["id"],inputs(),parent["id"])
    payload=nonbuy(e["id"],parent["id"]); payload["card"]["filter_verdict"]="FAIL"
    assert c.post("/api/internal/cards/results",headers=INTERNAL,json=payload).status_code==409

def test_concurrent_identical_correction_has_one_successor(client):
    c,path=client; e=post_ev(c,"same"); parent=post_f(c,e["id"],incomplete()).json(); barrier=threading.Barrier(2)
    def go(_):
        barrier.wait()
        with TestClient(c.app) as cc:return post_f(cc,e["id"],inputs(),parent["id"])
    with ThreadPoolExecutor(2) as p: results=list(p.map(go,(1,2)))
    assert [r.status_code for r in results]==[200,200] and len({r.json()["id"] for r in results})==1
    assert sqlite3.connect(path).execute("select count(*) from deterministic_filter_results").fetchone()[0]==2

def test_concurrent_distinct_corrections_allow_exactly_one_successor(client):
    c,_=client; e=post_ev(c,"distinct"); parent=post_f(c,e["id"],incomplete()).json(); barrier=threading.Barrier(2)
    def go(label):
        barrier.wait()
        with TestClient(c.app) as cc:return post_f(cc,e["id"],inputs(economic_terms=label),parent["id"])
    with ThreadPoolExecutor(2) as p: results=list(p.map(go,("a","b")))
    assert sorted(r.status_code for r in results)==[200,409]

def test_local_workflow_successor_card_readback_and_no_orders(client):
    c,path=client; e=post_ev(c,"workflow"); parent=post_f(c,e["id"],incomplete()).json(); f=post_f(c,e["id"],inputs(),parent["id"]).json()
    saved=c.post("/api/internal/cards/results",headers=INTERNAL,json=nonbuy(e["id"],f["id"])); assert saved.status_code==200
    assert c.get(f'/api/internal/filters/{f["id"]}',headers=INTERNAL).json()["id"]==f["id"]
    assert c.get(f'/api/internal/cards/{saved.json()["id"]}',headers=INTERNAL).json()["filter_id"]==f["id"]
    db=sqlite3.connect(path); assert all(db.execute(f"select count(*) from {t}").fetchone()[0]==0 for t in ("order_plans","order_fills","positions","order_events")); db.close()

def test_prompt_preserves_v2_and_successor_contract():
    p=Path(__file__).parents[1]/"prompts/giraffe-decision-card-scheduler-v1.md"; text=p.read_text()
    for field in ("filter_inputs","short_term_excess_return_pct","short_term_window","short_term_provenance","parent_filter_id","현재 head filter ID","evidence 필드만 merge"): assert field in text
    assert "`id``" not in text
