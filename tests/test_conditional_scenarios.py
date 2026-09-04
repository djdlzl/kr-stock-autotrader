"""RED/GREEN contracts for evidence-only conditional judgment-hold scenarios."""
from copy import deepcopy
from datetime import timedelta
import sqlite3
import threading

import pytest
from fastapi import HTTPException

from kr_stock_autotrader import db as dbmod
from kr_stock_autotrader.conditional_scenario_backfill import run
from kr_stock_autotrader.conditional_scenarios import build_scenario_candidate, create
from kr_stock_autotrader.decision_cards import card_detail, evidence_detail, user_card_view
from tests.test_event_scenarios import _lineage


def _hold(db, *, symbol="277880", invalidated=False):
    evidence_id, card_id = _lineage(db, symbol)
    db.execute("""UPDATE material_evidence SET title=?,summary=?,source_url=?,announcement_at=?,status='card_generated' WHERE id=?""", (
        "믹싱시스템 공급 계약", "확인된 계약 사실", "https://example.test/dart", "2026-08-30T09:00:00+09:00", evidence_id))
    card = {"symbol": symbol, "headline": "확인된 계약 사실", "unknowns": "원가와 마진", "false_positive": "납기 지연 또는 비용 증가", "evidence_invalidation": "계약 취소"}
    db.execute("UPDATE decision_cards SET verdict='판단 보류',card_json=?,invalidated_at=? WHERE id=?", (
        dbmod.json.dumps(card, ensure_ascii=False), "2026-08-31T09:00:00+09:00" if invalidated else None, card_id))
    db.commit()
    return evidence_detail(db, evidence_id), card_detail(db, card_id)


def test_conditional_builder_is_source_only_and_fails_closed(monkeypatch, tmp_path):
    monkeypatch.setattr(dbmod, "DATABASE_PATH", str(tmp_path / "conditional.db"))
    db = dbmod.connect(); evidence, card = _hold(db)
    candidate = build_scenario_candidate(evidence, card)
    assert candidate["status"] == "COMPLETE"
    payload = candidate["payload"]
    assert [x["label"] for x in payload["scenarios"]] == ["BAD", "BASE", "GOOD"]
    rendered = dbmod.json.dumps(payload, ensure_ascii=False)
    assert all(term not in rendered for term in ("probability", "expected_value", "price_krw", "margin_pct", "discount_rate"))
    incomplete = deepcopy(card); incomplete["card"].pop("false_positive"); incomplete["card"].pop("evidence_invalidation")
    assert build_scenario_candidate(evidence, incomplete) == {"status": "SCENARIO_INPUTS_REQUIRED", "missing": ["card.false_positive_or_evidence_invalidation"]}
    db.close()


def test_conditional_immutable_retry_successor_observation_refusal_and_no_highlight(monkeypatch, tmp_path):
    monkeypatch.setattr(dbmod, "DATABASE_PATH", str(tmp_path / "conditional.db"))
    db = dbmod.connect(); evidence, card = _hold(db)
    payload = build_scenario_candidate(evidence, card)["payload"]
    first = create(db, payload)
    assert first["expected_value_krw"] is None and first["scenario_kind"] == "CONDITIONAL"
    assert create(db, payload)["idempotent"] is True
    mismatch = deepcopy(payload); mismatch["scenarios"][0]["conditions"].append({"text": "새 문장", "provenance": "test"})
    with pytest.raises(HTTPException) as exc: create(db, mismatch)
    assert exc.value.status_code == 409
    successor = deepcopy(payload); successor["version"] = 2
    with pytest.raises(HTTPException) as exc: create(db, successor)
    assert exc.value.status_code == 409  # corrections require a persisted successor lineage
    with pytest.raises(HTTPException) as exc:
        __import__("kr_stock_autotrader.event_scenarios", fromlist=["observe"]).observe(db, payload["event_identity"], {})
    assert exc.value.status_code == 422  # malformed observations fail before any write
    from kr_stock_autotrader.event_scenarios import OBSERVATION_KEYS, observe
    with pytest.raises(HTTPException) as exc: observe(db, payload["event_identity"], {key: "x" for key in OBSERVATION_KEYS})
    assert exc.value.status_code == 404
    db.execute("INSERT INTO users(email,password) VALUES('hold@example.test','p')"); db.commit()
    view = user_card_view(db, card["id"], 1)["event_scenarios"][0]
    assert view["tracking_state"] == "UNOBSERVABLE" and view["current"]["active_scenario_label"] is None and view["trust"]["status"] != "READY"
    assert db.execute("SELECT COUNT(*) FROM order_plans").fetchone()[0] == 0
    db.close()


def test_card_save_orchestrates_only_complete_judgment_hold_candidate(monkeypatch, tmp_path):
    monkeypatch.setattr(dbmod, "DATABASE_PATH", str(tmp_path / "orchestration.db"))
    from tests.test_decision_cards import card as card_payload, evidence as create_evidence, inputs
    from kr_stock_autotrader.decision_cards import save_card, save_filter
    db = dbmod.connect(); evidence = create_evidence(db)
    db.execute("UPDATE material_evidence SET source_url=?,announcement_at=? WHERE id=?", ("https://example.test/dart", "2026-08-30T09:00:00+09:00", evidence["id"])); db.commit()
    filt = save_filter(db, evidence["id"], inputs(), "2026-08-31T09:00:00+09:00", "2026-08-31T08:00:00+09:00")
    saved_payload = card_payload(evidence["id"], filt["id"])
    saved_payload["card"].update({"verdict": "판단 보류", "filter_verdict": "PASS", "price_cap": None, "window": None, "max_amount": None, "max_qty": None, "stop_loss": None, "take_profit": None, "holding_until": None, "review_at": None, "valid_until": None, "expires": None, "order_type": None})
    saved = save_card(db, saved_payload)
    assert db.execute("SELECT scenarios_json FROM event_conditional_scenario_sets WHERE card_id=?", (saved["id"],)).fetchone() is None
    assert db.execute("SELECT COUNT(*) FROM order_plans").fetchone()[0] == 0
    db.close()


def test_backfill_is_dry_by_default_apply_is_idempotent_and_skips_invalidated(monkeypatch, tmp_path):
    monkeypatch.setattr(dbmod, "DATABASE_PATH", str(tmp_path / "conditional.db"))
    db = dbmod.connect(); _, active = _hold(db); _, invalidated = _hold(db, symbol="012170", invalidated=True)
    dry = run(db)
    assert dry["active_cards"] == 1 and dry["complete"] == 1 and dry["inputs_required"] == 0 and dry["created"] == 0
    assert dry["items"] == [{"card_id": active["id"], "status": "COMPLETE", "action": "WOULD_CREATE", "event_identity": f"CONDITIONAL:{active['evidence_id']}:{active['id']}"}]
    assert db.execute("SELECT COUNT(*) FROM event_conditional_scenario_sets").fetchone()[0] == 0
    applied = run(db, apply=True)
    assert applied["created"] == 1 and db.execute("SELECT COUNT(*) FROM event_conditional_scenario_sets WHERE card_id=?", (invalidated["id"],)).fetchone()[0] == 0
    assert run(db, apply=True)["idempotent"] == 1
    assert all(db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0 for table in ("order_plans", "order_fills", "positions"))
    db.close()


def test_conditional_create_and_internal_endpoint_reject_every_forged_field(monkeypatch, tmp_path):
    """The direct API may replay, but cannot be an alternate source of truth."""
    monkeypatch.setattr(dbmod, "DATABASE_PATH", str(tmp_path / "forged.db"))
    db = dbmod.connect(); evidence, card = _hold(db)
    payload = build_scenario_candidate(evidence, card)["payload"]
    mutations = (
        lambda p: p["scenarios"][0]["conditions"][0].update(text="INVENTED ATTACKER CONDITION"),
        lambda p: p["scenarios"][0]["conditions"][0].update(provenance="forged.path"),
        lambda p: p["scenarios"][0].update(label="GOOD"),
        lambda p: p.update(scenarios=list(reversed(p["scenarios"]))),
        lambda p: p.update(event_identity="CONDITIONAL:forged:identity"),
        lambda p: p.update(source_url="https://attacker.example/forged"),
    )
    for mutate in mutations:
        forged = deepcopy(payload); mutate(forged)
        with pytest.raises(HTTPException): create(db, forged)
        assert db.execute("SELECT COUNT(*) FROM event_conditional_scenario_sets").fetchone()[0] == 0
    monkeypatch.setenv("INTERNAL_API_KEY", "test-internal-key")
    from fastapi.testclient import TestClient
    from kr_stock_autotrader.api import app
    forged = deepcopy(payload); forged["scenarios"][0]["conditions"][0]["text"] = "INVENTED ATTACKER CONDITION"
    response = TestClient(app).post("/api/internal/scenario-sets", json=forged, headers={"X-Internal-API-Key": "test-internal-key"})
    assert response.status_code == 409
    assert db.execute("SELECT COUNT(*) FROM event_conditional_scenario_sets").fetchone()[0] == 0
    assert TestClient(app).post("/api/internal/scenario-sets", json=payload, headers={"X-Internal-API-Key": "test-internal-key"}).status_code == 200
    assert create(db, payload)["idempotent"] is True
    db.close()


def test_conditional_timestamp_and_url_boundaries_are_executable(monkeypatch, tmp_path):
    monkeypatch.setattr(dbmod, "DATABASE_PATH", str(tmp_path / "timestamps.db"))
    db = dbmod.connect(); evidence, card = _hold(db)
    payload = build_scenario_candidate(evidence, card)["payload"]
    for field, value in (("known_at", "2026-08-30T09:00:00"), ("known_at", "2999-01-01T00:00:00+00:00"),
                         ("disclosed_at", "2026-08-31T09:00:00+09:00"), ("source_url", "http://127.0.0.1/private"),
                         ("source_url", "https://user:pass@example.test/private"), ("source_url", "file:///private")):
        forged = deepcopy(payload); forged[field] = value
        with pytest.raises(HTTPException): create(db, forged)
        assert db.execute("SELECT COUNT(*) FROM event_conditional_scenario_sets").fetchone()[0] == 0
    db.close()


def test_backfill_is_atomic_on_later_failure_and_exact_retry(monkeypatch, tmp_path):
    monkeypatch.setattr(dbmod, "DATABASE_PATH", str(tmp_path / "batch.db"))
    db = dbmod.connect(); _hold(db); _hold(db, symbol="012170")
    import kr_stock_autotrader.conditional_scenario_backfill as backfill
    real_create, calls = backfill.create, []
    def fail_second(conn, payload, *, commit=True):
        calls.append(payload["card_id"])
        if len(calls) == 2: raise HTTPException(409, "forced later failure")
        return real_create(conn, payload, commit=commit)
    monkeypatch.setattr(backfill, "create", fail_second)
    with pytest.raises(HTTPException, match="forced later failure"): backfill.run(db, apply=True)
    assert db.execute("SELECT COUNT(*) FROM event_conditional_scenario_sets").fetchone()[0] == 0
    monkeypatch.setattr(backfill, "create", real_create)
    applied = backfill.run(db, apply=True)
    assert {key: applied[key] for key in ("active_cards", "complete", "inputs_required", "created", "idempotent")} == {"active_cards": 2, "complete": 2, "inputs_required": 0, "created": 2, "idempotent": 0}
    rerun = backfill.run(db, apply=True)
    assert rerun["created"] == 0 and rerun["idempotent"] == 2
    db.close()


def test_conditional_create_write_reservation_blocks_racing_evidence_change(monkeypatch, tmp_path):
    monkeypatch.setattr(dbmod, "DATABASE_PATH", str(tmp_path / "race.db"))
    seed = dbmod.connect(); evidence, card = _hold(seed)
    payload = build_scenario_candidate(evidence, card)["payload"]; seed.close()
    import kr_stock_autotrader.conditional_scenarios as conditional
    original_builder, canonical_checked, allow_insert = conditional.build_scenario_candidate, threading.Event(), threading.Event()

    def pause_after_authoritative_read(*args):
        canonical_checked.set()
        assert allow_insert.wait(2)
        return original_builder(*args)

    monkeypatch.setattr(conditional, "build_scenario_candidate", pause_after_authoritative_read)
    outcome = {}
    def create_in_owner_connection():
        owner = dbmod.connect()
        try:
            outcome["saved"] = create(owner, payload)
        except Exception as exc:  # assertion below reports an unexpected lock failure
            outcome["error"] = exc
        finally:
            owner.close()

    worker = threading.Thread(target=create_in_owner_connection)
    worker.start(); assert canonical_checked.wait(2)
    racer = dbmod.connect(); racer.execute("PRAGMA busy_timeout=0")
    with pytest.raises(Exception):
        racer.execute("UPDATE material_evidence SET summary='RACE_REPLACED_SOURCE' WHERE id=?", (evidence["id"],))
    assert racer.execute("SELECT summary FROM material_evidence WHERE id=?", (evidence["id"],)).fetchone()[0] == "확인된 계약 사실"
    racer.rollback()  # release the failed writer/read transaction before owner commit
    allow_insert.set(); worker.join(2)
    assert not worker.is_alive() and "error" not in outcome and outcome["saved"]["idempotent"] is False
    assert racer.execute("SELECT COUNT(*) FROM event_conditional_scenario_sets").fetchone()[0] == 1
    assert racer.execute("SELECT summary FROM material_evidence WHERE id=?", (evidence["id"],)).fetchone()[0] == "확인된 계약 사실"
    racer.close()


def test_persisted_nonpublic_ip_urls_require_inputs_and_never_persist(monkeypatch, tmp_path):
    monkeypatch.setattr(dbmod, "DATABASE_PATH", str(tmp_path / "private-urls.db"))
    db = dbmod.connect(); evidence, card = _hold(db)
    safe_payload = build_scenario_candidate(evidence, card)["payload"]
    for source_url in ("http://127.0.0.1/x", "http://10.0.0.1/x", "http://[::1]/x",
                       "http://169.254.1.1/x", "http://[fe80::1]/x", "http://[fc00::1]/x",
                       "http://[::]/x", "http://[ff00::1]/x", "http://240.0.0.1/x"):
        db.execute("UPDATE material_evidence SET source_url=? WHERE id=?", (source_url, evidence["id"])); db.commit()
        candidate = build_scenario_candidate(evidence_detail(db, evidence["id"]), card_detail(db, card["id"]))
        assert candidate["status"] == "SCENARIO_INPUTS_REQUIRED"
        assert "evidence.source_url" in candidate["missing"]
        forged = deepcopy(safe_payload); forged["source_url"] = source_url
        with pytest.raises(HTTPException, match="invalid conditional scenario source fields"):
            create(db, forged)
        assert db.execute("SELECT COUNT(*) FROM event_conditional_scenario_sets").fetchone()[0] == 0
    db.close()


def test_conditional_create_commit_false_requires_owner_transaction(monkeypatch, tmp_path):
    monkeypatch.setattr(dbmod, "DATABASE_PATH", str(tmp_path / "create-owner.db"))
    db = dbmod.connect(); evidence, card = _hold(db)
    payload = build_scenario_candidate(evidence, card)["payload"]
    with pytest.raises(HTTPException, match="owner transaction"):
        create(db, payload, commit=False)
    assert db.execute("SELECT COUNT(*) FROM event_conditional_scenario_sets").fetchone()[0] == 0
    db.close()


def test_backfill_preserves_caller_owned_transaction_on_success_and_failure(monkeypatch, tmp_path):
    monkeypatch.setattr(dbmod, "DATABASE_PATH", str(tmp_path / "caller-owned.db"))
    db = dbmod.connect(); _hold(db)
    db.execute("BEGIN IMMEDIATE")
    db.execute("INSERT INTO users(email,password) VALUES('outer-success@example.test','p')")
    saved = run(db, apply=True)
    assert saved["created"] == 1 and db.in_transaction
    assert db.execute("SELECT COUNT(*) FROM users WHERE email='outer-success@example.test'").fetchone()[0] == 1
    assert db.execute("SELECT COUNT(*) FROM event_conditional_scenario_sets").fetchone()[0] == 1
    db.rollback()
    assert db.execute("SELECT COUNT(*) FROM users WHERE email='outer-success@example.test'").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM event_conditional_scenario_sets").fetchone()[0] == 0

    db.execute("BEGIN IMMEDIATE")
    db.execute("INSERT INTO users(email,password) VALUES('outer-failure@example.test','p')")
    import kr_stock_autotrader.conditional_scenario_backfill as backfill
    real_create = backfill.create
    monkeypatch.setattr(backfill, "create", lambda *args, **kwargs: (_ for _ in ()).throw(HTTPException(409, "forced failure")))
    with pytest.raises(HTTPException, match="forced failure"):
        run(db, apply=True)
    assert db.in_transaction
    assert db.execute("SELECT COUNT(*) FROM users WHERE email='outer-failure@example.test'").fetchone()[0] == 1
    assert db.execute("SELECT COUNT(*) FROM event_conditional_scenario_sets").fetchone()[0] == 0
    monkeypatch.setattr(backfill, "create", real_create)
    db.commit()
    assert db.execute("SELECT COUNT(*) FROM users WHERE email='outer-failure@example.test'").fetchone()[0] == 1
    db.close()


def test_repeated_connect_preserves_legacy_quantitative_schema_customizations(monkeypatch, tmp_path):
    monkeypatch.setattr(dbmod, "DATABASE_PATH", str(tmp_path / "legacy.db"))
    db = dbmod.connect()
    db.executescript("ALTER TABLE event_scenario_sets ADD COLUMN legacy_note TEXT; CREATE INDEX legacy_quant_idx ON event_scenario_sets(legacy_note); CREATE TRIGGER legacy_quant_trigger AFTER INSERT ON event_scenario_sets BEGIN SELECT 1; END;")
    db.commit(); db.close()
    for _ in range(2):
        db = dbmod.connect(); db.close()
    db = dbmod.connect()
    assert "legacy_note" in db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='event_scenario_sets'").fetchone()[0]
    assert db.execute("SELECT sql FROM sqlite_master WHERE name='legacy_quant_idx'").fetchone()[0] == "CREATE INDEX legacy_quant_idx ON event_scenario_sets(legacy_note)"
    assert "CREATE TRIGGER legacy_quant_trigger" in db.execute("SELECT sql FROM sqlite_master WHERE name='legacy_quant_trigger'").fetchone()[0]
    db.close()


def test_legacy_quantitative_readback_is_schema_safe_and_conditional_first(monkeypatch, tmp_path):
    """A production-shaped old quantitative table remains read-only-compatible."""
    database = tmp_path / "legacy-quantitative-readback.db"
    monkeypatch.setattr(dbmod, "DATABASE_PATH", str(database))
    raw = sqlite3.connect(database)
    raw.executescript("""
    CREATE TABLE event_scenario_sets (
      id INTEGER PRIMARY KEY, event_identity TEXT NOT NULL, version INTEGER NOT NULL, symbol TEXT NOT NULL,
      event_type TEXT NOT NULL, profile_id TEXT NOT NULL, profile_version INTEGER NOT NULL,
      evidence_id INTEGER NOT NULL REFERENCES material_evidence(id), card_id INTEGER NOT NULL REFERENCES decision_cards(id),
      disclosed_at TEXT NOT NULL, known_at TEXT NOT NULL, frozen_at TEXT NOT NULL, scenario_json TEXT NOT NULL,
      scenarios_json TEXT NOT NULL, expected_value_krw REAL NOT NULL, legacy_note TEXT,
      UNIQUE(card_id, event_identity, version)
    );
    CREATE INDEX legacy_quantitative_note_idx ON event_scenario_sets(legacy_note);
    CREATE TRIGGER legacy_quantitative_note_trigger AFTER INSERT ON event_scenario_sets BEGIN SELECT 1; END;
    """)
    objects_before = dict(raw.execute("SELECT name, sql FROM sqlite_master WHERE name IN ('event_scenario_sets', 'legacy_quantitative_note_idx', 'legacy_quantitative_note_trigger')"))
    raw.close()

    db = dbmod.connect()
    assert {row["name"] for row in db.execute("PRAGMA table_info(event_scenario_sets)")} >= {"expected_value_krw", "legacy_note"}
    assert not {"scenario_kind", "scenario_schema_version"} & {row["name"] for row in db.execute("PRAGMA table_info(event_scenario_sets)")}
    evidence, card = _hold(db)
    conditional_json = dbmod.json.dumps([{"label": "BASE", "conditions": [{"text": "hold", "provenance": "test"}]}])
    for ident, version, frozen_at in ((1, 1, "2026-08-31T09:00:00+09:00"), (2, 2, "2026-09-04T09:00:00+09:00")):
        db.execute("""INSERT INTO event_conditional_scenario_sets(id,event_identity,version,symbol,evidence_id,card_id,disclosed_at,known_at,source_url,frozen_at,scenario_json,scenarios_json)
                      VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""", (ident, "CONDITIONAL:legacy", version, evidence["symbol"], evidence["id"], card["id"], "2026-08-30T09:00:00+09:00", "2026-08-31T09:00:00+09:00", "https://example.test/dart", frozen_at, "{}", conditional_json))

    from kr_stock_autotrader.event_scenarios import create, observe
    from tests.test_event_scenarios import _observation, payload
    first_payload = payload(evidence["id"], card["id"])
    first_payload["symbol"] = evidence["symbol"]
    first_payload["event_identity"] = "LEGACY:quantitative"; first = create(db, first_payload)
    second_payload = deepcopy(first_payload); second_payload["version"] = 2; second = create(db, second_payload)
    # Independent-table IDs are deliberately non-chronological: old quantitative IDs are high.
    db.execute("UPDATE event_scenario_sets SET id=99 WHERE id=?", (first["id"],))
    db.execute("UPDATE event_scenario_sets SET id=100 WHERE id=?", (second["id"],))
    db.commit()
    frozen_at = db.execute("SELECT frozen_at FROM event_scenario_sets WHERE id=100").fetchone()[0]
    from kr_stock_autotrader.domain import parse_kst
    import kr_stock_autotrader.event_scenarios as quantitative_scenarios
    monkeypatch.setattr(quantitative_scenarios, "now_kst", lambda: parse_kst(frozen_at) + timedelta(minutes=2))
    observation = _observation({**second, "frozen_at": frozen_at}, "legacy-observation", 100)
    observation["symbol"] = evidence["symbol"]
    observed = observe(db, "LEGACY:quantitative", observation)
    assert observed["active_scenario_label"] is not None
    quantitative_before = db.execute("SELECT id, quote(event_identity), quote(version), quote(frozen_at), quote(expected_value_krw), quote(scenarios_json), quote(legacy_note) FROM event_scenario_sets ORDER BY id").fetchall()

    view = user_card_view(db, card["id"], 1)["event_scenarios"]
    assert [(item["kind"], item["version"]) for item in view] == [("CONDITIONAL", 2), ("CONDITIONAL", 1), ("QUANTITATIVE", 2), ("QUANTITATIVE", 1)]
    assert view[0]["schema_version"] == 1 and view[0]["tracking_state"] == "UNOBSERVABLE"
    assert view[0]["current"]["active_scenario_label"] is None
    assert view[2]["schema_version"] == 1 and view[2]["tracking_state"] == "ACTIVE"
    assert view[2]["current"]["active_scenario_label"] is not None
    db.close()

    from fastapi.testclient import TestClient
    from kr_stock_autotrader.api import app
    client = TestClient(app)
    assert client.post("/api/signup", json={"email": "legacy-readback@example.test", "password": "long-password"}).status_code == 200
    response = client.get(f"/api/cards/{card['id']}")
    assert response.status_code == 200, response.text
    api_view = response.json()["event_scenarios"]
    assert api_view[0]["kind"] == "CONDITIONAL" and api_view[0]["schema_version"] == 1
    assert api_view[0]["tracking_state"] == "UNOBSERVABLE" and api_view[0]["current"]["active_scenario_label"] is None

    db = dbmod.connect()
    assert dict(db.execute("SELECT name, sql FROM sqlite_master WHERE name IN ('event_scenario_sets', 'legacy_quantitative_note_idx', 'legacy_quantitative_note_trigger')")) == objects_before
    assert db.execute("SELECT id, quote(event_identity), quote(version), quote(frozen_at), quote(expected_value_krw), quote(scenarios_json), quote(legacy_note) FROM event_scenario_sets ORDER BY id").fetchall() == quantitative_before
    db.close()


def test_fresh_quantitative_schema_reads_its_stored_kind_and_schema_version(monkeypatch, tmp_path):
    monkeypatch.setattr(dbmod, "DATABASE_PATH", str(tmp_path / "fresh-modern.db"))
    db = dbmod.connect(); evidence_id, card_id = _lineage(db)
    from kr_stock_autotrader.event_scenarios import create
    from tests.test_event_scenarios import payload
    created = create(db, payload(evidence_id, card_id))
    stored = db.execute("SELECT scenario_kind,scenario_schema_version FROM event_scenario_sets WHERE id=?", (created["id"],)).fetchone()
    view = user_card_view(db, card_id, 1)["event_scenarios"]
    assert tuple(stored) == ("QUANTITATIVE", 1)
    assert (view[0]["kind"], view[0]["schema_version"]) == tuple(stored)
    db.close()
