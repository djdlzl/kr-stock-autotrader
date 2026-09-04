"""Executable Release 0 prototype UI contracts."""
import os
import re
import subprocess

from fastapi.testclient import TestClient

from app import app


def prototype_html() -> str:
    response = TestClient(app).get("/prototype")
    assert response.status_code == 200
    return response.text


def run_ui_behavior(html: str) -> None:
    script = re.search(r"<script>(.*?)</script>", html, re.S).group(1)
    harness = r'''
const assert = require('assert');
const timers = [];
global.setTimeout = callback => timers.push(callback);
class Element {
  constructor(tag) { this.tagName=tag; this.children=[]; this.attrs={}; this.listeners={}; this.hidden=false; this.disabled=false; this.inert=false; this.textContent=''; this.className=''; this.parentNode=null; this.computedStyle={display:'block',visibility:'visible'}; }
  set id(value) { this._id=value; document.nodes[value]=this; } get id() { return this._id; }
  get parentElement() { return this.parentNode; }
  set href(value) { this.attrs.href=value; } get href() { return this.attrs.href; }
  append(...nodes) { for (const node of nodes) { if (node && typeof node !== 'string') { node.parentNode=this; this.children.push(node); } } }
  prepend(...nodes) { for (const node of nodes.reverse()) { if (node && typeof node !== 'string') { node.parentNode=this; this.children.unshift(node); } } }
  replaceChildren(...nodes) { this.children=[]; this.append(...nodes); }
  removeChild(node) { this.children.splice(this.children.indexOf(node),1); }
  setAttribute(key,value) { this.attrs[key]=String(value); }
  getAttribute(key) { return this.attrs[key] === undefined ? null : this.attrs[key]; }
  removeAttribute(key) { delete this.attrs[key]; }
  addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); }
  click() { for (const fn of this.listeners.click || []) fn({}); }
  focus() { document.activeElement=this; }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  querySelectorAll(selector) {
    const result=[]; const matches=(child) => {
      if (selector==='.panel') return child.className.split(' ').includes('panel');
      if (selector==='.disclosure') return child.className.split(' ').includes('disclosure');
      if (selector==='[aria-current="true"]') return child.getAttribute('aria-current')==='true';
      const focusSelector=selector.includes('button:not') || selector.includes('[href]');
      if (!focusSelector) return false;
      return (child.tagName==='button' && !child.disabled) ||
        (selector.includes('[href]') && child.getAttribute('href')!==null) ||
        (['input','select','textarea'].includes(child.tagName) && !child.disabled) ||
        (child.getAttribute('tabindex')!==null && child.getAttribute('tabindex')!=='-1');
    }; const walk=node => { for (const child of node.children) { if (matches(child)) result.push(child); walk(child); } }; walk(this); return result;
  }
}
const document = global.document = {
  nodes:{}, activeElement:null, listeners:{},
  createElement: tag => new Element(tag),
  getElementById: id => document.nodes[id],
  addEventListener: (type,fn) => { document.listeners[type]=fn; },
  dispatch(key,shiftKey=false) { let prevented=false; document.listeners.keydown({key,shiftKey,preventDefault(){prevented=true}}); return prevented; }
};
global.getComputedStyle = node => node.computedStyle;
function fixed(id, tag='div') { const node=new Element(tag); node.id=id; return node; }
const opener=fixed('fresh-opener','button'); opener.focus();
fixed('prototype-list'); const detailRoot=fixed('detail'); fixed('detail-title','h1'); const detailContent=fixed('detail-content'); const back=fixed('back','button'); back.textContent='← 뒤로'; detailRoot.append(back, detailContent); fixed('status-live');
''' + script + r'''
function text(node) { return node.textContent + node.children.map(text).join(''); }
function all(node) { return [node, ...node.children.flatMap(all)]; }
function byText(root, expected) { return all(root).find(node => node.textContent === expected); }
function countText(root, expected) { return text(root).split(expected).length - 1; }
function assertFocusWrap(expectedTexts) {
  const actual=visibleFocusable(document.getElementById('detail'));
  assert.deepEqual(actual.map(node => node.textContent), expectedTexts);
  actual[actual.length-1].focus(); assert.equal(document.dispatch('Tab'),true); assert.equal(document.activeElement,actual[0]);
  actual[0].focus(); assert.equal(document.dispatch('Tab',true),true); assert.equal(document.activeElement,actual[actual.length-1]);
}
function openAndAssert(id, expectedStatus, expectedCurrent) {
  opener.focus(); openItem(id);
  const detail=document.getElementById('detail'), list=document.getElementById('prototype-list'), content=document.getElementById('detail-content');
  assert.equal(detail.hidden,false); assert.equal(list.inert,true); assert.equal(list.getAttribute('aria-hidden'),'true');
  assert.ok(text(content).includes(expectedStatus));
  assert.deepEqual(all(content).filter(node => node.tagName==='h2').map(node => node.textContent), ['무엇이 달라졌나','현재 판단','지금 할 일과 다음 확인 항목','누락·충돌','자동화 단계']);
  const scenarios=document.getElementById('scenarios-panel');
  assert.equal(scenarios.hidden,true); assert.equal(byText(content,'조건별 시나리오 ⌄').getAttribute('aria-expanded'),'false');
  const current=content.querySelectorAll('[aria-current="true"]'); assert.equal(current.length, expectedCurrent ? 1 : 0); if(expectedCurrent) assert.equal(current[0].id,expectedCurrent);
  assertFocusWrap(['← 뒤로','조건별 시나리오 ⌄','출처와 변경 이력 ⌄']);
  return content;
}
let content=openAndAssert('fresh','가설 유지 · 기준 조건을 검토합니다.','scenario-base');
let scenariosButton=byText(content,'조건별 시나리오 ⌄'); scenariosButton.click(); assert.equal(document.getElementById('scenarios-panel').hidden,false); assert.equal(scenariosButton.getAttribute('aria-expanded'),'true'); assertFocusWrap(['← 뒤로','조건별 시나리오 ⌄','출처와 변경 이력 ⌄']); scenariosButton.click(); assert.equal(document.getElementById('scenarios-panel').hidden,true);
let freshHistory=byText(content,'출처와 변경 이력 ⌄'); freshHistory.click();
for (const expected of ['한빛반도체 공시 목업','목업 원문 · 실제 자료 아님','공개 확인 시각: 2026-09-04 09:00 KST','마지막 확인 성공시각: 2026-09-04 10:00 KST','신뢰 상태: 확인됨']) assert.ok(text(content).includes(expected));
const freshLink=byText(content,'목업 원문 · 실제 자료 아님'); assert.equal(freshLink.href,'/prototype/mock-source/hanbit-disclosure'); assert.equal(freshLink.getAttribute('target'),'_blank'); assert.equal(freshLink.getAttribute('rel'),'noopener noreferrer'); assertFocusWrap(['← 뒤로','조건별 시나리오 ⌄','출처와 변경 이력 ⌄','목업 원문 · 실제 자료 아님']); freshHistory.click(); assert.equal(document.getElementById('history-panel').hidden,true);
assert.equal(document.dispatch('Escape'),true); assert.equal(document.getElementById('detail').hidden,true); assert.equal(document.activeElement,opener); assert.equal(document.getElementById('prototype-list').inert,false);
content=openAndAssert('stale','판단 보류 · 정보가 오래됨.',null);
let historyButton=byText(content,'출처와 변경 이력 ⌄'); historyButton.click(); for (const expected of ['모노바이오 임상 발표 목업','공개 확인 시각: 2026-08-28 09:00 KST','마지막 확인 성공시각: 2026-08-28 10:00 KST','신뢰 상태: 오래됨']) assert.ok(text(content).includes(expected)); const retry=byText(content,'다시 확인'); retry.click(); assert.equal(retry.textContent,'재확인 중'); assert.equal(document.getElementById('status-live').textContent,'재확인 중'); timers.shift()(); assert.equal(retry.textContent,'여전히 정보가 오래됨'); assert.equal(document.getElementById('status-live').textContent,'재확인 완료: 여전히 정보가 오래됨'); assertFocusWrap(['← 뒤로','조건별 시나리오 ⌄','출처와 변경 이력 ⌄','목업 원문 · 실제 자료 아님']); historyButton.click(); assert.equal(document.getElementById('history-panel').hidden,true);
closeDetail(); content=openAndAssert('conflict','판단 보류 · 출처 충돌.',null);
historyButton=byText(content,'출처와 변경 이력 ⌄'); historyButton.click(); for (const expected of ['동해전기 공식 자료 목업','동해전기 보도자료 목업','공개 확인 시각: 2026-09-03 08:30 KST','마지막 확인 성공시각: 2026-09-04 09:30 KST','신뢰 상태: 충돌 확인','“양산은 10월에 시작합니다.”','“양산은 11월에 시작합니다.”']) assert.ok(text(content).includes(expected)); assert.equal(document.getElementById('history-panel').hidden,false);
for (const expected of ['동해전기 공식 자료 목업','동해전기 보도자료 목업','“양산은 10월에 시작합니다.”','“양산은 11월에 시작합니다.”','공개 확인 시각: 2026-09-03 08:30 KST']) assert.equal(countText(content,expected),1); assert.equal(all(content).filter(node=>node.className==='fact').length,2);
assertFocusWrap(['← 뒤로','조건별 시나리오 ⌄','출처와 변경 이력 ⌄','목업 원문 · 실제 자료 아님','목업 원문 · 실제 자료 아님']); historyButton.click(); assert.equal(document.getElementById('history-panel').hidden,true);
// Production helper excludes hidden/aria-hidden/disabled/CSS-hidden descendants.
const hiddenParent=element('div'); hiddenParent.hidden=true; const hiddenButton=element('button','hidden'); hiddenParent.append(hiddenButton); detailRoot.append(hiddenParent);
const ariaParent=element('div'); ariaParent.setAttribute('aria-hidden','true'); const ariaButton=element('button','aria hidden'); ariaParent.append(ariaButton); detailRoot.append(ariaParent);
const disabled=element('button','disabled'); disabled.disabled=true; detailRoot.append(disabled);
const cssHidden=element('button','css hidden'); cssHidden.computedStyle.display='none'; detailRoot.append(cssHidden);
assert.ok(!visibleFocusable(detailRoot).includes(hiddenButton)); assert.ok(!visibleFocusable(detailRoot).includes(ariaButton)); assert.ok(!visibleFocusable(detailRoot).includes(disabled)); assert.ok(!visibleFocusable(detailRoot).includes(cssHidden));
console.log('behavior assertions passed');
'''
    result = subprocess.run(
        ["node", "-e", harness], text=True, capture_output=True, env=os.environ, check=False
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "behavior assertions passed"


def test_public_prototype_is_static_mock_without_order_surface():
    html = prototype_html()
    for required in (
        "Release 0 목업 · 실제 데이터가 아닙니다", "오늘 판단이 필요한 종목",
        "한빛반도체", "모노바이오", "동해전기", "현재 단계: 기록 전용", "주문 기능 없음",
        'role="dialog"', 'aria-modal="true"', 'aria-live="polite"', "min-height:44px",
        ":focus-visible", "prefers-reduced-motion", "font-size:clamp(16px",
    ):
        assert required in html
    for forbidden in ("실시간 시세", "주문 수량", "매수 주문", "매도 주문", "수익률", "fetch("):
        assert forbidden not in html


def test_prototype_mock_source_links_have_a_real_44px_touch_target():
    html = prototype_html()
    mock_link_rule = re.search(r"\.mock-link\{([^}]*)\}", html)
    assert mock_link_rule is not None
    declarations = mock_link_rule.group(1)
    assert "display:inline-flex" in declarations or "display:inline-block" in declarations
    assert "min-width:44px" in declarations
    assert "min-height:44px" in declarations


def test_prototype_mock_source_links_are_honest_and_allowlisted():
    client = TestClient(app)
    html = prototype_html()
    hrefs = re.findall(r"sourceUrl:'(/prototype/mock-source/[a-z-]+)'", html)
    assert len(hrefs) == 4
    assert len(set(hrefs)) == 4
    assert "setAttribute('target','_blank')" in html
    assert "setAttribute('rel','noopener noreferrer')" in html
    assert "목업 원문 · 실제 자료 아님" in html
    for href in hrefs:
        response = client.get(href)
        assert response.status_code == 200
        assert "목업 원문 · 실제 자료 아님" in response.text
        assert "실제 출처" in response.text
    assert client.get("/prototype/mock-source/not-an-allowed-source").status_code == 404


def test_prototype_detail_behavior_regression_contract():
    run_ui_behavior(prototype_html())
