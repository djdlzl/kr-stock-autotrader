"""Privacy-cache and truthful ARIA regression coverage."""
from fastapi.testclient import TestClient

from app import app
from kr_stock_autotrader.api import merge_vary


PRIVATE_CACHE_CONTROL = "no-store, private"


def contrast_ratio(foreground: str, background: str) -> float:
    def luminance(color: str) -> float:
        channels = [int(color[index:index + 2], 16) / 255 for index in (1, 3, 5)]
        linear = [value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4 for value in channels]
        return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]

    lighter, darker = sorted((luminance(foreground), luminance(background)), reverse=True)
    return (lighter + 0.05) / (darker + 0.05)


def assert_private_response(response):
    assert response.headers["cache-control"] == PRIVATE_CACHE_CONTROL
    assert "cookie" in {token.strip().lower() for token in response.headers["vary"].split(",")}


def test_vary_merge_preserves_existing_tokens_without_duplicate_cookie():
    assert merge_vary("Accept-Encoding, cookie", "Cookie") == "Accept-Encoding, cookie"
    assert merge_vary("Accept-Encoding", "Cookie") == "Accept-Encoding, Cookie"


def test_session_sensitive_routes_and_auth_posts_are_private_and_vary_by_cookie():
    anonymous = TestClient(app)
    assert_private_response(anonymous.get("/"))
    assert_private_response(anonymous.get("/app", follow_redirects=False))
    assert_private_response(anonymous.get("/api/plans"))

    signup = anonymous.post(
        "/api/signup", json={"email": "cache-owner@test.com", "password": "long-password"}
    )
    assert signup.status_code == 200
    assert_private_response(signup)
    assert_private_response(anonymous.get("/", follow_redirects=False))
    assert_private_response(anonymous.get("/app"))
    assert_private_response(anonymous.get("/api/plans"))
    assert_private_response(anonymous.post("/api/logout"))


def test_auth_shell_is_login_only_without_signup_controls_or_code():
    html = TestClient(app).get("/").text
    assert html.count('type="submit"') == 1
    assert "fetch('/api/login'" in html
    for forbidden in ("회원가입", "signup", "login-tab", "signup-tab", "role=\"group\"", "role=\"tablist\"", "role=\"tab\"", "aria-controls=\"login-panel\"", "aria-controls=\"signup-panel\""):
        assert forbidden not in html


def test_plan_stepper_is_step_navigation_not_false_tabs():
    client = TestClient(app)
    assert client.post(
        "/api/signup", json={"email": "aria-stepper@test.com", "password": "long-password"}
    ).status_code == 200
    html = client.get("/app").text
    assert '<nav class="stepper" aria-label="계획 작성 단계">' in html
    assert 'data-step="0" aria-current="step"' in html
    assert "setAttribute('aria-current'" in html
    assert 'role="tablist"' not in html
    assert 'role="tab"' not in html
    assert 'aria-controls="' not in html


def test_primary_and_success_tokens_meet_aa_and_plan_interactions_validate():
    assert contrast_ratio("#1f1d1b", "#3b82f6") >= 4.5
    assert contrast_ratio("#126b49", "#e9f8f0") >= 4.5
    client = TestClient(app)
    assert client.post(
        "/api/signup", json={"email": "form-accessibility@test.com", "password": "long-password"}
    ).status_code == 200
    html = client.get("/app").text
    assert ".primary{border:0;background:var(--primary);color:var(--ink)}" in html
    assert "backdrop-filter" not in html
    assert "<form id=\"plan-form\">" in html
    assert "panel.checkValidity()" in html and "panel.reportValidity()" in html
    assert "function syncLimitField()" in html
    assert "limitInput.disabled=!isLimit" in html
    assert "limitInput.required=isLimit" in html
    assert "if(!isLimit)limitInput.value=''" in html
    assert "syncLimitField();" in html
    assert "matchMedia('(prefers-reduced-motion: reduce)').matches?'auto':'smooth'" in html
    assert " — " not in html


def test_wizard_blocks_direct_forward_jumps_and_reveals_first_invalid_control():
    client = TestClient(app)
    assert client.post(
        "/api/signup", json={"email": "wizard-validation@test.com", "password": "long-password"}
    ).status_code == 200
    html = client.get("/app").text

    # The only step-button route is the guarded navigator: backwards is allowed,
    # while a forward jump must be exactly one validated panel.
    assert "function requestStep(target)" in html
    assert "if(target<=step){showStep(target);return}" in html
    assert "if(target!==step+1){tell(" in html
    assert "if(validatePanel(step))showStep(target)" in html
    assert "x.onclick=()=>requestStep(Number(x.dataset.step))" in html
    assert "$('#next-step').onclick=()=>requestStep(step+1)" in html
    assert "x.onclick=()=>showStep(Number(x.dataset.step))" not in html

    # Submit discovers the first invalid enabled control, reveals its containing
    # panel, reports it, focuses it, and only then permits the POST path.
    assert "function revealFirstInvalid(form)" in html
    assert "[...form.elements].find(control=>typeof control.checkValidity==='function'&&!control.checkValidity())" in html
    assert "invalid.closest('[data-step-panel]')" in html
    assert "if(panel)showStep(Number(panel.dataset.stepPanel))" in html
    assert "invalid.reportValidity();invalid.focus();return true" in html
    assert "if(!validatePlanForm())return;const f=new FormData(e.currentTarget)" in html


def test_wizard_validation_semantics_execute_in_lightweight_node_dom():
    """Exercise the embedded guard with a tiny DOM shim; no JS package is needed."""
    import shutil
    import subprocess

    from kr_stock_autotrader.ui import APP_HTML

    node = shutil.which("node")
    assert node, "Node is required for the repository's lightweight embedded-JS regression"
    script = "let step=0;" + APP_HTML.split("let step=0;", 1)[1].split("function addCondition()", 1)[0]
    harness = r'''
const events=[];
function control(valid=true){return {valid, disabled:false, required:false, value:'', hidden:false, report:0, focused:0, checkValidity(){return this.disabled||this.valid}, reportValidity(){this.report++;return this.checkValidity()}, focus(){this.focused++}, closest(selector){return selector==='[data-step-panel]'?this.panel:null}}}
function panel(n, controls=[]){const p=control(true);p.dataset={stepPanel:String(n)};p.controls=controls;p.checkValidity=()=>controls.every(x=>x.checkValidity());p.reportValidity=()=>{p.report++;return p.checkValidity()};return p}
const symbol=control(false), name=control(true), scheduled=control(true), qty=control(true), limit=control(false), condition=control(true);
const panels=[panel(0,[symbol,name,scheduled]),panel(1,[qty,limit]),panel(2,[condition])];
for(const p of panels) for(const c of p.controls) c.panel=p;
const wrap={hidden:true}, order={value:'market'}, form={elements:[symbol,name,scheduled,qty,limit,condition], addEventListener(){}};
const steps=[0,1,2].map(n=>({dataset:{step:String(n)},classList:{toggle(){}},setAttribute(){},removeAttribute(){}}));
const ids={'#plan-form':form,'[name="limit_price"]':limit,'#order-type':order,'#limit-wrap':wrap,'#back-step':{hidden:false},'#next-step':{hidden:false},'#save-plan':{hidden:false,addEventListener(){}}};
globalThis.document={querySelector(s){if(s.startsWith('[data-step-panel="')) return panels[Number(s.match(/\d+/)[0])]; return ids[s]},querySelectorAll(s){if(s==='[data-step-panel]') return panels;if(s==='.step') return steps;return []}};
globalThis.$=s=>document.querySelector(s);globalThis.tell=t=>events.push(t);
''' + script + r'''
showStep(0); syncLimitField(); if(!(limit.disabled && !limit.required && wrap.hidden && limit.value==='')) throw Error('market state');
order.value='limit'; syncLimitField(); if(limit.disabled || !limit.required || wrap.hidden) throw Error('limit state');
limit.value='70000'; order.value='market'; syncLimitField(); if(!limit.disabled || limit.required || !wrap.hidden || limit.value!=='') throw Error('market reset');
order.value='limit'; syncLimitField();
requestStep(2); if(events.length!==1 || panels[0].hidden) throw Error('direct jump bypass');
requestStep(1); if(panels[0].report!==1 || panels[1].hidden!==true) throw Error('invalid forward bypass');
symbol.valid=true; requestStep(1); if(panels[1].hidden) throw Error('valid next failed');
symbol.valid=false; if(!revealFirstInvalid(form) || panels[0].hidden || symbol.report!==1 || symbol.focused!==1) throw Error('invalid reveal');
'''
    result = subprocess.run([node, "-e", harness], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_plan_card_executes_explicit_buy_time_and_sell_condition_details():
    """Plan cards must expose the two decision-critical fields with clear labels."""
    import json
    import shutil
    import subprocess

    from kr_stock_autotrader.ui import APP_HTML

    node = shutil.which("node")
    assert node, "Node is required for the embedded plan-card regression"
    script = APP_HTML.split("const $=", 1)[1].split("function render(plans)", 1)[0]
    plan = {
        "id": 7,
        "symbol": "005930",
        "name": "삼성전자",
        "scheduled_at": "2026-08-31T09:00:00+09:00",
        "qty": 3,
        "order_type": "limit",
        "limit_price": 70000,
        "combine_mode": "AND",
        "status": "scheduled",
        "conditions": [
            {"kind": "absolute_price", "operator": ">=", "value": "75000"},
            {"kind": "deadline", "operator": ">=", "value": "2026-09-04T14:30:00+09:00"},
        ],
        "events": [],
    }
    harness = (
        "globalThis.document={querySelector(){return {textContent:'',className:''}}};"
        "const $=" + script
        + f";console.log(card({json.dumps(plan, ensure_ascii=False)}));"
    )
    result = subprocess.run([node, "-e", harness], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    card_html = result.stdout
    assert "매수 예정" in card_html
    assert "2026. 8. 31." in card_html and "오전 9:00" in card_html
    assert "매도 조건" in card_html and "모두 충족" in card_html
    assert "가격 75,000원 이상" in card_html
    assert "마감 시각 2026. 9. 4. 오후 2:30 이후" in card_html
