"""Focused RED→GREEN coverage for topic 7923 verifier rework."""
import sqlite3

import pytest
from fastapi.testclient import TestClient

from kr_stock_autotrader import db as dbmod
from kr_stock_autotrader.decision_cards import create_evidence, save_card, save_filter
from tests.test_decision_card_invariants import card, raw

INTERNAL = {"X-Internal-API-Key": "topic-7923-v2"}
D1 = "2026-09-04T08:00:03+09:00"
D2 = "2026-09-05T08:00:03+09:00"


def evidence(key):
    return {"symbol": "005930", "name": "삼성전자", "kind": "공시", "title": key,
            "summary": "fixture", "source": "DART", "source_url": "https://example.test/e",
            "snapshot": {}, "dedupe_key": key, "announcement_at": D1, "collected_at": D1,
            "known_at": D1}


def nonbuy(eid, fid):
    payload = card(eid, fid, filter_verdict="PASS", verdict="관찰", price_cap=None, window=None,
                   max_amount=None, max_qty=None, stop_loss=None, take_profit=None,
                   evidence_invalidation=None, holding_until=None, review_at=None,
                   valid_until=None, expires=None, order_type=None)
    payload["lineage_key"] = f"005930:{eid}"
    return payload


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("INTERNAL_API_KEY", INTERNAL["X-Internal-API-Key"])
    monkeypatch.setattr(dbmod, "DATABASE_PATH", str(tmp_path / "rework.db"))
    from app import app
    return TestClient(app), tmp_path / "rework.db", monkeypatch


def post_evidence(client, key):
    response = client.post("/api/internal/evidence", headers=INTERNAL, json=evidence(key))
    assert response.status_code == 200, response.text
    return response.json()


def post_filter(client, evidence_id, inputs, timestamp, parent=None):
    body = {"evidence_id": evidence_id, "inputs": inputs, "as_of": timestamp, "known_at": timestamp}
    if parent is not None:
        body["parent_filter_id"] = parent
    return client.post("/api/internal/filters", headers=INTERNAL, json=body)


def test_operation_summary_is_immutable_processing_history(client):
    api, _, monkeypatch = client
    import kr_stock_autotrader.decision_cards as cards
    monkeypatch.setattr(cards, "now", lambda: D1)
    item = post_evidence(api, "processing-history")
    root = post_filter(api, item["id"], raw(market_data_known_at=D1), D1).json()
    root_card = api.post("/api/internal/cards/results", headers=INTERNAL, json=nonbuy(item["id"], root["id"])).json()
    monkeypatch.setattr(cards, "now", lambda: D2)
    successor = post_filter(api, item["id"], raw(economic_terms="corrected", market_data_known_at=D1), D1, root["id"])
    assert successor.status_code == 200
    successor_card = api.post("/api/internal/cards/results", headers=INTERNAL, json=nonbuy(item["id"], successor.json()["id"])).json()
    assert api.post("/api/signup", json={"email": "history@example.test", "password": "long-password"}).status_code == 200
    old_list = api.get("/api/cards?operation_date=2026-09-04").json()
    assert [row["id"] for row in old_list] == [root_card["id"]]
    old_summary = api.get("/api/cards/summary?operation_date=2026-09-04").json()
    new_summary = api.get("/api/cards/summary?operation_date=2026-09-05").json()
    assert (old_summary["필터 PASS"], old_summary["카드 생성"], old_summary["날짜 축"]) == (1, 1, "운영 처리 이력")
    assert (new_summary["필터 PASS"], new_summary["카드 생성"]) == (1, 1)
    assert api.get("/api/cards?operation_date=2026-09-05").json()[0]["id"] == successor_card["id"]


def test_filter_trigger_abort_is_not_conflict_and_rolls_back_audit(client):
    api, path, _ = client
    item = post_evidence(api, "trigger-abort")
    db = sqlite3.connect(path)
    db.execute("""CREATE TRIGGER fail_filter BEFORE INSERT ON deterministic_filter_results
                  BEGIN SELECT RAISE(ABORT, 'injected persistence failure'); END""")
    db.commit(); db.close()
    response = post_filter(api, item["id"], raw(market_data_known_at=D1), D1)
    assert response.status_code == 500
    db = sqlite3.connect(path)
    assert db.execute("SELECT count(*) FROM deterministic_filter_results").fetchone()[0] == 0
    assert db.execute("SELECT count(*) FROM audit_logs WHERE entity_type='filter'").fetchone()[0] == 0
    db.close()


def test_head_discovery_api_and_scheduler_like_successor_card_journey(client):
    api, _, _ = client
    item = post_evidence(api, "head-discovery")
    root = post_filter(api, item["id"], raw(market_data_known_at=D1), D1).json()
    missing = api.get("/api/internal/filters/head", params={"evidence_id": item["id"], "as_of": D2, "known_at": D2}, headers=INTERNAL)
    assert missing.status_code == 404, missing.text
    found = api.get("/api/internal/filters/head", params={"evidence_id": item["id"], "as_of": D1, "known_at": D1}, headers=INTERNAL)
    assert found.status_code == 200
    assert found.json()["id"] == root["id"] and found.json()["is_current"] is True
    successor = post_filter(api, item["id"], raw(economic_terms="corrected", market_data_known_at=D1), D1, found.json()["id"])
    assert successor.status_code == 200
    detail = api.get(f"/api/internal/filters/{successor.json()['id']}", headers=INTERNAL).json()
    saved = api.post("/api/internal/cards/results", headers=INTERNAL, json=nonbuy(item["id"], detail["id"]))
    assert saved.status_code == 200
    assert api.get(f"/api/internal/cards/{saved.json()['id']}", headers=INTERNAL).json()["filter_id"] == detail["id"]


def test_cli_filter_head_builds_exact_readonly_request(monkeypatch, capsys):
    from kr_stock_autotrader import cli
    seen = {}
    monkeypatch.setattr(cli, "call", lambda method, path, payload=None: seen.update(method=method, path=path, payload=payload) or {"id": 7})
    assert cli.main(["filter-head", "9", D1, D1]) == 0
    assert seen == {"method": "GET", "path": "/api/internal/filters/head?evidence_id=9&as_of=2026-09-04T08%3A00%3A03%2B09%3A00&known_at=2026-09-04T08%3A00%3A03%2B09%3A00", "payload": None}
    assert '"id": 7' in capsys.readouterr().out


def _legacy_db(path, custom_index=True):
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.executescript("""
      CREATE TABLE material_evidence (id INTEGER PRIMARY KEY);
      INSERT INTO material_evidence VALUES (1);
      CREATE TABLE deterministic_filter_results (
        id INTEGER PRIMARY KEY, evidence_id INTEGER NOT NULL REFERENCES material_evidence(id), raw_inputs TEXT NOT NULL,
        computed_outputs TEXT NOT NULL, as_of TEXT NOT NULL, known_at TEXT NOT NULL,
        evidence_version INTEGER NOT NULL DEFAULT 1, parent_filter_id INTEGER REFERENCES deterministic_filter_results(id), verdict TEXT NOT NULL, reasons TEXT NOT NULL,
        created_at TEXT NOT NULL, UNIQUE(evidence_id,as_of,known_at,evidence_version));
      INSERT INTO deterministic_filter_results VALUES (7,1,' {\"z\": 1} ','{}','2026-09-04T08:00:03+09:00','2026-09-04T08:00:03+09:00',1,NULL,'PASS','[]','2026-09-04T08:00:03+09:00');
      INSERT INTO deterministic_filter_results VALUES (8,1,'{\"y\": 2}','{}','2026-09-04T08:01:03+09:00','2026-09-04T08:01:03+09:00',1,7,'PASS','[]','2026-09-04T08:01:03+09:00');
      CREATE TABLE decision_cards (id INTEGER PRIMARY KEY, filter_id INTEGER NOT NULL REFERENCES deterministic_filter_results(id));
      INSERT INTO decision_cards VALUES (11,7);
    """)
    if custom_index:
        db.execute("CREATE INDEX user_filter_created_at ON deterministic_filter_results(created_at)")
    db.commit()
    return db


def test_legacy_migration_preserves_custom_index_and_foreign_keys(tmp_path, monkeypatch):
    path = tmp_path / "legacy.db"
    db = _legacy_db(path)
    monkeypatch.setattr(dbmod, "DATABASE_PATH", str(path))
    db.close()
    migrated = dbmod.connect()
    indexes = {row["name"] for row in migrated.execute("PRAGMA index_list(deterministic_filter_results)")}
    assert "user_filter_created_at" in indexes
    assert migrated.execute("SELECT id,raw_inputs FROM deterministic_filter_results").fetchone()["id"] == 7
    assert migrated.execute("SELECT filter_id FROM decision_cards").fetchone()["filter_id"] == 7
    assert migrated.execute("SELECT parent_filter_id FROM deterministic_filter_results WHERE id=8").fetchone()["parent_filter_id"] == 7
    assert migrated.execute("PRAGMA foreign_key_check").fetchall() == []
    assert migrated.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    migrated.close()
    assert dbmod.connect().execute("SELECT count(*) FROM deterministic_filter_results").fetchone()[0] == 2


def test_legacy_migration_copy_failure_rolls_back_intact_table(tmp_path, monkeypatch):
    path = tmp_path / "broken-legacy.db"
    db = _legacy_db(path, custom_index=False)
    monkeypatch.setattr(dbmod, "_canonical_input_sha256", lambda _: (_ for _ in ()).throw(RuntimeError("copy failed")))
    with pytest.raises(RuntimeError, match="copy failed"):
        dbmod._migrate_filter_lineage(db)
    assert db.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert "UNIQUE" in db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='deterministic_filter_results'").fetchone()[0]
    assert db.execute("SELECT id FROM deterministic_filter_results").fetchone()[0] == 7
    assert db.execute("PRAGMA foreign_key_check").fetchall() == []
    db.close()


def test_legacy_migration_index_recreation_failure_rolls_back_intact_table(tmp_path, monkeypatch):
    path = tmp_path / "index-failure-legacy.db"
    db = _legacy_db(path)
    monkeypatch.setattr(dbmod, "_recreate_filter_index", lambda *_: (_ for _ in ()).throw(RuntimeError("index failed")))
    with pytest.raises(RuntimeError, match="index failed"):
        dbmod._migrate_filter_lineage(db)
    assert db.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert "UNIQUE" in db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='deterministic_filter_results'").fetchone()[0]
    assert db.execute("SELECT id FROM deterministic_filter_results").fetchone()[0] == 7
    assert db.execute("PRAGMA foreign_key_check").fetchall() == []
    db.close()
