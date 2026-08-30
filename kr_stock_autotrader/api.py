"""FastAPI boundary and lightweight Korean paper-planning UI."""
import sqlite3
from typing import Literal
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field, field_validator, model_validator
from .auth import csrf_origin_ok, current_user, hash_password, issue_session, verify_password
from .config import COOKIE_SECURE, LIVE_TRADING
from .db import connect
from .domain import CONDITION_KINDS, OPERATORS, Quote, parse_kst
from .service import audit, evaluate_tick


class AuthIn(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=8, max_length=200)


class ConditionIn(BaseModel):
    kind: Literal["deadline", "absolute_price", "relative_pct", "volume", "relative_volume"]
    operator: Literal[">=", "<="]
    value: str | float

    @model_validator(mode="after")
    def valid_value(self):
        if self.kind == "deadline":
            try: parse_kst(str(self.value))
            except ValueError: raise ValueError("deadline은 ISO KST 날짜/시간이어야 합니다")
        else:
            try:
                if float(self.value) < 0 and self.kind in {"absolute_price", "volume", "relative_volume"}:
                    raise ValueError
            except (TypeError, ValueError): raise ValueError("조건 값이 올바르지 않습니다")
        return self


class PlanIn(BaseModel):
    symbol: str
    name: str
    scheduled_at: str
    qty: int = Field(gt=0)
    order_type: Literal["market", "limit"] = "market"
    limit_price: float | None = None
    combine_mode: Literal["OR", "AND"] = "OR"
    conditions: list[ConditionIn] = Field(default_factory=list, max_length=20)

    @field_validator("symbol")
    @classmethod
    def six_digits(cls, value):
        if not value.isdigit() or len(value) != 6: raise ValueError("종목코드는 6자리 숫자입니다")
        return value

    @field_validator("name")
    @classmethod
    def name_present(cls, value):
        if not value.strip(): raise ValueError("종목명을 입력하세요")
        return value.strip()

    @field_validator("scheduled_at")
    @classmethod
    def scheduled_kst(cls, value):
        return parse_kst(value).isoformat()

    @model_validator(mode="after")
    def limit_required(self):
        if self.order_type == "limit" and (self.limit_price is None or self.limit_price <= 0): raise ValueError("지정가 주문에는 양수 지정가가 필요합니다")
        return self


class TickIn(BaseModel):
    symbol: str
    price: float = Field(gt=0)
    volume: float = Field(ge=0)
    baseline_volume: float = Field(ge=0)
    known_at: str | None = None
    idempotency_key: str = Field(min_length=1, max_length=200)

    @field_validator("symbol")
    @classmethod
    def tick_symbol(cls, value):
        if not value.isdigit() or len(value) != 6: raise ValueError("종목코드는 6자리 숫자입니다")
        return value


app = FastAPI(title="KR Stock Autotrader — Paper Only")


def set_session(response: Response, uid: int) -> None:
    response.set_cookie("session", issue_session(uid), httponly=True, samesite="lax", secure=COOKIE_SECURE, max_age=28800)


@app.post("/api/signup")
def signup(data: AuthIn, request: Request, response: Response):
    csrf_origin_ok(request); db = connect()
    try:
        row = db.execute("INSERT INTO users(email,password) VALUES(?,?) RETURNING id", (data.email.lower(), hash_password(data.password))).fetchone(); db.commit()
    except sqlite3.IntegrityError: raise HTTPException(409, "이미 가입된 이메일입니다")
    set_session(response, row["id"]); return {"ok": True}


@app.post("/api/login")
def login(data: AuthIn, request: Request, response: Response):
    csrf_origin_ok(request); row = connect().execute("SELECT * FROM users WHERE email=?", (data.email.lower(),)).fetchone()
    if not row or not verify_password(data.password, row["password"]): raise HTTPException(401, "로그인 정보가 올바르지 않습니다")
    set_session(response, row["id"]); return {"ok": True}


@app.post("/api/logout")
def logout(request: Request, response: Response):
    csrf_origin_ok(request); response.delete_cookie("session", httponly=True, samesite="lax", secure=COOKIE_SECURE); return {"ok": True}


@app.post("/api/plans")
def create_plan(data: PlanIn, request: Request):
    csrf_origin_ok(request); uid = current_user(request); db = connect()
    row = db.execute("INSERT INTO plans(user_id,symbol,name,scheduled_at,qty,order_type,limit_price,combine_mode) VALUES(?,?,?,?,?,?,?,?) RETURNING id", (uid,data.symbol,data.name,data.scheduled_at,data.qty,data.order_type,data.limit_price,data.combine_mode)).fetchone()
    db.executemany("INSERT INTO conditions(plan_id,kind,operator,value) VALUES(?,?,?,?)", [(row["id"], c.kind,c.operator,str(c.value)) for c in data.conditions])
    audit(db,row["id"],"created"); db.commit(); return {"id":row["id"],"status":"scheduled"}


@app.get("/api/plans")
def list_plans(request: Request):
    uid=current_user(request); db=connect(); output=[]
    for plan in db.execute("SELECT * FROM plans WHERE user_id=? ORDER BY id DESC",(uid,)):
        item=dict(plan); item["conditions"]=[dict(x) for x in db.execute("SELECT kind,operator,value FROM conditions WHERE plan_id=?",(plan["id"],))]; item["events"]=[dict(x) for x in db.execute("SELECT event,reason,at FROM events WHERE plan_id=? ORDER BY id",(plan["id"],))]; item["fills"]=[dict(x) for x in db.execute("SELECT side,qty,price,filled_at,fee,tax,slippage FROM fills WHERE plan_id=? ORDER BY id",(plan["id"],))]; output.append(item)
    return output


@app.post("/api/plans/{plan_id}/cancel")
def cancel(plan_id: int, request: Request):
    csrf_origin_ok(request); uid=current_user(request); db=connect(); row=db.execute("SELECT * FROM plans WHERE id=? AND user_id=?",(plan_id,uid)).fetchone()
    if not row: raise HTTPException(404,"계획을 찾을 수 없습니다")
    if row["status"] not in {"closed","cancelled"}: db.execute("UPDATE plans SET status='cancelled' WHERE id=? AND user_id=?",(plan_id,uid)); audit(db,plan_id,"cancelled"); db.commit()
    return {"status":"cancelled"}


@app.post("/api/ticks")
def submit_tick(data: TickIn, request: Request):
    csrf_origin_ok(request); uid=current_user(request); db=connect(); known_at=parse_kst(data.known_at) if data.known_at else __import__('kr_stock_autotrader.domain',fromlist=['now_kst']).now_kst(); quote=Quote(data.symbol,data.price,data.volume,data.baseline_volume,known_at)
    db.execute("INSERT OR REPLACE INTO quotes VALUES(?,?,?,?,?,?)",(uid,data.symbol,data.price,data.volume,data.baseline_volume,known_at.isoformat()))
    evaluate_tick(db,uid,quote,data.idempotency_key); db.commit(); return {"ok":True,"mode":"paper","live_trading":LIVE_TRADING}


@app.get("/", response_class=HTMLResponse)
def home():
    return HTMLResponse(HTML)


HTML = '''<!doctype html><html lang="ko"><meta name="viewport" content="width=device-width,initial-scale=1"><title>국내주식 모의투자</title><style>body{font:15px system-ui;margin:auto;max-width:760px;padding:12px;background:#f5f7fb}*{box-sizing:border-box}input,select,button,textarea{width:100%;padding:9px;margin:4px 0}section{background:#fff;padding:12px;margin:10px 0;border-radius:9px;overflow-wrap:anywhere}.warn{color:#9a3412}.row{display:grid;grid-template-columns:1fr 1fr;gap:7px}pre{white-space:pre-wrap;overflow-wrap:anywhere}@media(max-width:390px){.row{grid-template-columns:1fr}}</style><h1>국내주식 자동매매 계획</h1><p class=warn><b>모의투자 전용</b> · 실주문은 영구 비활성화 · 비용/세금/슬리피지 0</p><section><input id=e placeholder=이메일><input id=p type=password placeholder="비밀번호 8자 이상"><button onclick="auth('signup')">가입</button><button onclick="auth('login')">로그인</button></section><section><h2>매수 계획</h2><div class=row><input id=s value=005930><input id=n value=삼성전자><input id=d type=datetime-local><input id=q type=number value=1 min=1><select id=o><option value=market>시장가</option><option value=limit>지정가</option></select><input id=l type=number placeholder=지정가></div><select id=combine><option>OR</option><option>AND</option></select><div id=conds></div><button onclick=addCond()>매도 조건 추가</button><small>조건을 비워두면 자동매도하지 않습니다.</small><button onclick=save()>계획 저장</button></section><section><h2>시세 입력/평가</h2><div class=row><input id=price type=number value=70000><input id=vol type=number value=100><input id=base type=number value=100><input id=known type=datetime-local></div><button onclick=tick()>모의 tick 평가</button></section><section><button onclick=list()>내 상태·체결·감사로그 새로고침</button><pre id=out>로그인하세요.</pre></section><script>const api=(u,o={})=>fetch('/api/'+u,{headers:{'Content-Type':'application/json'},...o}).then(async r=>{let j=await r.json();if(!r.ok)throw Error(j.detail||JSON.stringify(j));return j});function auth(x){api(x,{method:'POST',body:JSON.stringify({email:e.value,password:p.value})}).then(list).catch(alert)}function addCond(){conds.insertAdjacentHTML('beforeend','<div class=row><select class=k><option value=absolute_price>가격</option><option value=relative_pct>수익률%</option><option value=volume>거래량</option><option value=relative_volume>상대 거래량</option><option value=deadline>마감 ISO시간</option></select><select class=op><option>>=</option><option><=</option></select><input class=v placeholder="값 또는 2026-08-31T15:00+09:00"><button onclick="this.parentElement.remove()">삭제</button></div>')}function save(){let cs=[...document.querySelectorAll('#conds>div')].map(x=>({kind:x.querySelector('.k').value,operator:x.querySelector('.op').value,value:x.querySelector('.v').value}));api('plans',{method:'POST',body:JSON.stringify({symbol:s.value,name:n.value,scheduled_at:d.value,qty:+q.value,order_type:o.value,limit_price:l.value?+l.value:null,combine_mode:combine.value,conditions:cs})}).then(list).catch(alert)}function tick(){api('ticks',{method:'POST',body:JSON.stringify({symbol:s.value,price:+price.value,volume:+vol.value,baseline_volume:+base.value,known_at:known.value||null,idempotency_key:crypto.randomUUID()})}).then(list).catch(alert)}function list(){api('plans').then(x=>out.textContent=JSON.stringify(x,null,2)).catch(alert)}</script></html>'''
