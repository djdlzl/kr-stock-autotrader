"""Independent verifier regressions: lineage, schema, UI/API, and CLI contracts."""
import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import sqlite3
import tempfile

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from kr_stock_autotrader import db as dbmod
from kr_stock_autotrader.auth import issue_session
from kr_stock_autotrader.decision_cards import (
    edit_draft, evaluate_order_plan, mutate_evidence, save_card, save_filter, user_card_view,
    user_decision,
)
from tests.test_decision_card_invariants import AS_OF, card, evidence, make_plan, raw, tick


@pytest.fixture
def db(monkeypatch):
    path = tempfile.mktemp(suffix=".db")
    monkeypatch.setattr(dbmod, "DATABASE_PATH", path)
    connection = dbmod.connect()
    yield connection
    connection.close()


def test_repeat_safe_migration_backfills_version_one(monkeypatch, tmp_path):
    path = tmp_path / "legacy.db"
    old = sqlite3.connect(path)
    old.execute("""CREATE TABLE material_evidence (
      id INTEGER PRIMARY KEY, symbol TEXT NOT NULL, name TEXT, kind TEXT NOT NULL, title TEXT NOT NULL, summary TEXT NOT NULL,
      source TEXT NOT NULL, source_url TEXT, announcement_at TEXT, collected_at TEXT NOT NULL, known_at TEXT NOT NULL,
      snapshot TEXT NOT NULL, newness TEXT NOT NULL, dedupe_key TEXT NOT NULL UNIQUE, status TEXT NOT NULL DEFAULT 'new',
      created_by TEXT NOT NULL, updated_at TEXT NOT NULL, invalidated_at TEXT, audit_json TEXT NOT NULL DEFAULT '[]')""")
    old.execute("INSERT INTO material_evidence VALUES(1,'005930',NULL,'x','old','old','dart',NULL,NULL,'2026-08-31T08:00:00+09:00','2026-08-31T08:00:00+09:00','{}','new','old-key','new','test','2026-08-31T08:00:00+09:00',NULL,'[]')")
    old.commit(); old.close()
    monkeypatch.setattr(dbmod, "DATABASE_PATH", str(path))
    first = dbmod.connect(); assert first.execute("SELECT version FROM material_evidence WHERE id=1").fetchone()["version"] == 1; first.close()
    second = dbmod.connect(); assert second.execute("SELECT version FROM material_evidence WHERE id=1").fetchone()["version"] == 1; second.close()


def test_partial_fill_mutation_stops_entry_and_preserves_frozen_exit(db):
    created, plan = make_plan(db)
    assert evaluate_order_plan(db, plan, tick("buy-before-mutation", fill_qty=1))["fills"] == [{"side": "buy", "qty": 1, "price": 90}]
    mutate_evidence(db, created["evidence_id"], {"summary": "revised"})
    assert db.execute("SELECT status FROM order_plans WHERE id=?", (plan,)).fetchone()["status"] == "review_required"
    assert evaluate_order_plan(db, plan, tick("fresh-buy-after-mutation", fill_qty=1))["fills"] == []
    assert evaluate_order_plan(db, plan, tick("stop-after-mutation", price=79))["fills"] == [{"side": "sell", "qty": 1, "price": 79}]


def test_filter_lineage_rejects_lookahead_market_data_and_stale_filter(db):
    item = evidence(db)
    with pytest.raises(HTTPException):
        save_filter(db, item["id"], raw(), "2026-08-31T08:00:00+09:00", "2026-08-31T07:30:00+09:00")
    with pytest.raises(HTTPException):
        save_filter(db, item["id"], raw(market_data_known_at="2026-08-31T10:01:00+09:00"), AS_OF, "2026-08-31T09:00:00+09:00")
    filt = save_filter(db, item["id"], raw(), AS_OF, "2026-08-31T09:00:00+09:00")
    assert filt["evidence_version"] == 1
    mutate_evidence(db, item["id"], {"summary": "revised"})
    with pytest.raises(HTTPException): save_card(db, card(item["id"], filt["id"]))
    fresh = save_filter(db, item["id"], raw(), "2026-08-31T10:01:00+09:00", "2026-08-31T09:01:00+09:00")
    assert fresh["evidence_version"] == 2
    assert save_card(db, card(item["id"], fresh["id"]))["id"]


def test_filter_rejects_market_data_newer_than_filter_known_at(db):
    item = evidence(db)
    with pytest.raises(HTTPException, match="market_data_known_at must be at or before filter.known_at"):
        save_filter(
            db, item["id"], raw(market_data_known_at="2026-08-31T09:30:00+09:00"),
            "2026-08-31T10:00:00+09:00", "2026-08-31T09:00:00+09:00",
        )


def test_unavailable_attempt_later_than_requested_as_of_is_rejected(db):
    item = evidence(db)
    unavailable = {"market_data_status":"unavailable", "market_data_attempted_at":"2026-08-31T09:01:00+09:00"}
    with pytest.raises(HTTPException, match="market_data_attempted_at must be at or before filter.known_at"):
        save_filter(db, item["id"], unavailable, AS_OF, "2026-08-31T09:00:00+09:00")


@pytest.mark.parametrize(
    ("evidence_known_at", "market_known_at", "message"),
    [
        ("2026-08-31T10:01:00+09:00", "2026-08-31T09:00:00+09:00", "evidence.known_at must be at or before filter.known_at"),
        ("2026-08-31T09:00:00+09:00", "2026-08-31T10:01:00+09:00", "market_data_known_at must be at or before filter.known_at"),
    ],
)
def test_filter_rejects_evidence_or_market_future_of_filter_and_as_of(db, evidence_known_at, market_known_at, message):
    item = evidence(db)
    db.execute("UPDATE material_evidence SET known_at=? WHERE id=?", (evidence_known_at, item["id"]))
    db.commit()
    with pytest.raises(HTTPException, match=message):
        save_filter(
            db, item["id"], raw(market_data_known_at=market_known_at),
            "2026-08-31T10:00:00+09:00", "2026-08-31T09:00:00+09:00",
        )


def test_schema_rejects_malformed_cards_and_keeps_pass_nonbuy_valid(db):
    item = evidence(db)
    filt = save_filter(db, item["id"], raw(), AS_OF, "2026-08-31T09:00:00+09:00")
    for override in (
        {"schema_version": 2}, {"source_urls": ["not-url"]},
        {"price_cap": float("inf")}, {"confidence": 1.1},
        {"take_profit": [{"price": 1, "qty": 0}]},
    ):
        with pytest.raises(HTTPException): save_card(db, card(item["id"], filt["id"], **override))
    assert save_card(db, card(item["id"], filt["id"], verdict="관찰"))["schema_version"] == 1


def test_schema_rejects_boolean_trading_scalars_but_accepts_numbers(db):
    item = evidence(db)
    filt = save_filter(db, item["id"], raw(), AS_OF, "2026-08-31T09:00:00+09:00")
    for override in (
        {"max_qty": True}, {"take_profit": [{"price": 110, "qty": True}]},
        {"price_cap": True}, {"max_amount": True}, {"stop_loss": True},
        {"take_profit": [{"price": True, "qty": 1}]}, {"confidence": True},
    ):
        with pytest.raises(HTTPException):
            save_card(db, card(item["id"], filt["id"], **override))
    saved = save_card(db, card(item["id"], filt["id"], price_cap=100, max_amount=250.5,
                                max_qty=3, stop_loss=80.5,
                                take_profit=[{"price": 110, "qty": 1}], confidence=0.5))
    assert saved["schema_version"] == 1


def test_nonbuy_cards_preserve_honest_null_order_fields_but_cannot_trade(db):
    item = evidence(db)
    fail = save_filter(db, item["id"], raw(baseline_volume=0), AS_OF, "2026-08-31T09:00:00+09:00")
    unavailable = {
        "price_cap": None, "window": None, "max_amount": None, "max_qty": None,
        "stop_loss": None, "take_profit": [], "evidence_invalidation": "",
        "holding_until": "", "review_at": None, "valid_until": "", "expires": None,
        "order_type": None,
    }
    pending = save_card(db, card(item["id"], fail["id"], filter_verdict="FAIL", verdict="판단 보류", **unavailable))
    assert pending["card"]["price_cap"] is None and pending["card"]["take_profit"] == []
    db.execute("INSERT INTO users(email,password) VALUES('null-order@test','p')"); db.commit()
    with pytest.raises(HTTPException): edit_draft(db, pending["id"], 1, {})
    with pytest.raises(HTTPException): user_decision(db, pending["id"], 1, "approve")

    observed = save_card(db, card(item["id"], save_filter(db, item["id"], raw(), "2026-08-31T10:01:00+09:00", "2026-08-31T09:01:00+09:00")["id"], verdict="관찰", **unavailable))
    assert observed["card"]["window"] is None


def test_buy_review_rejects_each_unavailable_order_field(db):
    item = evidence(db)
    filt = save_filter(db, item["id"], raw(), AS_OF, "2026-08-31T09:00:00+09:00")
    complete = {
        "valid_until": "2026-09-01T10:00:00+09:00", "expires": "2026-09-01T10:00:00+09:00",
        "order_type": "limit",
    }
    for unavailable in (
        {"price_cap": None}, {"window": None}, {"max_amount": None}, {"max_qty": None},
        {"stop_loss": None}, {"take_profit": []}, {"evidence_invalidation": ""},
        {"holding_until": ""}, {"review_at": None}, {"valid_until": None},
        {"expires": ""}, {"order_type": ""}, {"order_type": None},
    ):
        with pytest.raises(HTTPException):
            save_card(db, card(item["id"], filt["id"], **(complete | unavailable)))


def test_internal_status_invalidated_revokes_partial_entry_and_preserves_exit(monkeypatch, db):
    monkeypatch.setenv("INTERNAL_API_KEY", "test-key")
    created, plan = make_plan(db)
    assert evaluate_order_plan(db, plan, tick("buy-before-status-patch", fill_qty=1))["fills"] == [{"side": "buy", "qty": 1, "price": 90}]
    old_version = db.execute("SELECT version FROM material_evidence WHERE id=?", (created["evidence_id"],)).fetchone()["version"]
    from app import app
    client = TestClient(app)
    response = client.patch(f"/api/internal/evidence/{created['evidence_id']}", json={"status": "invalidated"}, headers={"X-Internal-API-Key": "test-key"})
    assert response.status_code == 200
    assert response.json()["version"] == old_version + 1
    assert db.execute("SELECT status FROM order_plans WHERE id=?", (plan,)).fetchone()["status"] == "review_required"
    assert evaluate_order_plan(db, plan, tick("fresh-buy-after-status-patch", fill_qty=1))["fills"] == []
    assert evaluate_order_plan(db, plan, tick("stop-after-status-patch", price=79))["fills"] == [{"side": "sell", "qty": 1, "price": 79}]


def test_authenticated_approval_detail_edit_and_reapproval_api_journey(monkeypatch, tmp_path):
    monkeypatch.setattr(dbmod, "DATABASE_PATH", str(tmp_path / "journey.db"))
    item_db = dbmod.connect()
    item = evidence(item_db)
    filt = save_filter(item_db, item["id"], raw(), AS_OF, "2026-08-31T09:00:00+09:00")
    created = save_card(item_db, card(item["id"], filt["id"]))
    item_db.execute("INSERT INTO users(email,password) VALUES('journey@example.test','p')")
    item_db.commit(); item_db.close()
    from app import app
    client = TestClient(app)
    client.cookies.set("session", issue_session(1))
    approved = client.post(f"/api/cards/{created['id']}/decisions", json={"decision":"approve"})
    assert approved.status_code == 200
    old_plan = approved.json()["order_plan_id"]
    detail = client.get(f"/api/cards/{created['id']}")
    assert detail.status_code == 200
    snapshot = detail.json()["user_state"]["order_plan"]["snapshot"]
    assert snapshot["window"] == {"start":"2026-08-31T09:00:00+09:00", "end":"2026-08-31T15:00:00+09:00"}
    payload = {"price_cap": 91, "max_amount": 180, "max_qty": 2, "order_type": "market",
               "holding_until": "2026-09-02T10:00:00+09:00", "review_at": "2026-09-02T09:00:00+09:00",
               "valid_until": "2026-09-02T11:00:00+09:00", "expires": "2026-09-02T12:00:00+09:00"}
    changed = client.post(f"/api/order-plans/{old_plan}/edit", json=payload)
    assert changed.status_code == 200 and changed.json()["status"] == "entry_invalidated"
    replacement = client.post(f"/api/cards/{created['id']}/decisions", json={"decision":"approve"})
    assert replacement.json()["order_plan_id"] != old_plan
    frozen = client.get(f"/api/cards/{created['id']}").json()["user_state"]["order_plan"]["snapshot"]
    assert {key: frozen[key] for key in payload} == payload


def test_cli_pending_cards_uses_missing_query_without_printing_secret(tmp_path):
    seen = []
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append((self.path, self.headers.get("X-Internal-API-Key")))
            self.send_response(200); self.send_header("Content-Type", "application/json"); self.end_headers()
            self.wfile.write(b'[{"id":1,"title":"missing"}]')
        def log_message(self, *_): pass
    server = HTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever); worker.start()
    try:
        env = {**os.environ, "GIRAFFE_URL": f"http://127.0.0.1:{server.server_port}", "INTERNAL_API_KEY": "never-print-this"}
        result = subprocess.run([sys.executable, "-m", "kr_stock_autotrader.cli", "pending-cards"], cwd=os.getcwd(), env=env, text=True, capture_output=True, check=True)
    finally:
        server.shutdown(); worker.join()
    assert seen == [("/api/internal/cards?missing=true", "never-print-this")]
    assert "never-print-this" not in result.stdout + result.stderr
