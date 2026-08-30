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
    evaluate_order_plan, mutate_evidence, save_card, save_filter, user_card_view,
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
    changed = client.post(f"/api/order-plans/{old_plan}/edit", json={"price_cap": 91})
    assert changed.status_code == 200 and changed.json()["status"] == "entry_invalidated"
    assert client.post(f"/api/cards/{created['id']}/decisions", json={"decision":"approve"}).json()["order_plan_id"] != old_plan


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
