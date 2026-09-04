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
    zulu = cards._source_facts({**evidence, "known_at": "2026-09-04T01:00:00Z", "collected_at": "2026-09-04T01:01:00Z"}, {"reasons": []}, invalidated=False)
    assert zulu["trust_state"] == "확인됨"
    naive = cards._source_facts({**evidence, "known_at": "2026-09-04T10:00:00", "collected_at": "2026-09-04T10:01:00"}, {"reasons": []}, invalidated=False)
    assert naive == {"source": "DART", "source_url": "https://example.test/original?notice=1", "known_at": None, "last_success_at": None, "trust_state": "확인 필요"}
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


def test_release0_persisted_naive_timestamp_and_ipv6_renderer_regressions(monkeypatch, tmp_path):
    import kr_stock_autotrader.decision_cards as cards
    from kr_stock_autotrader import db as dbmod
    from kr_stock_autotrader.domain import parse_kst
    from tests.test_date_lifecycle import _seed

    monkeypatch.setattr(dbmod, "DATABASE_PATH", str(tmp_path / "source-facts-naive-ipv6.db"))
    monkeypatch.setattr(cards, "now_kst", lambda: parse_kst("2026-09-04T12:00:00+09:00"))
    db = dbmod.connect()
    evidence, saved = _seed(db, "source-facts-naive-ipv6", "2026-09-04T10:00:00+09:00", collected_at="2026-09-04T10:01:00+09:00")
    db.close()
    client = TestClient(app)
    assert client.post("/api/signup", json={"email": "source-facts-naive-ipv6@test.com", "password": "long-password"}).status_code == 200
    script = re.search(r"<script>(.*?)</script>", client.get("/app").text, re.S).group(1)
    renderer = re.search(r"function safeSourceLink.*?(?=function trustedScenario)", script, re.S).group(0)

    db = dbmod.connect()
    db.execute("UPDATE material_evidence SET known_at=?, collected_at=? WHERE id=?", ("2026-09-04T10:00:00", "2026-09-04T10:01:00", evidence["id"]))
    db.commit(); db.close()
    payload = client.get(f"/api/cards/{saved['id']}").json()
    assert payload["source_facts"] == {"source": "DART", "source_url": "https://example.test/e", "known_at": None, "last_success_at": None, "trust_state": "확인 필요"}
    node = "const esc=v=>String(v??'');const kst=v=>String(v??'');" + renderer + "\nconsole.log(sourceFacts(" + json.dumps(payload) + "));"
    rendered = subprocess.check_output(["node", "-e", node], text=True, env=os.environ).strip()
    assert "원문 보기" in rendered and "신뢰 상태: 확인됨" not in rendered and "신뢰 상태: 확인 필요" in rendered and "2026" not in rendered

    ipv6 = "https://[2001:db8::1]:8443/path?q=one"
    db = dbmod.connect()
    db.execute("UPDATE material_evidence SET source_url=?, known_at=?, collected_at=? WHERE id=?", (ipv6, "2026-09-04T10:00:00+09:00", "2026-09-04T10:01:00+09:00", evidence["id"]))
    db.commit(); db.close()
    payload = client.get(f"/api/cards/{saved['id']}").json()
    assert payload["source_facts"]["source_url"] == ipv6 and payload["source_facts"]["trust_state"] == "확인됨"
    node = "const esc=v=>String(v??'');const kst=v=>String(v??'');" + renderer + "\nconsole.log(sourceFacts(" + json.dumps(payload) + "));"
    rendered = subprocess.check_output(["node", "-e", node], text=True, env=os.environ).strip()
    assert rendered.count("원문 보기") == 1 and "신뢰 상태: 확인됨" in rendered

    for hostile in ("https://[2001:db8::1]@evil.example/path", "https://[2001:db8::1]:bad/path"):
        tampered = {"source_facts": {"source": "DART", "source_url": hostile, "known_at": "2026-09-04T10:00:00+09:00", "last_success_at": "2026-09-04T10:01:00+09:00", "trust_state": "확인됨"}}
        node = "const esc=v=>String(v??'');const kst=v=>String(v??'');" + renderer + "\nconsole.log(sourceFacts(" + json.dumps(tampered) + "));"
        rendered = subprocess.check_output(["node", "-e", node], text=True, env=os.environ).strip()
        assert "원문 보기" not in rendered and "신뢰 상태: 확인 필요" in rendered


def test_release0_detail_request_ownership_rejects_late_switch_refresh_close_and_failure():
    html = authenticated_app_html()
    script = re.search(r"<script>(.*?)</script>", html, re.S).group(1)
    ownership = re.search(r"function ownsDetail.*?(?=const today)", script, re.S).group(0)
    node = r'''
let detailGeneration=0,detailAbort=null,detailOwner=null,opener=null,sheetCloseTimer=0,sheetCloseFinish=null;
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


def test_release0_persisted_blockers_keep_string_items_whole_and_fail_closed(monkeypatch, tmp_path):
    """Legacy string unknowns must survive the authenticated API-to-renderer path intact."""
    from kr_stock_autotrader import db as dbmod
    from tests.test_date_lifecycle import _seed

    monkeypatch.setattr(dbmod, "DATABASE_PATH", str(tmp_path / "blocker-normalization.db"))
    db = dbmod.connect()
    evidence, saved = _seed(db, "hanwha-ocean-blockers", "2026-08-31T10:00:00+09:00")
    unknowns = "원가, 마진, 실제 매출 인식 시점, 당일 시가·갭·거래량; backend short-term 임계값·provenance 누락"
    db.execute("UPDATE decision_cards SET card_json=json_set(card_json, '$.unknowns', json(?)) WHERE id=?", (json.dumps(unknowns, ensure_ascii=False), saved["id"]))
    db.execute("UPDATE deterministic_filter_results SET reasons=? WHERE id=?", (json.dumps(["market cap outside range"]), saved["filter_id"]))
    db.commit(); db.close()

    client = TestClient(app)
    assert client.post("/api/signup", json={"email": "blocker-normalization@test.com", "password": "long-password"}).status_code == 200
    detail = client.get(f"/api/cards/{saved['id']}")
    assert detail.status_code == 200
    assert detail.json()["card"]["unknowns"] == unknowns
    assert detail.json()["filter"]["reasons"] == ["market cap outside range"]

    script = re.search(r"<script>(.*?)</script>", client.get("/app").text, re.S).group(1)
    korean = re.search(r"function korean.*?(?=function blockerItems)", script, re.S).group(0)
    normalizer = re.search(r"function blockerItems.*?(?=function kst)", script, re.S).group(0)
    renderer = re.search(r"function renderDetail.*?(?=function visibleFocusable)", script, re.S).group(0)
    node = (
        "const esc=v=>String(v??'');const scenarios=()=>'';const sourceFacts=()=>'';"
        + korean + normalizer + renderer
        + "\nconsole.log(JSON.stringify([renderDetail(" + json.dumps(detail.json()) + "),"
        + "renderDetail({card:{unknowns:['첫 항목','둘째 항목',null,7,{}]},filter:{reasons:['셋째 항목',false,{}]}})]));"
    )
    persisted, controls = json.loads(subprocess.check_output(["node", "-e", node], text=True, env=os.environ).strip())
    primary = re.search(r"<h3>판단을 막는 누락·충돌</h3><p>(.*?)</p>", persisted).group(1)
    expected = "원가, 마진, 실제 매출 인식 시점, 당일 시가·갭·거래량; 시스템 단기 기준값·근거 정보 누락 · 시가총액이 조건 범위를 벗어남"
    assert primary == expected
    assert "backend" not in primary
    assert "short-term" not in primary
    assert "provenance" not in primary
    assert "원 · 가" not in primary
    assert "시 · 가" not in primary
    assert "시가·갭·거래량" in primary
    assert "시가 · 갭" not in primary
    assert "시가총액이 조건 범위를 벗어남" in primary
    assert "market cap outside range" not in primary
    assert "첫 항목 · 둘째 항목 · 7 · 셋째 항목" in controls
    assert "[object Object]" not in controls


def test_release0_bottom_sheet_transition_lifecycle_contract():
    """EDD: only the sheet transform can finish one animated close; reopen cancels stale completion."""
    html = authenticated_app_html()
    script = re.search(r"<script>(.*?)</script>", html, re.S).group(1)
    lifecycle = re.search(r"function animatedSheet.*?(?=function disclosure)", script, re.S).group(0)
    node = r'''
let sheetCloseTimer=0,sheetCloseFinish=null,sheetCloseNode=null,focuses=0,nextTimer=0;const timers=new Map();
const cls=()=>({items:new Set(),add(x){this.items.add(x)},remove(...x){x.forEach(y=>this.items.delete(y))},contains(x){return this.items.has(x)}});
const sheetListeners={},dialogListeners={};const sheet={style:{},addEventListener:(type,fn)=>sheetListeners[type]=fn,removeEventListener:(type,fn)=>{if(sheetListeners[type]===fn)delete sheetListeners[type]}};
const dialog={hidden:false,classList:cls(),querySelector:()=>sheet,addEventListener:(type,fn)=>dialogListeners[type]=fn,removeEventListener:(type,fn)=>{if(dialogListeners[type]===fn)delete dialogListeners[type]}};const main={inert:false,setAttribute:()=>{},removeAttribute:()=>{}};
const close={focus:()=>focuses++};const nodes={'#detail':dialog,'#app-main':main,'#detail-close':close,'#detail-title':{textContent:''},'#detail-content':{innerHTML:''}};const $=s=>nodes[s];
let opener={focus:()=>focuses++};const invalidateDetail=()=>{};let reduced=false;const matchMedia=()=>({matches:reduced});
const setTimeout=(fn)=>{const id=++nextTimer;timers.set(id,fn);return id};const clearTimeout=id=>timers.delete(id);const requestAnimationFrame=fn=>fn();
''' + lifecycle + r'''
closeDetail();const descendant=sheetListeners.transitionend||dialogListeners.transitionend;descendant({target:{},propertyName:'opacity'});const afterDescendant={hidden:dialog.hidden,focuses,listener:Boolean(sheetListeners.transitionend)};
descendant({target:sheet,propertyName:'transform'});const afterTransform={hidden:dialog.hidden,focuses,listener:Boolean(sheetListeners.transitionend)};
dialog.hidden=false;showDetail('timer','content',opener);closeDetail();const timerListener=sheetListeners.transitionend,timer=timers.get(sheetCloseTimer);timer();timerListener({target:sheet,propertyName:'transform'});const singleShot={hidden:dialog.hidden,focuses};
dialog.hidden=false;showDetail('new','content',opener);closeDetail();const oldTimer=sheetCloseTimer;showDetail('replacement','content',opener);const staleListener=sheetListeners.transitionend;const staleTimer=timers.get(oldTimer);if(staleListener)staleListener({target:sheet,propertyName:'transform'});if(staleTimer)staleTimer();const reopened={hidden:dialog.hidden,focuses,listener:Boolean(sheetListeners.transitionend),timer:sheetCloseTimer};
reduced=true;showDetail('reduced','content',opener);closeDetail();const reducedMotion={hidden:dialog.hidden,focuses,listener:Boolean(sheetListeners.transitionend),timer:sheetCloseTimer};
console.log(JSON.stringify({afterDescendant,afterTransform,singleShot,reopened,reducedMotion}));
'''
    state = json.loads(subprocess.check_output(["node", "-e", node], text=True, env=os.environ).strip())
    assert state == {
        "afterDescendant": {"hidden": False, "focuses": 0, "listener": True},
        "afterTransform": {"hidden": True, "focuses": 1, "listener": False},
        "singleShot": {"hidden": True, "focuses": 3},
        "reopened": {"hidden": False, "focuses": 5, "listener": False, "timer": 0},
        "reducedMotion": {"hidden": True, "focuses": 7, "listener": False, "timer": 0},
    }


def test_release0_bottom_sheet_swipe_contract():
    """TDD: shipped JS uses terminal touch/pen coordinates and monotonic timestamps only."""
    html = authenticated_app_html()
    for expected in ('id="detail-handle"', 'aria-label="아래로 밀어 상세 닫기"', 'transform:translateY(100%)', '.dialog.is-open .dialog-sheet{transform:translateY(0)', 'touch-action:pan-x'):
        assert expected in html
    script = re.search(r"<script>(.*?)</script>", html, re.S).group(1)
    motion = re.search(r"const sheetGesture=.*?(?=const today)", script, re.S).group(0)
    node = r'''
let closeCalls=0,captures=0;const listeners={};
const cls=()=>({items:new Set(),add(x){this.items.add(x)},remove(...x){x.forEach(y=>this.items.delete(y))},contains(x){return this.items.has(x)}});
const sheet={style:{}};const dialog={hidden:false,classList:cls(),querySelector:()=>sheet};
const handle={addEventListener:(type,fn)=>listeners[type]=fn,setPointerCapture:()=>captures++,releasePointerCapture:()=>{}};
const $=s=>s==='#detail'?dialog:handle;const window={innerHeight:600};const closeDetail=()=>closeCalls++;
''' + motion + r'''
const fire=(type,e={})=>listeners[type]({pointerType:'touch',isPrimary:true,pointerId:1,clientX:0,clientY:0,timeStamp:100,preventDefault:()=>{},...e,type});
const drag=(type,up)=>{fire('pointerdown',{pointerType:type,timeStamp:100});fire('pointermove',{clientY:24,timeStamp:110});fire('pointerup',{...up,pointerType:type})};
drag('touch',{clientY:-100,timeStamp:111});const upward=closeCalls;
drag('touch',{clientX:140,clientY:20,timeStamp:120});const horizontal=closeCalls;
drag('touch',{clientY:24,timeStamp:50});drag('touch',{clientY:24,timeStamp:NaN});const badTime=closeCalls;
const capturesBeforeRejected=captures;for(const type of ['unknown','','mouse']){fire('pointerdown',{pointerType:type,pointerId:2});fire('pointermove',{pointerId:2,clientY:130,timeStamp:110});fire('pointerup',{pointerType:type,pointerId:2,clientY:130,timeStamp:120})}const rejectedTypes={closeCalls,captures:captures-capturesBeforeRejected};
drag('touch',{clientY:130,timeStamp:250});drag('pen',{clientY:130,timeStamp:250});const qualified=closeCalls;
fire('pointerdown',{timeStamp:300});fire('pointermove',{clientY:130,timeStamp:310});fire('pointercancel',{clientY:130,timeStamp:320});fire('pointerdown',{timeStamp:400});fire('pointermove',{clientY:130,timeStamp:410});fire('lostpointercapture',{clientY:130,timeStamp:420});
console.log(JSON.stringify({upward,horizontal,badTime,rejectedTypes,qualified,after:{closeCalls,transform:sheet.style.transform,drag:dialog.classList.contains('is-dragging')}}));
'''
    state = json.loads(subprocess.check_output(["node", "-e", node], text=True, env=os.environ).strip())
    assert state == {
        "upward": 0,
        "horizontal": 0,
        "badTime": 0,
        "rejectedTypes": {"closeCalls": 0, "captures": 0},
        "qualified": 2,
        "after": {"closeCalls": 2, "transform": "", "drag": False},
    }
