"""DOM and accessibility regressions for the Quiet Authority presentation layer."""

import json
import re
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
APP_HTML = (ROOT / "kr_stock_autotrader" / "decision_card_app.html").read_text(encoding="utf-8")
AUTH_HTML = (ROOT / "kr_stock_autotrader" / "ui.py").read_text(encoding="utf-8")


def _run_load_for_operation_date(summary: dict, cards: list[dict], missing: list[dict]) -> dict[str, object]:
    script = re.search(r"<script>(.*?)</script>", APP_HTML, re.S).group(1)
    draw = re.search(r"function draw\(\)\{.*?\}(?=\s*function missingDetail)", script, re.S).group(0)
    load = re.search(r"async function load\(\)\{.*?\}(?=\s*const sheetGesture)", script, re.S).group(0)
    node = f"""
const values = {{'date': {{value: '2026-08-30'}}, 'reviewable-count': {{textContent: ''}}, 'changed-count': {{textContent: ''}}, 'unverified-count': {{textContent: ''}}, 'cards': {{innerHTML: '', querySelectorAll: () => []}}, 'error': {{hidden: false, textContent: ''}}}};
const $ = selector => values[selector.slice(1)];
let summary = null, cards = [], missing = [], lastGood = [], loadGeneration = 0;
const calls = [];
const api = async path => {{ calls.push(path); return {{'cards/summary?operation_date=2026-08-30&current_only=true': {json.dumps(summary, ensure_ascii=False)}, 'cards?operation_date=2026-08-30&current_only=true': {json.dumps(cards, ensure_ascii=False)}, 'cards/missing?operation_date=2026-08-30': {json.dumps(missing, ensure_ascii=False)}}}[path]; }};
const invalidateDetail = () => {{}};
const resetSheetMotion = () => {{}};
const row = item => `<article>${{item.id}}</article>`;
const emptyState = () => '<article>empty</article>';
{draw}
{load}
(async () => {{ await load(); console.log(JSON.stringify({{calls, reviewable: values['reviewable-count'].textContent, changed: values['changed-count'].textContent, unverified: values['unverified-count'].textContent, rendered: values.cards.innerHTML}})); }})();
"""
    return json.loads(subprocess.check_output(["node", "-e", node], text=True))


def test_summary_strip_assigns_explicit_api_semantics_to_rendered_dom_for_operation_date():
    """Invalidation, holds, and missing evidence cannot redefine summary meanings client-side."""
    rendered = _run_load_for_operation_date(
        {"카드 생성": 9, "판단 보류": 4, "카드 미생성": 2},
        [
            {"id": "active", "invalidated_at": None, "card": {"verdict": "매수 검토 가능"}},
            {"id": "invalidated", "invalidated_at": "2026-08-30T11:00:00+09:00", "card": {"verdict": "판단 보류"}},
            {"id": "held", "invalidated_at": None, "card": {"verdict": "판단 보류"}},
        ],
        [{"id": "missing"}],
    )
    assert rendered == {
        "calls": [
            "cards/summary?operation_date=2026-08-30&current_only=true",
            "cards?operation_date=2026-08-30&current_only=true",
            "cards/missing?operation_date=2026-08-30",
        ],
        "reviewable": "9",
        "changed": "4",
        "unverified": "2",
        "rendered": "<article>active</article><article>invalidated</article><article>held</article><article>missing</article>",
    }


def test_failed_operation_date_refresh_never_reuses_another_dates_summary():
    """Fallback rows remain useful, but counts must not be attributed to a failed date."""
    script = re.search(r"<script>(.*?)</script>", APP_HTML, re.S).group(1)
    draw = re.search(r"function draw\(\)\{.*?\}(?=\s*function missingDetail)", script, re.S).group(0)
    load = re.search(r"async function load\(\)\{.*?\}(?=\s*const sheetGesture)", script, re.S).group(0)
    node = f"""
const values = {{'date': {{value: '2026-08-30'}}, 'reviewable-count': {{textContent: ''}}, 'changed-count': {{textContent: ''}}, 'unverified-count': {{textContent: ''}}, 'cards': {{innerHTML: '', querySelectorAll: () => []}}, 'error': {{hidden: true, textContent: ''}}}};
const $ = selector => values[selector.slice(1)];
let summary = null, cards = [], missing = [], lastGood = [], loadGeneration = 0;
const api = async path => {{
  if (path.includes('2026-08-31')) throw new Error('refresh failed');
  return {{
    'cards/summary?operation_date=2026-08-30&current_only=true': {{'카드 생성': 9, '판단 보류': 4, '카드 미생성': 2}},
    'cards?operation_date=2026-08-30&current_only=true': [{{id: 'prior-row'}}],
    'cards/missing?operation_date=2026-08-30': []
  }}[path];
}};
const invalidateDetail = () => {{}};
const resetSheetMotion = () => {{}};
const row = item => `<article>${{item.id}}</article>`;
const emptyState = () => '<article>empty</article>';
{draw}
{load}
(async () => {{
  await load();
  values.date.value = '2026-08-31';
  await load();
  console.log(JSON.stringify({{
    date: values.date.value,
    summary: [values['reviewable-count'].textContent, values['changed-count'].textContent, values['unverified-count'].textContent],
    rendered: values.cards.innerHTML,
    errorHidden: values.error.hidden,
    error: values.error.textContent
  }}));
}})();
"""
    rendered = json.loads(subprocess.check_output(["node", "-e", node], text=True))
    assert rendered == {
        "date": "2026-08-31",
        "summary": ["—", "—", "—"],
        "rendered": "<article>prior-row</article>",
        "errorHidden": False,
        "error": "현재 정보를 갱신하지 못했습니다. 마지막으로 확인한 목록을 유지합니다.",
    }


def test_session_expired_operation_date_refresh_fails_closed_without_overwriting_notice():
    """A selected date must never display counts retrieved for a prior date after expiry."""
    script = re.search(r"<script>(.*?)</script>", APP_HTML, re.S).group(1)
    api = re.search(r"async function api\(path,options=\{\}\)\{.*?\}(?=\s*function statusFor)", script, re.S).group(0)
    draw = re.search(r"function draw\(\)\{.*?\}(?=\s*function missingDetail)", script, re.S).group(0)
    load = re.search(r"async function load\(\)\{.*?\}(?=\s*const sheetGesture)", script, re.S).group(0)
    node = f"""
const values = {{'date': {{value: '2026-08-30'}}, 'reviewable-count': {{textContent: ''}}, 'changed-count': {{textContent: ''}}, 'unverified-count': {{textContent: ''}}, 'cards': {{innerHTML: '', querySelectorAll: () => []}}, 'error': {{hidden: true, textContent: '', innerHTML: ''}}}};
const $ = selector => values[selector.slice(1)];
let summary = null, cards = [], missing = [], lastGood = [], loadGeneration = 0;
const fetch = async url => {{
  if (url.includes('2026-08-31')) return {{status: 401, ok: false, json: async () => ({{}})}};
  const path = url.slice('/api/'.length);
  return {{status: 200, ok: true, json: async () => ({{
    'cards/summary?operation_date=2026-08-30&current_only=true': {{'카드 생성': 9, '판단 보류': 4, '카드 미생성': 2}},
    'cards?operation_date=2026-08-30&current_only=true': [{{id: 'prior-row'}}],
    'cards/missing?operation_date=2026-08-30': []
  }}[path])}};
}};
const invalidateDetail = () => {{}};
const resetSheetMotion = () => {{}};
const row = item => `<article>${{item.id}}</article>`;
const emptyState = () => '<article>empty</article>';
{api}
{draw}
{load}
(async () => {{
  await load();
  values.date.value = '2026-08-31';
  await load();
  console.log(JSON.stringify({{
    date: values.date.value,
    summary: [values['reviewable-count'].textContent, values['changed-count'].textContent, values['unverified-count'].textContent],
    rendered: values.cards.innerHTML,
    errorHidden: values.error.hidden,
    notice: values.error.innerHTML
  }}));
}})();
"""
    rendered = json.loads(subprocess.check_output(["node", "-e", node], text=True))
    assert rendered == {
        "date": "2026-08-31",
        "summary": ["—", "—", "—"],
        "rendered": "<article>prior-row</article>",
        "errorHidden": False,
        "notice": '세션이 만료되었습니다. <a class="session-link" href="/?expired=1">로그인 화면으로 이동</a>',
    }


def test_late_success_for_stale_operation_date_cannot_replace_current_expiry_state():
    """The selected B expiry state owns the UI after delayed A succeeds."""
    script = re.search(r"<script>(.*?)</script>", APP_HTML, re.S).group(1)
    api = re.search(r"async function api\(path,options=\{\}\)\{.*?\}(?=\s*function statusFor)", script, re.S).group(0)
    draw = re.search(r"function draw\(\)\{.*?\}(?=\s*function missingDetail)", script, re.S).group(0)
    load = re.search(r"async function load\(\)\{.*?\}(?=\s*const sheetGesture)", script, re.S).group(0)
    node = f"""
const values = {{'date': {{value: '2026-08-30'}}, 'reviewable-count': {{textContent: ''}}, 'changed-count': {{textContent: ''}}, 'unverified-count': {{textContent: ''}}, 'cards': {{innerHTML: '', querySelectorAll: () => []}}, 'error': {{hidden: true, textContent: '', innerHTML: ''}}}};
const $ = selector => values[selector.slice(1)];
let summary = null, cards = [], missing = [], lastGood = [], loadGeneration = 0;
const pendingA = [];
const fetch = url => {{
  if (url.includes('2026-08-31')) return Promise.resolve({{status: 401, ok: false, json: async () => ({{}})}});
  return new Promise(resolve => pendingA.push(() => resolve({{status: 200, ok: true, json: async () => ({{
    'cards/summary?operation_date=2026-08-30&current_only=true': {{'카드 생성': 9, '판단 보류': 4, '카드 미생성': 2}},
    'cards?operation_date=2026-08-30&current_only=true': [{{id: 'stale-A'}}],
    'cards/missing?operation_date=2026-08-30': []
  }}[url.slice('/api/'.length)])}})));
}};
const invalidateDetail = () => {{}};
const resetSheetMotion = () => {{}};
const row = item => `<article>${{item.id}}</article>`;
const emptyState = () => '<article>empty</article>';
{api}
{draw}
{load}
(async () => {{
  const loadA = load();
  values.date.value = '2026-08-31';
  await load();
  pendingA.forEach(resolve => resolve());
  await loadA;
  console.log(JSON.stringify({{date: values.date.value, summary: [values['reviewable-count'].textContent, values['changed-count'].textContent, values['unverified-count'].textContent], rendered: values.cards.innerHTML, errorHidden: values.error.hidden, notice: values.error.innerHTML}}));
}})();
"""
    rendered = json.loads(subprocess.check_output(["node", "-e", node], text=True))
    assert rendered == {
        "date": "2026-08-31",
        "summary": ["—", "—", "—"],
        "rendered": "<article>empty</article>",
        "errorHidden": False,
        "notice": '세션이 만료되었습니다. <a class="session-link" href="/?expired=1">로그인 화면으로 이동</a>',
    }


def test_late_expiry_for_stale_operation_date_cannot_replace_current_success_state():
    """A delayed A 401 must not write a notice after B has loaded successfully."""
    script = re.search(r"<script>(.*?)</script>", APP_HTML, re.S).group(1)
    api = re.search(r"async function api\(path,options=\{\}\)\{.*?\}(?=\s*function statusFor)", script, re.S).group(0)
    draw = re.search(r"function draw\(\)\{.*?\}(?=\s*function missingDetail)", script, re.S).group(0)
    load = re.search(r"async function load\(\)\{.*?\}(?=\s*const sheetGesture)", script, re.S).group(0)
    node = f"""
const values = {{'date': {{value: '2026-08-30'}}, 'reviewable-count': {{textContent: ''}}, 'changed-count': {{textContent: ''}}, 'unverified-count': {{textContent: ''}}, 'cards': {{innerHTML: '', querySelectorAll: () => []}}, 'error': {{hidden: true, textContent: '', innerHTML: ''}}}};
const $ = selector => values[selector.slice(1)];
let summary = null, cards = [], missing = [], lastGood = [], loadGeneration = 0;
const pendingA = [];
const fetch = url => {{
  if (url.includes('2026-08-31')) return Promise.resolve({{status: 200, ok: true, json: async () => ({{
    'cards/summary?operation_date=2026-08-31&current_only=true': {{'카드 생성': 7, '판단 보류': 3, '카드 미생성': 1}},
    'cards?operation_date=2026-08-31&current_only=true': [{{id: 'current-B'}}],
    'cards/missing?operation_date=2026-08-31': []
  }}[url.slice('/api/'.length)])}});
  return new Promise(resolve => pendingA.push(() => resolve({{status: 401, ok: false, json: async () => ({{}})}})));
}};
const invalidateDetail = () => {{}};
const resetSheetMotion = () => {{}};
const row = item => `<article>${{item.id}}</article>`;
const emptyState = () => '<article>empty</article>';
{api}
{draw}
{load}
(async () => {{
  const loadA = load();
  values.date.value = '2026-08-31';
  await load();
  pendingA.forEach(resolve => resolve());
  await loadA;
  console.log(JSON.stringify({{date: values.date.value, summary: [values['reviewable-count'].textContent, values['changed-count'].textContent, values['unverified-count'].textContent], rendered: values.cards.innerHTML, errorHidden: values.error.hidden, notice: values.error.innerHTML}}));
}})();
"""
    rendered = json.loads(subprocess.check_output(["node", "-e", node], text=True))
    assert rendered == {
        "date": "2026-08-31",
        "summary": ["7", "3", "1"],
        "rendered": "<article>current-B</article>",
        "errorHidden": True,
        "notice": "",
    }


def test_invalidation_renderer_distinguishes_replacement_from_hypothesis_discard_and_escapes_reason():
    """A regenerated card is history; genuine and legacy invalidations retain an explicit reason."""
    script = re.search(r"<script>(.*?)</script>", APP_HTML, re.S).group(1)
    invalidation = re.search(r"function invalidationState\(c\)\{.*?\}(?=\s*function statusFor)", script, re.S).group(0)
    status = re.search(r"function statusFor\(c\)\{.*?\}(?=\s*function deadline)", script, re.S).group(0)
    node = (
        invalidation
        + status
        + "\nconsole.log(JSON.stringify(["
        + "invalidationState({invalidated_at:'2026-09-08',invalidation_reason:'new_card'}),"
        + "invalidationState({invalidated_at:'2026-09-08',invalidation_reason:'evidence_invalidated'}),"
        + "invalidationState({invalidated_at:'2026-09-08',invalidation_reason:'<legacy>'}),"
        + "invalidationState({invalidated_at:'2026-09-08'}),"
        + "statusFor({card:{verdict:'매수 검토 가능'}})"
        + "]));"
    )
    states = json.loads(subprocess.check_output(["node", "-e", node], text=True))
    assert states == [
        {"label": "새 카드로 대체됨 · 이전 판단 기록", "kind": "history", "detail": "새 카드가 생성되어 이 카드는 이전 판단 기록으로 보관됩니다."},
        {"label": "가설 폐기 조건 충족", "kind": "danger", "detail": "가설 폐기 사유: 근거 자료가 무효화되었습니다."},
        {"label": "가설 폐기 조건 충족", "kind": "danger", "detail": "가설 폐기 사유: <legacy>"},
        {"label": "판단 종료 · 사유 확인 필요", "kind": "history", "detail": "이 판단은 종료되었지만 종료 사유가 기록되지 않았습니다."},
        ["매수 검토 가능", ""],
    ]
    korean = re.search(r"function korean.*?(?=function blockerItems)", script, re.S).group(0)
    blockers = re.search(r"function blockerItems.*?(?=function kst)", script, re.S).group(0)
    detail = re.search(r"function renderDetail.*?(?=function visibleFocusable)", script, re.S).group(0)
    rendered = subprocess.check_output(
        [
            "node",
            "-e",
            "const esc=v=>String(v??'').replace(/[&<>\\\"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','\\\"':'&quot;',\"'\":'&#39;'}[c]));const compactSections=()=>'';const marketContext=()=>'';const scenarios=()=>'';const sourceFacts=()=>'';"
            + invalidation
            + korean
            + blockers
            + detail
            + "console.log(renderDetail({invalidated_at:'2026-09-08',invalidation_reason:'<legacy>',card:{}}));",
        ],
        text=True,
    )
    assert "<h3>판단 종료 사유</h3><p>가설 폐기 사유: &lt;legacy&gt;</p>" in rendered
    assert "가설 폐기 사유: <legacy>" not in rendered


def test_legacy_scenario_renderer_adapts_provenance_fail_closed_with_escaping():
    """Historical v1 rows use the actionable adapter without inventing GOOD facts."""
    script = re.search(r"<script>(.*?)</script>", APP_HTML, re.S).group(1)
    renderer = re.search(r"function trustedScenario.*?(?=function renderDetail)", script, re.S).group(0)
    payload = {
        "event_scenarios": [{
            "scenarios": [{
                "label": "GOOD",
                "conditions": [
                    {"text": "계약 <확정>", "provenance": "evidence.summary"},
                    {"text": "공시 제목", "provenance": "evidence.title"},
                    {"text": "카드의 판단", "provenance": "card.headline"},
                    {"text": "성립할 신호", "provenance": "card.proof_point"},
                    {"text": "다음 공시", "provenance": "card.next_check"},
                    {"text": "원가 확인 전", "provenance": "card.unknowns"},
                    {"text": "납기 지연", "provenance": "card.false_positive"},
                    {"text": "계약 취소", "provenance": "card.evidence_invalidation"},
                    {"text": "기존 수치 조건", "provenance": "legacy.price_krw"},
                    {"text": "근거 없는 조건", "provenance": None},
                ],
            }],
        }],
    }
    node = "const korean=v=>String(v??'');const esc=v=>String(v??'').replace(/[&<>\\\"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','\\\"':'&quot;',\"'\":'&#39;'}[c]));" + renderer + "\nconsole.log(scenarios(" + json.dumps(payload, ensure_ascii=False) + "));"
    rendered = subprocess.check_output(["node", "-e", node], text=True).strip()
    assert "구형 시나리오" in rendered and "상방 시나리오 미생성" in rendered and "가격 미설정" in rendered
    assert "원가 확인 전" in rendered and "계약 &lt;확정&gt;" not in rendered
    for literal in ("공시 제목", "카드의 판단", "성립할 신호", "다음 공시", "납기 지연", "계약 취소", "기존 수치 조건", "근거 없는 조건"):
        assert literal not in rendered
    assert "evidence.summary" not in rendered and "card.headline" not in rendered and "legacy.price_krw" not in rendered
    assert "<dl" in rendered and "<dt" in rendered and "<dd" in rendered


def test_scenario_renderer_classifies_nested_provenance_by_boundary_safe_root():
    """Nested source leaves retain their parent category without accepting lookalikes."""
    script = re.search(r"<script>(.*?)</script>", APP_HTML, re.S).group(1)
    category = re.search(r"function scenarioCategory.*?(?=function scenarioConditions)", script, re.S).group(0)
    cases = {
        "evidence.summary.detail": "확인된 사실",
        "evidence.title[0]": "확인된 사실",
        "card.headline.reason": "카드 판단",
        "card.proof_point[0]": "성립 신호",
        "card.next_check.items[0]": "다음 확인",
        "card.unknowns": "미확인",
        "card.unknowns.detail": "미확인",
        "card.unknowns[0]": "미확인",
        "card.false_positive.risk": "오탐 위험",
        "card.evidence_invalidation.reason": "무효화 조건",
        "card.unknownship": "조건",
        "card.headline_extra": "조건",
        "evidence.summaryFake": "조건",
    }
    node = category + "\nconsole.log(JSON.stringify(Object.fromEntries(Object.keys(" + json.dumps(cases, ensure_ascii=False) + ").map(key => [key, scenarioCategory(key)]))));"
    assert json.loads(subprocess.check_output(["node", "-e", node], text=True)) == cases


def _relative_luminance(hex_color: str) -> float:
    channels = [int(hex_color[index:index + 2], 16) / 255 for index in (1, 3, 5)]
    linear = [value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4 for value in channels]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def _contrast(first: str, second: str) -> float:
    high, low = sorted((_relative_luminance(first), _relative_luminance(second)), reverse=True)
    return (high + 0.05) / (low + 0.05)


def test_quiet_authority_buttons_are_44px_and_focus_token_meets_nontext_contrast():
    app_handle = re.search(r"\.dialog-handle\{([^}]*)\}", APP_HTML).group(1)
    auth_toggle = re.search(r"\.show-password\{([^}]*)\}", AUTH_HTML).group(1)
    for rule in (app_handle, auth_toggle):
        width = re.search(r"(?:min-)?width:(\d+)px", rule)
        height = re.search(r"(?:min-)?height:(\d+)px", rule)
        assert width is not None and int(width.group(1)) >= 44
        assert height is not None and int(height.group(1)) >= 44

    for source in (APP_HTML, AUTH_HTML):
        focus = re.search(r"focus-visible[^}]*outline:3px solid (var\(--focus\)|#[0-9a-fA-F]{6})", source).group(1)
        token = re.search(r"--focus:(#[0-9a-fA-F]{6})", source).group(1) if focus == "var(--focus)" else focus
        assert _contrast(token, "#ffffff") >= 3
        assert _contrast(token, "#f4f6f8") >= 3


def test_mobile_sheet_is_viewport_anchored_and_touch_content_drag_closes_once():
    """Content-origin iOS touch drags close only when they genuinely claim the sheet."""
    sheet_rule = re.search(r"\.dialog-sheet\{([^}]*)\}", APP_HTML).group(1)
    assert "margin:0 auto" in sheet_rule

    script = re.search(r"<script>(.*?)</script>", APP_HTML, re.S).group(1)
    motion = re.search(r"let sheetCloseGeneration=0;.*?(?=function disclosure)", script, re.S).group(0)
    gesture = re.search(r"const sheetGesture=.*?(?=const today)", script, re.S).group(0)
    node = """
const listeners = {{}};
const classes = () => {{ const values = new Set(); return {{add: (...names) => names.forEach(name => values.add(name)), remove: (...names) => names.forEach(name => values.delete(name)), contains: name => values.has(name)}}; }};
const sheet = {{scrollTop: 0, style: {{}}, classList: classes(), addEventListener: (name, handler) => (listeners[name] ||= []).push(handler), removeEventListener: () => {{}}, setPointerCapture: () => {{}}, releasePointerCapture: () => {{}}, offsetHeight: 1}};
const openerButton = {{focusCount: 0, focus() {{ this.focusCount++; }}}};
const detail = {{hidden: false, classList: classes(), querySelector: selector => selector === '.dialog-sheet' ? sheet : null}};
const main = {{inert: true, attrs: {{'aria-hidden': 'true'}}, setAttribute(name, value) {{ this.attrs[name] = value; }}, removeAttribute(name) {{ delete this.attrs[name]; }}};
const values = {{detail, 'app-main': main}};
const $ = selector => values[selector.slice(1)];
let opener = openerButton;
const matchMedia = () => ({{matches: false}});
const window = {{innerHeight: 600}};
const setTimeout = callback => {{ callback(); return 1; }};
const clearTimeout = () => {{}};
const invalidateDetail = () => {{}};
const animatedSheet = () => true;
let sheetCloseTimer = 0, sheetCloseFinish = null, sheetCloseNode = null;
{motion}
{gesture}
const target = {{closest: () => null}};
const button = {{closest: selector => selector.includes('button') ? {{}} : null}};
const touch = (type, y, eventTarget = target) => {{
  const point = {{identifier: 7, clientX: 20, clientY: y}};
  for (const handler of listeners[type] || []) handler({{type, target: eventTarget, touches: type === 'touchend' || type === 'touchcancel' ? [] : [point], changedTouches: [point], cancelable: true, preventDefault() {{ this.prevented = true; }}});
}};
const resetDialog = () => {{ detail.hidden = false; main.inert = true; main.attrs['aria-hidden'] = 'true'; sheet.scrollTop = 0; sheet.style = {{}}; sheetGesture.reset(); }};
const state = () => ({{hidden: detail.hidden, restored: !main.inert && !('aria-hidden' in main.attrs), focus: openerButton.focusCount}});
resetDialog(); touch('touchstart', 0); touch('touchmove', 140); touch('touchend', 140); const closes = state();
touch('pointerup', 140); const compatibilityPointerDidNotDoubleClose = openerButton.focusCount === 1;
resetDialog(); touch('touchstart', 0); touch('touchmove', 40); touch('touchcancel', 40); const cancel = !detail.hidden;
resetDialog(); touch('touchstart', 100); touch('touchmove', 20); touch('touchend', 20); const upward = !detail.hidden;
resetDialog(); touch('touchstart', 0); const sideways = {{identifier: 7, clientX: 180, clientY: 20}}; for (const handler of listeners.touchmove || []) handler({{type: 'touchmove', target, touches: [sideways], changedTouches: [sideways], cancelable: true, preventDefault() {{}}}}); touch('touchend', 20); const horizontal = !detail.hidden;
resetDialog(); touch('touchstart', 0, button); touch('touchmove', 140, button); touch('touchend', 140, button); const control = !detail.hidden;
resetDialog(); sheet.scrollTop = 12; touch('touchstart', 0); touch('touchmove', 140); touch('touchend', 140); const scrolled = !detail.hidden;
console.log(JSON.stringify({{closes, compatibilityPointerDidNotDoubleClose, cancel, upward, horizontal, control, scrolled}}));
""".replace("{{", "{").replace("}}", "}").replace("{motion}", motion).replace("{gesture}", gesture)
    result = json.loads(subprocess.check_output(["node", "-e", node], text=True))
    assert result == {
        "closes": {"hidden": True, "restored": True, "focus": 1},
        "compatibilityPointerDidNotDoubleClose": True,
        "cancel": True,
        "upward": True,
        "horizontal": True,
        "control": True,
        "scrolled": True,
    }


def test_compact_decision_sections_render_grounded_actions_prices_and_legacy_gaps():
    """The default card/modal summary exposes price, meaning, and review action."""
    script = re.search(r"<script>(.*?)</script>", APP_HTML, re.S).group(1)
    invalidation = re.search(r"function invalidationState\(c\)\{.*?\}(?=\s*function statusFor)", script, re.S).group(0)
    compact = re.search(r"function compactAction\(c\)\{.*?\}(?=\s*function row)", script, re.S).group(0)
    payload = {
        "card": {
            "verdict": "매수 검토 가능", "proof_point": "계약 이행 확인", "business_value": "반복 매출 확대",
            "stop_loss": 80, "price_cap": 100, "take_profit": [{"price": 110}],
            "observation_scenarios": [
                {"label": "BAD", "level_krw": 95, "meaning": "이벤트 전 저점", "action": "95원 이하이면 보유 축소 여부를 검토", "checks": "저점 이탈 후 공시 무효화 여부 확인", "source_field": "market.pre_event_low"},
                {"label": "BASE", "level_krw": 100, "meaning": "이벤트 전 종가", "action": "100원 부근에서는 신규 판단을 보류하고 근거를 검토", "checks": "종가와 거래량의 지속 여부 확인", "source_field": "market.pre_event_close"},
                {"label": "GOOD", "level_krw": 110, "meaning": "이벤트 구간 고점", "action": "110원 이상이면 보유 근거와 분할 대응 필요성을 검토", "checks": "고점 갱신과 후속 공시 확인", "source_field": "market.event_window_high"},
            ],
            "evidence_invalidation": {"condition": "계약 취소", "risk_policy": "종목 손실 한도 0.15% · 명목 한도 3%", "policy_version": "v1"},
        },
    }
    legacy = {"card": {"verdict": "관찰", "business_value": "확인된 사업 가치"}}
    node = (
        "const esc=v=>String(v??'').replace(/[&<>\\\"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','\\\"':'&quot;',\"'\":'&#39;'}[c]));"
        + invalidation + compact
        + "const scenarioLabel=label=>({GOOD:'상방 조건',BASE:'기준 조건',BAD:'하방 조건'})[label]||'확인 필요';"
        + "console.log(JSON.stringify([compactSections(" + json.dumps(payload, ensure_ascii=False) + "),compactSections(" + json.dumps(legacy, ensure_ascii=False) + "),observationDetails(" + json.dumps(payload, ensure_ascii=False) + ")]));"
    )
    populated, no_price, detail = json.loads(subprocess.check_output(["node", "-e", node], text=True))
    assert "조건부 매수 검토" in populated
    assert "계약 이행 확인" in populated and "반복 매출 확대" not in populated
    assert all(text in populated for text in ("재료 전 저점 · 95원", "재료 전 종가 · 100원", "이벤트 구간 고점 · 110원", "이벤트 전 저점", "이벤트 전 종가", "이벤트 구간 고점", "95원 이하이면 보유 축소 여부를 검토"))
    assert 'scenario-level-bad' in populated and 'scenario-level-base' in populated and 'scenario-level-good' in populated
    assert "저점 이탈 후 공시 무효화 여부 확인" in detail
    assert "종가와 거래량의 지속 여부 확인" in detail
    assert "고점 갱신과 후속 공시 확인" in detail
    assert "80원" not in populated
    assert "위험 한도 종목 손실 한도 0.15% · 명목 한도 3%" in populated
    assert "지금 매수하지 않음 · 관찰" in no_price
    assert "확인된 사업 가치" in no_price
    assert "관찰 가격 기준이 아직 설정되지 않았습니다." in no_price
