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


def test_release0_renderer_handles_missing_trust_and_only_explicit_fresh_active_scenario():
    html = authenticated_app_html()
    script = re.search(r"<script>(.*?)</script>", html, re.S).group(1)
    scenario = re.search(r"function trustedScenario.*?(?=function scenarioLabel)", script, re.S).group(0)
    payload = {
        "event_scenarios": [{"tracking_state": "ACTIVE", "current": {"active_scenario_label": "GOOD", "market_context_status": "VERIFIED", "freshness": "FRESH", "match": "MATCH", "known_at": "2026-09-04T09:00:00+09:00", "retrieved_at": "2026-09-04T09:01:00+09:00"}, "scenarios": [{"label": "GOOD"}, {"label": "BASE"}, {"label": "BAD"}]}]
    }
    missing = {"event_scenarios": [{"tracking_state": "ACTIVE", "current": {"active_scenario_label": "GOOD", "market_context_status": "UNAVAILABLE", "match": "MATCH"}, "scenarios": [{"label": "GOOD"}]}]}
    node = scenario + "\nconsole.log(JSON.stringify([trustedScenario(" + json.dumps(payload) + "),trustedScenario(" + json.dumps(missing) + ")]));"
    output = subprocess.check_output(["node", "-e", node], text=True, env=os.environ).strip()
    assert json.loads(output) == ["GOOD", None]
