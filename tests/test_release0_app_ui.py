"""Release 0 authenticated app rendering and behavior contracts."""
import json
import os
import re
import subprocess
import uuid

from fastapi.testclient import TestClient

from app import app


def authenticated_app_html() -> str:
    client = TestClient(app)
    email = f"release0-ui-{uuid.uuid4().hex}@test.com"
    assert client.post("/api/signup", json={"email": email, "password": "long-password"}).status_code == 200
    response = client.get("/app")
    assert response.status_code == 200
    return response.text


def test_release0_primary_surface_is_change_first_and_record_only():
    html = authenticated_app_html()
    for expected in (
        "오늘 판단이 필요한 종목",
        "마지막 확인 이후 중요한 변화가 있는 종목만 표시합니다.",
        "무엇이 달라졌나",
        "현재 판단",
        "지금 할 일과 다음 확인 항목",
        "판단을 막는 누락·충돌",
        "조건별 시나리오",
        "출처와 변경 이력",
        "현재 단계: 기록 전용",
        "주문 기능 없음",
        "role=\"dialog\"",
        "aria-modal=\"true\"",
        "inert",
        "visibleFocusable",
        "operation_date=${day}",
        "api('cards/summary'+q),api('cards'+q),api('cards/missing'+q)",
        "min-height:44px",
        ":focus-visible",
        "prefers-reduced-motion",
    ):
        assert expected in html
    for forbidden in (
        "기본 모의투자 금액",
        "KIS 읽기전용 상태",
        "현재가 확인",
        "매수 승인",
        "수동 매도",
        "실전주문 사전점검",
        "주문 수량",
        "손절",
        "익절",
        "수익률",
        "시나리오 확률",
    ):
        assert forbidden not in html


def test_release0_renderer_uses_the_real_detail_trust_projection_and_fails_closed():
    html = authenticated_app_html()
    script = re.search(r"<script>(.*?)</script>", html, re.S).group(1)
    scenario = re.search(r"function trustedScenario.*?(?=function scenarioLabel)", script, re.S).group(0)
    payload = {
        "event_scenarios": [{"tracking_state": "ACTIVE", "trust": {"freshness": "FRESH", "context_readiness": "READY", "status": "READY"}, "current": {"active_scenario_label": "GOOD", "market_context_status": "VERIFIED", "match": "GOOD_MATCH", "known_at": "2026-09-04T09:00:00+09:00", "retrieved_at": "2026-09-04T09:01:00+09:00"}, "scenarios": [{"label": "GOOD"}, {"label": "BASE"}, {"label": "BAD"}]}]
    }
    blocked = {"event_scenarios": [{"tracking_state": "ACTIVE", "trust": {"freshness": "FRESH", "context_readiness": "BLOCKED", "status": "MISSING"}, "current": {"active_scenario_label": "GOOD", "market_context_status": "UNAVAILABLE", "match": "OUT_OF_RANGE"}, "scenarios": [{"label": "GOOD"}]}]}
    stale = {"event_scenarios": [{"tracking_state": "ACTIVE", "trust": {"freshness": "STALE", "context_readiness": "BLOCKED", "status": "STALE"}, "current": {"active_scenario_label": "GOOD", "market_context_status": "VERIFIED", "match": "GOOD_MATCH"}, "scenarios": [{"label": "GOOD"}]}]}
    out_of_range = {"event_scenarios": [{"tracking_state": "ACTIVE", "trust": {"freshness": "FRESH", "context_readiness": "READY", "status": "READY"}, "current": {"active_scenario_label": None, "market_context_status": "VERIFIED", "match": "OUT_OF_RANGE"}, "scenarios": [{"label": "GOOD"}]}]}
    node = scenario + "\nconsole.log(JSON.stringify([trustedScenario(" + json.dumps(payload) + "),trustedScenario(" + json.dumps(blocked) + "),trustedScenario(" + json.dumps(stale) + "),trustedScenario(" + json.dumps(out_of_range) + ")]));"
    output = subprocess.check_output(["node", "-e", node], text=True, env=os.environ).strip()
    assert json.loads(output) == ["GOOD", None, None, None]


def test_release0_detail_api_projects_trust_for_a_real_verified_match(monkeypatch, tmp_path):
    from kr_stock_autotrader import db as dbmod
    from kr_stock_autotrader.domain import parse_kst
    from tests.test_event_scenarios import _lineage, _observation, payload

    monkeypatch.setattr(dbmod, "DATABASE_PATH", str(tmp_path / "release0-trust.db"))
    monkeypatch.setenv("INTERNAL_API_KEY", "release0-trust-key")
    client = TestClient(app)
    assert client.post("/api/signup", json={"email": "release0-trust@test.com", "password": "long-password"}).status_code == 200
    db = dbmod.connect(); evidence_id, card_id = _lineage(db); db.close()
    created = client.post("/api/internal/scenario-sets", json=payload(evidence_id, card_id), headers={"X-Internal-API-Key": "release0-trust-key"}).json()
    ranges = {item["label"]: item["per_share_value_range_krw"] for item in created["scenarios"]}
    body = _observation(created, "real-current", sum(ranges["GOOD"].values()) / 2, seconds=1)
    import kr_stock_autotrader.event_scenarios as scenarios
    import kr_stock_autotrader.decision_cards as cards
    now = parse_kst(body["retrieved_at"])
    monkeypatch.setattr(scenarios, "now_kst", lambda: now)
    monkeypatch.setattr(cards, "now_kst", lambda: now)
    assert client.post(f"/api/internal/scenario-sets/{created['event_identity']}/observations", json=body, headers={"X-Internal-API-Key": "release0-trust-key"}).status_code == 200
    detail = client.get(f"/api/cards/{card_id}")
    assert detail.status_code == 200
    item = detail.json()["event_scenarios"][0]
    assert item["current"]["match"] == "GOOD_MATCH"
    assert item["trust"] == {"freshness": "FRESH", "context_readiness": "READY", "status": "READY"}
    html = authenticated_app_html()
    script = re.search(r"<script>(.*?)</script>", html, re.S).group(1)
    renderer = re.search(r"function trustedScenario.*?(?=function renderDetail)", script, re.S).group(0)
    node = "const korean=v=>String(v??'');const esc=v=>String(v??'');" + renderer + "\nconsole.log(JSON.stringify({label:trustedScenario(" + json.dumps(detail.json()) + "),current:(scenarios(" + json.dumps(detail.json()) + ").match(/scenario current/g)||[]).length}));"
    rendered = json.loads(subprocess.check_output(["node", "-e", node], text=True, env=os.environ).strip())
    assert rendered == {"label": "GOOD", "current": 1}
    from kr_stock_autotrader.decision_cards import _scenario_trust
    for change in (
        {"retrieved_at": "not-a-time"}, {"price_krw": float("nan")},
        {"provider": "unverified", "source": "unverified"},
        {"market_context_status": "UNAVAILABLE"}, {"match": "OUT_OF_RANGE", "active_scenario_label": None},
    ):
        assert _scenario_trust({**item["current"], **change}, invalidated=False)["status"] != "READY"
    stale_current = {**item["current"], "retrieved_at": "2026-09-04T08:55:59+09:00"}
    assert _scenario_trust(stale_current, invalidated=False)["status"] == "STALE"
    assert _scenario_trust(item["current"], invalidated=True)["status"] == "INVALIDATED"


def test_release0_detail_request_ownership_rejects_late_switch_refresh_close_and_failure():
    html = authenticated_app_html()
    script = re.search(r"<script>(.*?)</script>", html, re.S).group(1)
    ownership = re.search(r"function ownsDetail.*?(?=const today)", script, re.S).group(0)
    node = r'''
let detailGeneration=0,detailAbort=null,detailOwner=null,opener=null;
const nodes={
  '#detail':{hidden:true}, '#app-main':{inert:false,removeAttribute:()=>{},setAttribute:()=>{}},
  '#detail-title':{textContent:''}, '#detail-content':{innerHTML:''}, '#detail-close':{focus:()=>{}},
  '#error':{hidden:true,textContent:''}, '#date':{value:'2026-09-04'}, '#cards':{}
};
const $=s=>nodes[s];
const renderDetail=c=>'<h2>'+c.evidence.name+'</h2>';
const disclosure=()=>{};
const draw=()=>{};
const AbortController=class{constructor(){this.signal={aborted:false};this.abort=()=>{this.signal.aborted=true}}};
const button={focus:()=>{}};
const requests=[];
const api=(path,options={})=>new Promise((resolve,reject)=>requests.push({path,options,resolve,reject}));
const card=name=>({evidence:{name}});
''' + ownership.replace("function disclosure(id){const panel=$('#'+id+'-panel'),button=$('#'+id+'-toggle');button.addEventListener('click',()=>{panel.hidden=!panel.hidden;button.setAttribute('aria-expanded',String(!panel.hidden))})}\n", "") + r'''
(async()=>{
  const a=openDetail('A',button), b=openDetail('B',button);
  requests[1].resolve(card('B')); await b; requests[0].resolve(card('A')); await a;
  const switched={title:nodes['#detail-title'].textContent,body:nodes['#detail-content'].innerHTML,error:nodes['#error'].textContent,aborted:requests[0].options.signal.aborted};
  const refreshDetail=openDetail('R',button), loading=load();
  requests[2].resolve(card('R')); requests[3].resolve({}); requests[4].resolve([]); requests[5].resolve([]); await Promise.all([refreshDetail,loading]);
  const refreshed={title:nodes['#detail-title'].textContent,body:nodes['#detail-content'].innerHTML,aborted:requests[2].options.signal.aborted};
  const pending=openDetail('C',button); closeDetail(); requests[6].resolve(card('C')); await pending;
  const closed={hidden:nodes['#detail'].hidden,body:nodes['#detail-content'].innerHTML,aborted:requests[6].options.signal.aborted};
  const old=openDetail('D',button), newer=openDetail('D',button); requests[8].resolve(card('D-new')); await newer; requests[7].reject(Error('late failure')); await old;
  console.log(JSON.stringify({switched,refreshed,closed,error:nodes['#error'].textContent,owner:detailOwner}));
})().catch(error=>{console.error(error);process.exit(1)});
'''
    state = json.loads(subprocess.check_output(["node", "-e", node], text=True, env=os.environ).strip())
    assert state["switched"] == {"title": "B 판단 상세", "body": "<h2>B</h2>", "error": "", "aborted": True}
    assert state["refreshed"] == {"title": "B 판단 상세", "body": "<h2>B</h2>", "aborted": True}
    assert state["closed"] == {"hidden": True, "body": "<h2>B</h2>", "aborted": True}
    assert state["error"] == "" and state["owner"] == "D"
