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


def test_release0_source_facts_use_persisted_api_projection_for_missing_source(monkeypatch, tmp_path):
    from kr_stock_autotrader import db as dbmod
    from tests.test_date_lifecycle import _seed

    monkeypatch.setattr(dbmod, "DATABASE_PATH", str(tmp_path / "source-facts-missing.db"))
    db = dbmod.connect()
    evidence, saved = _seed(db, "source-facts-missing", "2026-08-31T10:00:00+09:00")
    db.close()
    client = TestClient(app)
    assert client.post("/api/signup", json={"email": "source-facts-missing@test.com", "password": "long-password"}).status_code == 200
    normal = client.get(f"/api/cards/{saved['id']}")
    assert normal.status_code == 200
    assert normal.json()["source_facts"] == {
        "source": "DART", "source_url": "https://example.test/e",
        "known_at": "2026-08-31T10:00:00+09:00", "last_success_at": "2026-08-31T10:00:00+09:00",
        "trust_state": "확인됨",
    }

    script = re.search(r"<script>(.*?)</script>", client.get("/app").text, re.S).group(1)
    renderer = re.search(r"function safeSourceLink.*?(?=function trustedScenario)", script, re.S).group(0)
    normal_node = "const esc=v=>String(v??'');const kst=v=>String(v??'');" + renderer + "\nconsole.log(sourceFacts(" + json.dumps(normal.json()) + "));"
    normal_rendered = subprocess.check_output(["node", "-e", normal_node], text=True, env=os.environ).strip()
    assert 'href="https://example.test/e"' in normal_rendered and normal_rendered.count("원문 보기") == 1

    db = dbmod.connect()
    db.execute("UPDATE material_evidence SET source='', source_url=NULL WHERE id=?", (evidence["id"],))
    db.commit(); db.close()
    detail = client.get(f"/api/cards/{saved['id']}")
    assert detail.status_code == 200
    payload = detail.json()
    assert {key: payload["evidence"][key] for key in ("source", "source_url", "status")} == {"source": "", "source_url": None, "status": "card_generated"}

    script = re.search(r"<script>(.*?)</script>", client.get("/app").text, re.S).group(1)
    renderer = re.search(r"function safeSourceLink.*?(?=function trustedScenario)", script, re.S).group(0)
    node = "const esc=v=>String(v??'');const kst=v=>String(v??'');" + renderer + "\nconsole.log(sourceFacts(" + json.dumps(payload) + "));"
    rendered = subprocess.check_output(["node", "-e", node], text=True, env=os.environ).strip()
    assert "신뢰 상태: 확인됨" not in rendered
    assert "신뢰 상태: 확인 필요" in rendered


def test_source_fact_projection_maps_only_authoritative_repository_reasons_fail_closed():
    from kr_stock_autotrader.decision_cards import _source_facts

    evidence = {
        "source": "DART", "source_url": "https://example.test/original",
        "known_at": "2026-08-31T10:00:00+09:00", "collected_at": "2026-08-31T10:01:00+09:00",
        "status": "card_generated", "snapshot": {"secret": "must-not-project"},
    }
    assert _source_facts(evidence, {"reasons": []}, invalidated=False) == {
        "source": "DART", "source_url": "https://example.test/original",
        "known_at": "2026-08-31T10:00:00+09:00", "last_success_at": "2026-08-31T10:01:00+09:00",
        "trust_state": "확인됨",
    }
    assert _source_facts(evidence, {"reasons": ["known_at future or stale"]}, invalidated=False)["trust_state"] == "정보가 오래됨"
    assert _source_facts(evidence, {"reasons": ["conflicting or recycled disclosure"]}, invalidated=False)["trust_state"] == "출처 간 내용이 다름"
    assert _source_facts(evidence, {"reasons": ["unrecognized persisted reason"]}, invalidated=False)["trust_state"] == "확인 필요"
    assert _source_facts(evidence, {"reasons": "unreadable"}, invalidated=False)["trust_state"] == "확인 필요"
    assert _source_facts({**evidence, "status": "new"}, {"reasons": []}, invalidated=False)["trust_state"] == "확인 필요"
    assert _source_facts({**evidence, "source_url": "javascript:alert(1)"}, {"reasons": []}, invalidated=False)["trust_state"] == "확인 필요"
    assert _source_facts({**evidence, "known_at": "not-a-time"}, {"reasons": []}, invalidated=False)["trust_state"] == "확인 필요"
    assert _source_facts({**evidence, "source": ""}, {"reasons": []}, invalidated=False)["trust_state"] == "확인 필요"
    assert _source_facts(evidence, {"reasons": []}, invalidated=True)["trust_state"] == "가설 폐기 조건 충족"


def test_source_fact_url_and_timestamp_controls_fail_closed(monkeypatch):
    import kr_stock_autotrader.decision_cards as cards
    from kr_stock_autotrader.domain import parse_kst

    now = parse_kst("2026-09-04T12:00:00+09:00")
    monkeypatch.setattr(cards, "now_kst", lambda: now)
    evidence = {
        "source": "DART", "source_url": "https://example.test/original?notice=1",
        "known_at": "2026-09-04T10:00:00+09:00", "collected_at": "2026-09-04T10:01:00+09:00",
        "status": "card_generated",
    }
    assert cards._source_facts(evidence, {"reasons": []}, invalidated=False)["trust_state"] == "확인됨"
    for unsafe in (
        "https://trusted.example@evil.example/original", "https://user:password@example.test/original",
        "https://%65vil.example/original", "https:///no-host", "//example.test/original",
        "https://example.test\\evil/original", "https://example.test:bad/original", "https://example.test\x01/original",
    ):
        assert cards._safe_source_url(unsafe) is None
        assert cards._source_facts({**evidence, "source_url": unsafe}, {"reasons": []}, invalidated=False)["trust_state"] == "확인 필요"
    future = cards._source_facts({**evidence, "known_at": "2026-09-04T12:00:01+09:00", "collected_at": "2026-09-04T12:01:00+09:00"}, {"reasons": []}, invalidated=False)
    assert future == {"source": "DART", "source_url": "https://example.test/original?notice=1", "known_at": None, "last_success_at": None, "trust_state": "확인 필요"}
    chronology = cards._source_facts({**evidence, "known_at": "2026-09-04T10:01:00+09:00", "collected_at": "2026-09-04T10:00:00+09:00"}, {"reasons": []}, invalidated=False)
    assert chronology["known_at"] is None and chronology["last_success_at"] is None and chronology["trust_state"] == "확인 필요"
    offset = cards._source_facts({**evidence, "known_at": "2026-09-04T01:00:00+00:00", "collected_at": "2026-09-04T10:01:00+09:00"}, {"reasons": []}, invalidated=False)
    assert offset["trust_state"] == "확인됨"
    stale = cards._source_facts({**evidence, "known_at": "2026-09-04T12:00:01+09:00", "collected_at": "2026-09-04T12:01:00+09:00"}, {"reasons": ["known_at future or stale"]}, invalidated=False)
    assert stale["known_at"] is None and stale["last_success_at"] is None and stale["trust_state"] == "정보가 오래됨"


def test_release0_persisted_source_url_and_timestamp_trust_regressions(monkeypatch, tmp_path):
    import kr_stock_autotrader.decision_cards as cards
    from kr_stock_autotrader import db as dbmod
    from kr_stock_autotrader.domain import parse_kst
    from tests.test_date_lifecycle import _seed

    monkeypatch.setattr(dbmod, "DATABASE_PATH", str(tmp_path / "source-facts-hostile.db"))
    monkeypatch.setattr(cards, "now_kst", lambda: parse_kst("2026-09-04T12:00:00+09:00"))
    db = dbmod.connect()
    evidence, saved = _seed(db, "source-facts-hostile", "2026-09-04T10:00:00+09:00", collected_at="2026-09-04T10:01:00+09:00")
    db.close()
    client = TestClient(app)
    assert client.post("/api/signup", json={"email": "source-facts-hostile@test.com", "password": "long-password"}).status_code == 200
    script = re.search(r"<script>(.*?)</script>", client.get("/app").text, re.S).group(1)
    renderer = re.search(r"function safeSourceLink.*?(?=function trustedScenario)", script, re.S).group(0)
    for source_url in ("https://trusted.example@evil.example/original", "https://%65vil.example/original"):
        db = dbmod.connect(); db.execute("UPDATE material_evidence SET source_url=? WHERE id=?", (source_url, evidence["id"])); db.commit(); db.close()
        payload = client.get(f"/api/cards/{saved['id']}").json()
        assert payload["source_facts"]["source_url"] is None
        assert payload["source_facts"]["trust_state"] == "확인 필요"
        node = "const esc=v=>String(v??'');const kst=v=>String(v??'');" + renderer + "\nconsole.log(sourceFacts(" + json.dumps(payload) + "));"
        rendered = subprocess.check_output(["node", "-e", node], text=True, env=os.environ).strip()
        assert "원문 보기" not in rendered and "신뢰 상태: 확인 필요" in rendered
    db = dbmod.connect(); db.execute("UPDATE material_evidence SET source_url=?, known_at=?, collected_at=? WHERE id=?", ("https://example.test/original", "2026-09-04T12:00:01+09:00", "2026-09-04T12:01:00+09:00", evidence["id"])); db.commit(); db.close()
    payload = client.get(f"/api/cards/{saved['id']}").json()
    assert payload["source_facts"] == {"source": "DART", "source_url": "https://example.test/original", "known_at": None, "last_success_at": None, "trust_state": "확인 필요"}
    node = "const esc=v=>String(v??'');const kst=v=>String(v??'');" + renderer + "\nconsole.log(sourceFacts(" + json.dumps(payload) + "));"
    rendered = subprocess.check_output(["node", "-e", node], text=True, env=os.environ).strip()
    assert "원문 보기" in rendered and "신뢰 상태: 확인 필요" in rendered and "2026" not in rendered
    tampered = {"source_facts": {"source": "DART", "source_url": "https://trusted.example@evil.example/original", "known_at": "2026-09-04T10:00:00+09:00", "last_success_at": "2026-09-04T10:01:00+09:00", "trust_state": "확인됨"}}
    node = "const esc=v=>String(v??'');const kst=v=>String(v??'');" + renderer + "\nconsole.log(sourceFacts(" + json.dumps(tampered) + "));"
    rendered = subprocess.check_output(["node", "-e", node], text=True, env=os.environ).strip()
    assert "원문 보기" not in rendered and "신뢰 상태: 확인 필요" in rendered


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
