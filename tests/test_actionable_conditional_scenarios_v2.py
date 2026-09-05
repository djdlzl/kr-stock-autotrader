"""Focused RED contracts for actionable conditional scenario v2."""
from copy import deepcopy
import math

import pytest
from fastapi.testclient import TestClient

from kr_stock_autotrader import db as dbmod
from kr_stock_autotrader.conditional_scenarios import build_scenario_candidate, create
from kr_stock_autotrader.decision_cards import card_detail, evidence_detail, user_card_view
from tests.test_conditional_scenarios import _hold


def _candidate(db, **card_patch):
    evidence, card = _hold(db)
    card["card"].update({"proof_point": "계약 이행 확인", "next_check": "다음 공시 확인", **card_patch})
    db.execute("UPDATE decision_cards SET card_json=? WHERE id=?", (dbmod.json.dumps(card["card"], ensure_ascii=False), card["id"])); db.commit()
    card = card_detail(db, card["id"])
    return evidence, card, build_scenario_candidate(evidence, card)


def test_v2_has_exact_actionable_shape_order_and_no_price_fabrication(monkeypatch, tmp_path):
    monkeypatch.setattr(dbmod, "DATABASE_PATH", str(tmp_path / "v2.db"))
    db = dbmod.connect(); _, _, candidate = _candidate(db)
    payload = candidate["payload"]
    assert payload["schema_version"] == 2
    assert [x["label"] for x in payload["scenarios"]] == ["BAD", "BASE", "GOOD"]
    for item in payload["scenarios"]:
        assert list(item) == ["label", "situation", "judgment", "actions", "price_criterion", "checks"]
        assert set(item["situation"]) == set(item["judgment"]) == {"text", "provenance"}
        assert set(item["actions"]) == {"no_position", "holding"}
        assert item["actions"]["no_position"] == "신규 진입 안 함"
        assert set(item["price_criterion"]) == {"value_krw", "basis", "provenance"}
        assert item["price_criterion"]["value_krw"] is None
        assert item["price_criterion"]["basis"] == "가격 미설정"
        assert item["price_criterion"]["provenance"] is None
    assert "원가와 마진" not in payload["scenarios"][2]["situation"]["text"]
    assert payload["scenarios"][2]["situation"]["provenance"] == "card.proof_point"
    db.close()


def test_v2_bad_has_invalidation_and_grounded_card_prices_only(monkeypatch, tmp_path):
    monkeypatch.setattr(dbmod, "DATABASE_PATH", str(tmp_path / "prices.db"))
    db = dbmod.connect()
    evidence, card, candidate = _candidate(db, price_cap=100, stop_loss=90, take_profit=[{"price": 120, "qty": 1}])
    assert candidate["status"] == "COMPLETE"
    by_label = {x["label"]: x for x in candidate["payload"]["scenarios"]}
    assert (by_label["BAD"]["price_criterion"], by_label["BASE"]["price_criterion"], by_label["GOOD"]["price_criterion"]) == (
        {"value_krw": 90.0, "basis": "손절 기준", "provenance": "card.stop_loss"},
        {"value_krw": 100.0, "basis": "진입 상한", "provenance": "card.price_cap"},
        {"value_krw": 120.0, "basis": "익절 기준", "provenance": "card.take_profit[0].price"},
    )
    assert "자동 매도 지시 없음" in by_label["BAD"]["actions"]["holding"]
    assert any(x["provenance"] == "card.evidence_invalidation" for x in by_label["BAD"]["checks"])
    for bad in (True, float("nan"), float("inf"), 0, -1, "90"):
        altered = deepcopy(card); altered["card"]["stop_loss"] = bad
        result = build_scenario_candidate(evidence, altered)
        assert result["payload"]["scenarios"][0]["price_criterion"]["value_krw"] is None
    db.close()


def test_v2_persists_from_card_save_read_model_and_rejects_forgery(monkeypatch, tmp_path):
    monkeypatch.setattr(dbmod, "DATABASE_PATH", str(tmp_path / "api.db")); db = dbmod.connect()
    evidence, card, candidate = _candidate(db)
    payload = candidate["payload"]; saved = create(db, payload)
    assert saved["scenarios"] == payload["scenarios"]
    db.execute("INSERT INTO users(email,password) VALUES('v2@example.test','p')"); db.commit()
    view = user_card_view(db, card["id"], 1)["event_scenarios"][0]
    assert view["schema_version"] == 2 and view["scenarios"] == payload["scenarios"]
    forged = deepcopy(payload); forged["scenarios"][0]["price_criterion"]["value_krw"] = 1
    with pytest.raises(Exception): create(db, forged)
    assert db.execute("SELECT COUNT(*) FROM order_plans").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM order_fills").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM positions").fetchone()[0] == 0
    db.close()


def test_v2_ui_escapes_and_renders_v1_and_v2_contracts():
    html = open("kr_stock_autotrader/decision_card_app.html", encoding="utf-8").read()
    assert "function actionableScenario" in html
    assert "가격 미설정" in html and "신규 진입 안 함" in html
    assert "s.scenario_version===2" in html
