"""Release 0 public prototype contract."""
import json
import os
import re
import subprocess

from fastapi.testclient import TestClient

from app import app


def prototype_html() -> str:
    response = TestClient(app).get("/prototype")
    assert response.status_code == 200
    return response.text


def test_public_prototype_has_three_mock_identities_and_no_order_surface():
    html = prototype_html()
    for required in (
        "Release 0 목업 · 실제 데이터가 아닙니다",
        "오늘 판단이 필요한 종목",
        "마지막 확인 이후 중요한 변화가 있는 종목만 표시합니다.",
        "한빛반도체", "모노바이오", "동해전기",
        "무엇이 달라졌나", "현재 판단", "지금 할 일과 다음 확인 항목",
        "누락·충돌", "조건별 시나리오", "출처와 변경 이력",
        "현재 단계: 기록 전용", "주문 기능 없음",
    ):
        assert required in html
    for forbidden in ("실시간 시세", "주문 수량", "매수 주문", "매도 주문", "수익률", "fetch("):
        assert forbidden not in html


def test_prototype_accessibility_and_disclosure_contracts():
    html = prototype_html()
    for required in (
        'aria-live="polite"', "min-height:44px", ":focus-visible",
        "prefers-reduced-motion", "font-size:clamp(16px", "aria-current",
        'aria-expanded="false"', 'aria-controls="scenarios-panel"',
        'aria-controls="history-panel"', "현재 확인된 조건과 가장 가까움",
    ):
        assert required in html
    assert html.count("현재 확인된 조건과 가장 가까움") == 1


def test_one_tap_detail_exposes_each_status_and_only_fresh_item_gets_glow():
    html = prototype_html()
    script = re.search(r"<script>(.*?)</script>", html, re.S).group(1)
    for required in (
        "function openItem(id)", "onclick=\"openItem('fresh')\"",
        "onclick=\"openItem('stale')\"", "onclick=\"openItem('conflict')\"",
        "glow:true", "glow:false", "item.glow", "aria-current=\"true\"",
        "function closeDetail()", "event.key==='Escape'", "openedBy.focus()",
    ):
        assert required in html
    harness = "global.document={addEventListener(){}};" + script + "console.log(JSON.stringify(items))"
    data = json.loads(subprocess.check_output(["node", "-e", harness], text=True, env=os.environ))
    assert [(data[key]["status"], data[key]["glow"]) for key in ("fresh", "stale", "conflict")] == [
        ("정상", True), ("정보가 오래됨", False), ("출처 충돌", False)
    ]
