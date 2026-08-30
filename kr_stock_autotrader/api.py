"""FastAPI boundary for Giraffe's paper-only trading planner."""
import sqlite3
from typing import Literal

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel, Field, field_validator, model_validator

from .auth import csrf_origin_ok, current_user, hash_password, issue_session, verify_password
from .config import COOKIE_SECURE, LIVE_TRADING
from .db import connect
from .domain import Quote, parse_kst
from .service import audit, evaluate_tick
from .ui import APP_HTML, AUTH_HTML


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
            try:
                parse_kst(str(self.value))
            except ValueError:
                raise ValueError("deadline은 ISO KST 날짜/시간이어야 합니다")
        else:
            try:
                if float(self.value) < 0 and self.kind in {"absolute_price", "volume", "relative_volume"}:
                    raise ValueError
            except (TypeError, ValueError):
                raise ValueError("조건 값이 올바르지 않습니다")
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
        if not value.isdigit() or len(value) != 6:
            raise ValueError("종목코드는 6자리 숫자입니다")
        return value

    @field_validator("name")
    @classmethod
    def name_present(cls, value):
        if not value.strip():
            raise ValueError("종목명을 입력하세요")
        return value.strip()

    @field_validator("scheduled_at")
    @classmethod
    def scheduled_kst(cls, value):
        return parse_kst(value).isoformat()

    @model_validator(mode="after")
    def limit_required(self):
        if self.order_type == "limit" and (self.limit_price is None or self.limit_price <= 0):
            raise ValueError("지정가 주문에는 양수 지정가가 필요합니다")
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
        if not value.isdigit() or len(value) != 6:
            raise ValueError("종목코드는 6자리 숫자입니다")
        return value


app = FastAPI(title="Giraffe — Paper Only")


def merge_vary(existing: str | None, token: str) -> str:
    """Add a Vary token once without discarding upstream response variants."""
    tokens = [item.strip() for item in (existing or "").split(",") if item.strip()]
    if token.lower() not in {item.lower() for item in tokens}:
        tokens.append(token)
    return ", ".join(tokens)


@app.middleware("http")
async def prevent_session_response_caching(request: Request, call_next):
    """Never let shared caches reuse auth, redirect, or user-data responses."""
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store, private"
    response.headers["Vary"] = merge_vary(response.headers.get("Vary"), "Cookie")
    return response


def set_session(response: Response, uid: int) -> None:
    response.set_cookie(
        "session", issue_session(uid), httponly=True, samesite="lax",
        secure=COOKIE_SECURE, max_age=28800,
    )


@app.post("/api/signup")
def signup(data: AuthIn, request: Request, response: Response):
    csrf_origin_ok(request)
    db = connect()
    try:
        row = db.execute(
            "INSERT INTO users(email,password) VALUES(?,?) RETURNING id",
            (data.email.lower(), hash_password(data.password)),
        ).fetchone()
        db.commit()
    except sqlite3.IntegrityError:
        raise HTTPException(409, "이미 가입된 이메일입니다")
    finally:
        db.close()
    set_session(response, row["id"])
    return {"ok": True}


@app.post("/api/login")
def login(data: AuthIn, request: Request, response: Response):
    csrf_origin_ok(request)
    db = connect()
    try:
        row = db.execute("SELECT * FROM users WHERE email=?", (data.email.lower(),)).fetchone()
    finally:
        db.close()
    if not row or not verify_password(data.password, row["password"]):
        raise HTTPException(401, "로그인 정보가 올바르지 않습니다")
    set_session(response, row["id"])
    return {"ok": True}


@app.post("/api/logout")
def logout(request: Request, response: Response):
    csrf_origin_ok(request)
    response.delete_cookie("session", httponly=True, samesite="lax", secure=COOKIE_SECURE)
    return {"ok": True}


@app.post("/api/plans")
def create_plan(data: PlanIn, request: Request):
    csrf_origin_ok(request)
    uid = current_user(request)
    db = connect()
    try:
        row = db.execute(
            "INSERT INTO plans(user_id,symbol,name,scheduled_at,qty,order_type,limit_price,combine_mode) VALUES(?,?,?,?,?,?,?,?) RETURNING id",
            (uid, data.symbol, data.name, data.scheduled_at, data.qty, data.order_type, data.limit_price, data.combine_mode),
        ).fetchone()
        db.executemany(
            "INSERT INTO conditions(plan_id,kind,operator,value) VALUES(?,?,?,?)",
            [(row["id"], c.kind, c.operator, str(c.value)) for c in data.conditions],
        )
        audit(db, row["id"], "created")
        db.commit()
        return {"id": row["id"], "status": "scheduled"}
    finally:
        db.close()


@app.get("/api/plans")
def list_plans(request: Request):
    uid = current_user(request)
    db = connect()
    try:
        output = []
        for plan in db.execute("SELECT * FROM plans WHERE user_id=? ORDER BY id DESC", (uid,)):
            item = dict(plan)
            item["conditions"] = [dict(x) for x in db.execute("SELECT kind,operator,value FROM conditions WHERE plan_id=?", (plan["id"],))]
            item["events"] = [dict(x) for x in db.execute("SELECT event,reason,at FROM events WHERE plan_id=? ORDER BY id", (plan["id"],))]
            item["fills"] = [dict(x) for x in db.execute("SELECT side,qty,price,filled_at,fee,tax,slippage FROM fills WHERE plan_id=? ORDER BY id", (plan["id"],))]
            output.append(item)
        return output
    finally:
        db.close()


@app.post("/api/plans/{plan_id}/cancel")
def cancel(plan_id: int, request: Request):
    csrf_origin_ok(request)
    uid = current_user(request)
    db = connect()
    try:
        transition = db.execute(
            "UPDATE plans SET status='cancelled' WHERE id=? AND user_id=? AND status NOT IN ('closed','cancelled')",
            (plan_id, uid),
        )
        if transition.rowcount == 1:
            audit(db, plan_id, "cancelled", key=f"cancel:{plan_id}")
            db.commit()
            return {"status": "cancelled", "idempotent": False}
        db.rollback()
        row = db.execute("SELECT status FROM plans WHERE id=? AND user_id=?", (plan_id, uid)).fetchone()
        if not row:
            raise HTTPException(404, "계획을 찾을 수 없습니다")
        if row["status"] == "closed":
            raise HTTPException(409, {"message": "종료된 계획은 취소할 수 없습니다", "current_status": "closed"})
        if row["status"] == "cancelled":
            return {"status": "cancelled", "idempotent": True}
        raise HTTPException(409, {"message": "계획을 취소할 수 없습니다", "current_status": row["status"]})
    finally:
        db.close()


@app.post("/api/ticks")
def submit_tick(data: TickIn, request: Request):
    csrf_origin_ok(request)
    uid = current_user(request)
    db = connect()
    try:
        known_at = parse_kst(data.known_at) if data.known_at else __import__("kr_stock_autotrader.domain", fromlist=["now_kst"]).now_kst()
        quote = Quote(data.symbol, data.price, data.volume, data.baseline_volume, known_at)
        db.execute("INSERT OR REPLACE INTO quotes VALUES(?,?,?,?,?,?)", (uid, data.symbol, data.price, data.volume, data.baseline_volume, known_at.isoformat()))
        evaluate_tick(db, uid, quote, data.idempotency_key)
        db.commit()
        return {"ok": True, "mode": "paper", "live_trading": LIVE_TRADING}
    finally:
        db.close()


@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    """The only unauthenticated page is the public authentication shell."""
    try:
        current_user(request)
    except HTTPException:
        return HTMLResponse(AUTH_HTML)
    return RedirectResponse("/app", status_code=303)


@app.get("/app", response_class=HTMLResponse)
def application(request: Request):
    """Browser navigation redirects unauthenticated visitors to the auth gate."""
    try:
        current_user(request)
    except HTTPException:
        return RedirectResponse("/", status_code=303)
    return HTMLResponse(APP_HTML)
