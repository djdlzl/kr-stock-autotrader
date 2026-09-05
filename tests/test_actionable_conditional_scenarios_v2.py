"""Executable contracts for actionable conditional scenarios v2 and legacy display."""
from copy import deepcopy
import json
import os
import subprocess

import pytest
from fastapi.testclient import TestClient

from kr_stock_autotrader import db as dbmod
from kr_stock_autotrader.conditional_scenarios import build_scenario_candidate, create
from kr_stock_autotrader.decision_cards import card_detail, evidence_detail, user_card_view
from tests.test_conditional_scenarios import _hold


def _candidate(db, **patch):
    evidence, card = _hold(db)
    card["card"].update({"proof_point": "계약 이행 확인", "next_check": "다음 공시 확인", **patch})
    db.execute("UPDATE decision_cards SET card_json=? WHERE id=?", (dbmod.json.dumps(card["card"], ensure_ascii=False), card["id"])); db.commit()
    return evidence, card_detail(db, card["id"]), build_scenario_candidate(evidence, card_detail(db, card["id"]))


def test_v2_policy_actions_prices_and_canonical_forgery_rejection(monkeypatch, tmp_path):
    monkeypatch.setattr(dbmod, "DATABASE_PATH", str(tmp_path / "policy.db")); db = dbmod.connect()
    evidence, card, candidate = _candidate(db, price_cap=100, stop_loss=90, take_profit=[{"price": 120, "qty": 1}])
    assert candidate["status"] == "COMPLETE"
    by_label = {x["label"]: x for x in candidate["payload"]["scenarios"]}
    assert [by_label[x]["judgment"] for x in ("BAD", "BASE", "GOOD")] == [
        {"text":"투자 논지가 약화되거나 무효화되었습니다.","provenance":"derived.policy_v2"},
        {"text":"확인된 사실 범위에서 현재 판단을 유지합니다.","provenance":"derived.policy_v2"},
        {"text":"긍정적 증거가 확인될 때에만 투자 논지가 강화됩니다.","provenance":"derived.policy_v2"},
    ]
    assert by_label["BAD"]["actions"]["holding"] == "90원 이하 축소·매도 검토"
    assert by_label["BASE"]["actions"] == {"no_position":"100원 이하에서만 신규 진입 검토", "holding":"확인된 사실 범위에서 보유 유지·다음 확인"}
    assert by_label["GOOD"]["actions"]["holding"] == "120원 이상 분할매도 검토"
    assert [by_label[x]["price_criterion"]["provenance"] for x in ("BAD", "BASE", "GOOD")] == ["card.stop_loss", "card.price_cap", "card.take_profit[0].price"]
    payload = candidate["payload"]
    for field, value in (("judgment", {"text":"위조","provenance":"derived.policy_v2"}), ("actions", {"no_position":"위조","holding":"위조"}), ("price_criterion", {"value_krw":90,"basis":"손절 기준","provenance":"card.price_cap"}), ("checks", [])):
        forged = deepcopy(payload); forged["scenarios"][0][field] = value
        with pytest.raises(Exception): create(db, forged)
    assert create(db, payload)["scenarios"] == payload["scenarios"]
    assert all(db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0 for table in ("order_plans", "order_fills", "positions"))
    db.close()


def test_card_save_edd_creates_hold_no_price_and_buy_review_grounded_prices(monkeypatch, tmp_path):
    monkeypatch.setattr(dbmod, "DATABASE_PATH", str(tmp_path / "save.db"))
    from tests.test_decision_cards import card as card_payload, evidence as create_evidence, inputs
    from kr_stock_autotrader.decision_cards import save_card, save_filter
    db = dbmod.connect(); evidence = create_evidence(db)
    db.execute("UPDATE material_evidence SET source_url=?,announcement_at=? WHERE id=?", ("https://example.test/dart", "2026-08-30T09:00:00+09:00", evidence["id"])); db.commit()
    filt = save_filter(db, evidence["id"], inputs(), "2026-08-31T09:00:00+09:00", "2026-08-31T08:00:00+09:00")
    hold = card_payload(evidence["id"], filt["id"]); hold["card"].update({"verdict":"판단 보류","price_cap":None,"window":None,"max_amount":None,"max_qty":None,"stop_loss":None,"take_profit":None,"holding_until":None,"review_at":None,"valid_until":None,"expires":None,"order_type":None,"proof_point":"계약 이행 확인","next_check":"다음 공시 확인"})
    saved_hold = save_card(db, hold)
    hold_item = user_card_view(db, saved_hold["id"], 1)["event_scenarios"][0]
    assert all(x["price_criterion"]["value_krw"] is None for x in hold_item["scenarios"])
    buy = card_payload(evidence["id"], filt["id"]); buy["lineage_key"] = "buy-review"; buy["card"].update({"proof_point":"계약 이행 확인","next_check":"다음 공시 확인"})
    saved_buy = save_card(db, buy)
    client = TestClient(__import__("kr_stock_autotrader.api", fromlist=["app"]).app)
    assert client.post("/api/signup", json={"email":"buy-review@example.test","password":"long-password"}).status_code == 200
    api_item = client.get(f"/api/cards/{saved_buy['id']}").json()["event_scenarios"][0]
    assert [x["price_criterion"]["value_krw"] for x in api_item["scenarios"]] == [95.0, 100.0, 110.0]
    assert all(db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0 for table in ("order_plans", "order_fills", "positions"))
    db.close()


def test_incomplete_and_noneligible_cards_do_not_generate(monkeypatch, tmp_path):
    monkeypatch.setattr(dbmod, "DATABASE_PATH", str(tmp_path / "closed.db")); db = dbmod.connect()
    evidence, card, _ = _candidate(db)
    for patch in ({"proof_point":None}, {"next_check":None}):
        altered=deepcopy(card); altered["card"].update(patch)
        result=build_scenario_candidate(evidence, altered)
        assert result["status"] == "SCENARIO_INPUTS_REQUIRED"
    db.close()


def _js_renderer(payload):
    html=open("kr_stock_autotrader/decision_card_app.html", encoding="utf-8").read()
    source=html.split("<script>",1)[1].split("</script>",1)[0]
    start=source.index("const $="); end=source.index("function scenarios")
    harness=source[start:end] + "\nconsole.log(JSON.stringify({v2: actionableScenario(INPUT.v2), v1: legacyActionableScenario(INPUT.v1)}));"
    return json.loads(subprocess.run(["node", "-e", harness], input=json.dumps(payload), text=True, capture_output=True, check=True, env={"INPUT": json.dumps(payload)}).stdout)


def test_extracted_js_dom_runtime_renders_v1_adapter_and_v2_actions_safely():
    payload={"v2":{"situation":{"text":"<상황>","provenance":"card.proof_point"},"judgment":{"text":"판단","provenance":"derived.policy_v2"},"actions":{"no_position":"100원 이하에서만 신규 진입 검토","holding":"120원 이상 분할매도 검토"},"price_criterion":{"value_krw":100,"basis":"진입 상한","provenance":"card.price_cap"},"checks":[{"text":"<확인>","provenance":"card.next_check"}]},"v1":{"label":"GOOD","conditions":[{"text":"<미확인>","provenance":"card.unknowns"},{"text":"<목록 미확인>","provenance":"card.unknowns[0]"},{"text":"<중첩 미확인>","provenance":"card.unknowns.items[0]"},{"text":"가짜 미확인 1","provenance":"card.unknownship"},{"text":"가짜 미확인 2","provenance":"card.unknownsFake"},{"text":"가짜 미확인 3","provenance":"card.unknowns_extra"},{"text":"가짜 상방","provenance":"card.proof_point"}]},"bad":{"label":"BAD","conditions":[{"text":"계약 취소","provenance":"card.evidence_invalidation"}]},"base":{"label":"BASE","conditions":[{"text":"<확정 사실>","provenance":"evidence.summary"}]}}
    # Feed the real extracted script without a browser; renderer input is only data.
    html=open("kr_stock_autotrader/decision_card_app.html", encoding="utf-8").read(); source=html.split("<script>",1)[1].split("</script>",1)[0]
    start=source.index("const $="); end=source.index("function scenarios")
    harness="const INPUT=JSON.parse(process.env.INPUT);"+source[start:end]+";console.log(JSON.stringify({v2:actionableScenario(INPUT.v2),v1:legacyActionableScenario(INPUT.v1),bad:legacyActionableScenario(INPUT.bad),base:legacyActionableScenario(INPUT.base)}));"
    env={**os.environ, "INPUT":json.dumps(payload)}
    rendered=json.loads(subprocess.run(["node","-e",harness],env=env,text=True,capture_output=True,check=True).stdout)
    for output in rendered.values():
        assert [output.index(x) for x in ("상황","판단","미보유","보유","가격 기준","확인·무효화")] == sorted(output.index(x) for x in ("상황","판단","미보유","보유","가격 기준","확인·무효화"))
    assert "&lt;상황&gt;" in rendered["v2"] and "100원 · 진입 상한" in rendered["v2"]
    assert "구형 시나리오" in rendered["v1"] and "상방 시나리오 미생성" in rendered["v1"] and "가격 미설정" in rendered["v1"]
    assert "계약 취소" in rendered["bad"] and "&lt;확정 사실&gt;" in rendered["base"]
    assert "가짜 상방" not in rendered["v1"] and "&lt;미확인&gt;" in rendered["v1"]
    assert "&lt;목록 미확인&gt;" in rendered["v1"] and "&lt;중첩 미확인&gt;" in rendered["v1"]
    assert all(value not in rendered["v1"] for value in ("가짜 미확인 1", "가짜 미확인 2", "가짜 미확인 3"))
    assert ".scenario-actionable" in html and "overflow-wrap:anywhere" in html and "@media(max-width:520px)" in html
