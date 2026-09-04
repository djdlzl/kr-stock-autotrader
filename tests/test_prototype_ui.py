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
  constructor(tag) { this.tagName=tag; this.children=[]; this.attrs={}; this.listeners={}; this.hidden=false; this.disabled=false; this.inert=false; this.textContent=''; this.className=''; }
  set id(value) { this._id=value; document.nodes[value]=this; } get id() { return this._id; }
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
    const result=[]; const walk=node => { for (const child of node.children) { if ((selector==='.panel' && child.className.split(' ').includes('panel')) || (selector==='.disclosure' && child.className.split(' ').includes('disclosure')) || (selector==='[aria-current="true"]' && child.getAttribute('aria-current')==='true') || (selector.includes('button:not') && child.tagName==='button' && !child.disabled)) result.push(child); walk(child); } }; walk(this); return result;
  }
}
const document = global.document = {
  nodes:{}, activeElement:null, listeners:{},
  createElement: tag => new Element(tag),
  getElementById: id => document.nodes[id],
  addEventListener: (type,fn) => { document.listeners[type]=fn; },
  dispatch(key,shiftKey=false) { let prevented=false; document.listeners.keydown({key,shiftKey,preventDefault(){prevented=true}}); return prevented; }
};
function fixed(id, tag='div') { const node=new Element(tag); node.id=id; return node; }
const opener=fixed('fresh-opener','button'); opener.focus();
fixed('prototype-list'); const detailRoot=fixed('detail'); fixed('detail-title','h1'); const detailContent=fixed('detail-content'); const back=fixed('back','button'); detailRoot.append(back, detailContent); fixed('status-live');
''' + script + r'''
function text(node) { return node.textContent + node.children.map(text).join(''); }
function all(node) { return [node, ...node.children.flatMap(all)]; }
function byText(root, expected) { return all(root).find(node => node.textContent === expected); }
function openAndAssert(id, expectedStatus) {
  opener.focus(); openItem(id);
  const detail=document.getElementById('detail'), list=document.getElementById('prototype-list'), content=document.getElementById('detail-content');
  assert.equal(detail.hidden,false); assert.equal(list.inert,true); assert.equal(list.getAttribute('aria-hidden'),'true');
  assert.ok(text(content).includes(expectedStatus));
  const scenarios=document.getElementById('scenarios-panel');
  assert.equal(scenarios.hidden,true); assert.equal(byText(content,'조건별 시나리오 ⌄').getAttribute('aria-expanded'),'false');
  return content;
}
let content=openAndAssert('fresh','가설 유지 · 기준 조건을 검토합니다.');
let current=document.getElementById('detail-content').querySelectorAll('[aria-current="true"]');
assert.equal(current.length,1); assert.equal(current[0].id,'scenario-base');
let scenariosButton=byText(content,'조건별 시나리오 ⌄'); scenariosButton.click(); assert.equal(document.getElementById('scenarios-panel').hidden,false); assert.equal(scenariosButton.getAttribute('aria-expanded'),'true');
let freshHistory=byText(content,'출처와 변경 이력 ⌄'); freshHistory.click(); for (const expected of ['한빛반도체 공시 목업','목업 원문 보기','공개 확인 시각: 2026-09-04 09:00 KST','마지막 확인 성공시각: 2026-09-04 10:00 KST','신뢰 상태: 확인됨']) assert.ok(text(content).includes(expected));
assert.equal(document.dispatch('Escape'),true); assert.equal(document.getElementById('detail').hidden,true); assert.equal(document.activeElement,opener); assert.equal(document.getElementById('prototype-list').inert,false);
content=openAndAssert('stale','판단 보류 · 정보가 오래됨.');
let historyButton=byText(content,'출처와 변경 이력 ⌄'); historyButton.click(); for (const expected of ['모노바이오 임상 발표 목업','공개 확인 시각: 2026-08-28 09:00 KST','마지막 확인 성공시각: 2026-08-28 10:00 KST','신뢰 상태: 오래됨']) assert.ok(text(content).includes(expected)); const retry=byText(content,'다시 확인'); retry.click(); assert.equal(retry.textContent,'재확인 중'); assert.equal(document.getElementById('status-live').textContent,'재확인 중'); timers.shift()(); assert.equal(retry.textContent,'여전히 정보가 오래됨'); assert.equal(document.getElementById('status-live').textContent,'재확인 완료: 여전히 정보가 오래됨');
closeDetail(); content=openAndAssert('conflict','판단 보류 · 출처 충돌.'); historyButton=byText(content,'출처와 변경 이력 ⌄'); historyButton.click(); for (const expected of ['동해전기 공식 자료 목업','동해전기 보도자료 목업','공개 확인 시각: 2026-09-03 08:30 KST','마지막 확인 성공시각: 2026-09-04 09:30 KST','신뢰 상태: 충돌 확인','“양산은 10월에 시작합니다.”','“양산은 11월에 시작합니다.”']) assert.ok(text(content).includes(expected)); assert.equal(document.getElementById('history-panel').hidden,false);
const detail=document.getElementById('detail'); const focusable=detail.querySelectorAll('button:not([disabled]),[href],input,select,textarea,[tabindex]:not([tabindex="-1"])'); focusable[focusable.length-1].focus(); assert.equal(document.dispatch('Tab'),true); assert.equal(document.activeElement,focusable[0]); focusable[0].focus(); assert.equal(document.dispatch('Tab',true),true); assert.equal(document.activeElement,focusable[focusable.length-1]);
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


def test_prototype_detail_behavior_regression_contract():
    run_ui_behavior(prototype_html())
