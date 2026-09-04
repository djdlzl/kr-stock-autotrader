"""RED/GREEN contracts for evidence-only conditional judgment-hold scenarios."""
from copy import deepcopy

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
    assert create(db, successor)["version"] == 2
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
    assert dry["eligible_cards"] == 1 and dry["created"] == 0 and dry["items"] == [{"card_id": active["id"], "status": "WOULD_CREATE", "event_identity": f"CONDITIONAL:{active['evidence_id']}:{active['id']}"}]
    assert db.execute("SELECT COUNT(*) FROM event_conditional_scenario_sets").fetchone()[0] == 0
    applied = run(db, apply=True)
    assert applied["created"] == 1 and db.execute("SELECT COUNT(*) FROM event_conditional_scenario_sets WHERE card_id=?", (invalidated["id"],)).fetchone()[0] == 0
    assert run(db, apply=True)["idempotent"] == 1
    assert all(db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0 for table in ("order_plans", "order_fills", "positions"))
    db.close()
