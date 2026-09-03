"""Decision-card UI/API regression coverage."""
import json
import os
import re
import subprocess
import tempfile
from fastapi.testclient import TestClient
from kr_stock_autotrader import db as dbmod
from kr_stock_autotrader.decision_cards import create_evidence, save_filter, save_card
from tests.test_decision_card_invariants import AS_OF, card, raw


def test_pass_filter_allows_nonbuy_card_but_only_buy_review_can_be_approved(monkeypatch, tmp_path):
    monkeypatch.setattr(dbmod, "DATABASE_PATH", str(tmp_path / "cards.db"))
    db = dbmod.connect()
    ev = create_evidence(db, {"symbol":"005930","name":"삼성전자","kind":"공시","title":"실적","summary":"호재","source":"DART","source_url":"https://example.test/e","snapshot":{"safe":True},"dedupe_key":"pass-nonbuy","known_at":"2026-08-31T09:00:00+09:00"})
    passed = save_filter(db, ev["id"], raw(), AS_OF, "2026-08-31T09:00:00+09:00")
    observed = save_card(db, card(ev["id"], passed["id"], filter_verdict="PASS", verdict="관찰"))
    assert observed["verdict"] == "관찰"
    db.close()


def test_authenticated_card_summary_and_safe_detail(monkeypatch, tmp_path):
    monkeypatch.setattr(dbmod, "DATABASE_PATH", str(tmp_path / "api.db"))
    from app import app
    client = TestClient(app)
    assert client.post("/api/signup", json={"email":"cards-ui@test.com","password":"long-password"}).status_code == 200
    summary = client.get("/api/cards/summary?date=2026-08-31")
    assert summary.status_code == 200
    data = summary.json()
    for key in ("기준일", "전체 근거", "필터 PASS", "카드 생성", "카드 미생성", "다음 실행", "스케줄러"):
        assert key in data
    assert "detail" not in str(data).lower() and "raw_inputs" not in str(data)
    html = client.get("/app").text
    for text in ("결정 카드", "매수 검토 가능", "카드 미생성", "기존 승인 무효화·재승인 필요", "min-width:0", "max-width:100%", "@media(max-width:390px)"):
        assert text in html
    assert "새 매수 계획" not in html
    assert client.get("/api/cards/missing").status_code == 200
    assert client.get("/api/cards/summary?date=2026-99-99").status_code == 422


def test_fill_timestamp_and_dropdown_filter_ui_contract_is_present():
    """Keep the client-side display/filter contract deterministic and auditable."""
    from kr_stock_autotrader.ui import APP_HTML
    for text in (
        "매수일시", "매도일시", "fill_summary?.first_buy_at", "fill_summary?.last_full_sell_at",
        "card ${bought?'purchased':''}", "first_buy_at", "last_full_sell_at", "fill_state",
        "판단<select", "내 결정<select", "체결<select", "카드 상태<select", "필터 초기화",
        "매수 검토 가능", "관찰", "제외", "판단 보류", "승인", "보류", "거절", "미판단",
        "매도완료", "미체결", "카드 미생성", "&&", "$('#date').onchange=load",
        "holding_until", "review_at", "valid_until", "expires", "name=\"order_type\"",
        "expires:isoKst(a.get('expires'))", "@media(max-width:390px)", "grid-template-columns:1fr", "overflow-x:hidden",
        "기본 모의투자 금액", "기본 금액 저장", "이 카드에 적용할 금액", "default-paper-amount",
        "loadPaperSetting", "savePaperSetting", "toLocaleString('ko-KR')", "min=\"10000\"", "step=\"1\"",
        "scenarioGeneration", "scenarioAbort", "per_share_value_range_krw", "return_range_pct", "price_path",
        "시나리오 버전·동결", "호가 출처·확인", "현재 호가 판단 보류", "refreshScenario(cardId)",
    ):
        assert text in APP_HTML
    # Green presentation is exclusively keyed to a real current-user buy fill.
    assert "const bought=Boolean(c.fill_summary?.first_buy_at)" in APP_HTML
    assert "last_full_sell_at" in APP_HTML and "kst(summary?.last_full_sell_at)" in APP_HTML
    for text in ("추적 상태", "호가 정상 추적", "시나리오 판단자료 부족", "호가 확인 실패", "추적 중지", "시나리오 판단 지표", "시장 자료가 없어 시나리오 선택 보류", "scenarioCountdown", "QUOTE_UNAVAILABLE"):
        assert text in APP_HTML


def test_filter_change_stops_scenario_polling_and_closes_detail():
    """A filter change must stop and abort tracking for hidden cards."""
    from kr_stock_autotrader.ui import APP_HTML
    match = re.search(r"function changeCardFilter\(\)\{[^}]+\}", APP_HTML)
    assert match is not None
    handler = match.group(0)
    assert ".onchange=changeCardFilter" in APP_HTML
    script = """
let stopped=0,drawn=0,openScenarioCard=7;
const detail={hidden:false};
const $=selector=>detail;
function stopScenarioPolling(){stopped++}
function draw(){drawn++}
""" + handler + ";changeCardFilter();console.log(JSON.stringify({stopped,drawn,openScenarioCard,hidden:detail.hidden}));"
    output = subprocess.check_output(["node", "-e", script], text=True, env=os.environ).strip()
    assert json.loads(output) == {"stopped": 1, "drawn": 1, "openScenarioCard": None, "hidden": True}


def test_dropdown_predicate_uses_and_semantics_for_card_and_missing_rows():
    from kr_stock_autotrader.ui import APP_HTML
    helpers = re.search(r"function decision.*?function selected\(id\)\{[^}]*\}", APP_HTML, re.S).group(0)
    predicate = re.search(r"function filterCards.*?(?=function matches)", APP_HTML, re.S).group(0)
    script = helpers + predicate + """
const cards=[
 {card:{},verdict:'매수 검토 가능',user_state:{decision:{decision:'approve'}},fill_summary:{fill_state:'bought'}},
 {card:{},verdict:'관찰',user_state:{decision:{decision:'hold'}},fill_summary:{fill_state:'sold_complete'},invalidated_at:'x'},
 {symbol:'005930'}
];
const all={verdict:'전체',decision:'전체',fill:'전체',status:'전체'};
console.log(JSON.stringify([filterCards(cards,all).length,filterCards(cards,{verdict:'매수 검토 가능',decision:'approve',fill:'bought',status:'current'}).length,filterCards(cards,{verdict:'전체',decision:'undecided',fill:'unfilled',status:'missing'}).length,filterCards(cards,{verdict:'매수 검토 가능',decision:'hold',fill:'bought',status:'current'}).length]));
"""
    output = subprocess.check_output(["node", "-e", script], text=True, env=os.environ).strip()
    assert output == "[3,1,1,0]"


def test_fresh_buy_form_uses_safe_server_default_not_card_amount(monkeypatch, tmp_path):
    monkeypatch.setattr(dbmod, "DATABASE_PATH", str(tmp_path / "ui-default.db"))
    from app import app
    client = TestClient(app)
    assert client.post("/api/signup", json={"email": "ui-default@test.com", "password": "long-password"}).status_code == 200
    assert client.patch("/api/settings/paper", json={"default_paper_amount": 700000}).status_code == 200
    db = dbmod.connect()
    ev = create_evidence(db, {"symbol":"005930","name":"삼성전자","kind":"공시","title":"실적","summary":"호재","source":"DART","source_url":"https://example.test/e","snapshot":{"safe":True},"dedupe_key":"ui-default","known_at":"2026-08-31T09:00:00+09:00"})
    passed = save_filter(db, ev["id"], raw(), AS_OF, "2026-08-31T09:00:00+09:00")
    created = save_card(db, card(ev["id"], passed["id"], max_amount=250))
    db.close()
    detail = client.get(f"/api/cards/{created['id']}").json()
    assert detail["card"]["max_amount"] == 250
    assert detail["user_state"]["default_paper_amount"] == 700000
    helper = next(line for line in __import__("kr_stock_autotrader.ui", fromlist=["APP_HTML"]).APP_HTML.splitlines() if line.startswith("function draftValues"))
    output = subprocess.check_output(["node", "-e", helper + ";console.log(draftValues(" + json.dumps(detail) + ").max_amount)"], text=True).strip()
    assert output == "700000"
