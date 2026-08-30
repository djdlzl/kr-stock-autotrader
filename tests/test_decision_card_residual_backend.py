"""Residual backend contracts: reapproval, gates, cards, lifecycle, UI readbacks."""
import tempfile
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from kr_stock_autotrader import db as dbmod
from kr_stock_autotrader.decision_cards import (
    create_evidence, edit_draft, edit_order_plan, evaluate_order_plan, save_card,
    save_filter, user_decision,
)
from tests.test_decision_card_invariants import AS_OF, card, evidence, make_plan, raw, tick


@pytest.fixture
def db(monkeypatch):
    path = tempfile.mktemp(suffix=".db")
    monkeypatch.setattr(dbmod, "DATABASE_PATH", path)
    d = dbmod.connect()
    yield d
    d.close()


def test_immediate_approval_is_idempotent_but_same_value_edit_reapproves_new_plan(db):
    c, old_id = make_plan(db)
    immediate = user_decision(db, c["id"], 1, "approve")
    assert immediate == {"decision": "approve", "order_plan_hash": immediate["order_plan_hash"], "idempotent": True, "order_plan_id": old_id}
    old = dict(db.execute("SELECT price_cap,max_amount,max_qty FROM order_plans WHERE id=?", (old_id,)).fetchone())
    edit_order_plan(db, old_id, 1, old)
    renewed = user_decision(db, c["id"], 1, "approve")
    assert renewed["idempotent"] is False and renewed["order_plan_id"] != old_id
    assert db.execute("SELECT status FROM order_plans WHERE id=?", (old_id,)).fetchone()["status"] == "entry_invalidated"
    assert db.execute("SELECT count(DISTINCT version_hash) FROM order_plans WHERE card_id=?", (c["id"],)).fetchone()[0] == 2


def test_draft_override_is_strict_allowlist_and_validates_exit_plan(db):
    c, plan = make_plan(db)
    for bad in ({"symbol": "000660"}, {"model": "other"}, {"unknown": 1}, {"max_qty": 1.2}, {"order_type": "iceberg"}, {"take_profit": [{"price": 1, "qty": 0}]}, {"holding_until": "nope"}):
        with pytest.raises(HTTPException):
            edit_draft(db, c["id"], 1, bad)
    assert edit_draft(db, c["id"], 1, {"max_qty": 2, "order_type": "market"})["status"] == "draft"
    with pytest.raises(HTTPException): edit_order_plan(db, plan, 1, {"filter_id": 9})


def test_exit_cannot_fill_outside_kst_exchange_hours_or_holiday(db):
    _, plan = make_plan(db)
    evaluate_order_plan(db, plan, tick("buy", fill_qty=2))
    for key, known, current in (
        ("weekend", "2026-09-05T10:00:00+09:00", "2026-09-05T10:03:00+09:00"),
        ("preopen", "2026-09-01T08:00:00+09:00", "2026-09-01T08:03:00+09:00"),
    ):
        assert evaluate_order_plan(db, plan, tick(key, price=79, known_at=known, server_now=current))["fills"] == []
    assert db.execute("SELECT sold_qty FROM order_plans WHERE id=?", (plan,)).fetchone()[0] == 0


def test_fail_filter_card_is_saved_but_never_draftable_or_approvable(db):
    e = evidence(db)
    fail = save_filter(db, e["id"], raw(baseline_volume=0), AS_OF, "2026-08-31T09:00:00+09:00")
    c = save_card(db, card(e["id"], fail["id"], filter_verdict="FAIL", verdict="관찰"))
    db.execute("INSERT INTO users(email,password) VALUES('u','p')"); db.commit()
    with pytest.raises(HTTPException): edit_draft(db, c["id"], 1, {})
    with pytest.raises(HTTPException): user_decision(db, c["id"], 1, "approve")
    with pytest.raises(HTTPException): save_card(db, card(e["id"], fail["id"], filter_verdict="FAIL", verdict="매수 검토 가능"))


def test_evidence_status_and_authenticated_ui_scheduler_readbacks(monkeypatch, tmp_path):
    monkeypatch.setenv("INTERNAL_API_KEY", "test-key")
    monkeypatch.setattr(dbmod, "DATABASE_PATH", str(tmp_path / "api.db"))
    from app import app
    client = TestClient(app); h={"X-Internal-API-Key":"test-key"}
    ev={"symbol":"005930","kind":"disclosure","title":"t","summary":"s","source":"dart","snapshot":{},"dedupe_key":"api-e","known_at":"2026-08-31T09:00:00+09:00"}
    created=client.post("/api/internal/evidence", json=ev, headers=h).json()
    assert created["status"] == "new"
    assert client.get("/api/internal/evidence?status=new", headers=h).json()[0]["id"] == created["id"]
    assert client.patch(f"/api/internal/evidence/{created['id']}",json={"status":"error"},headers=h).json()["status"] == "error"
    start=client.post("/api/internal/scheduler-runs/r-2026-08-31/start",json={"kind":"card"},headers=h)
    assert start.status_code == 200
    latest=client.get("/api/internal/scheduler-runs/latest?kind=card&date=2026-08-31",headers=h)
    assert latest.status_code == 200 and latest.json()["run_key"] == "r-2026-08-31"
    assert client.get("/api/internal/cards?missing=true",headers=h).status_code == 200
