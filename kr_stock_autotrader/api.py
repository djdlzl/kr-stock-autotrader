"""FastAPI boundary for Giraffe's paper-only trading planner."""
import math
import sqlite3
import re
import threading
import time as monotonic_time
from contextlib import contextmanager
from datetime import datetime, time, timedelta
from typing import Literal

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel, Field, StrictInt, ValidationError, field_validator, model_validator

from .auth import csrf_origin_ok, current_user, hash_password, issue_session, verify_password
from .config import COOKIE_SECURE, LIVE_TRADING, SIGNUP_ENABLED
from .db import connect
from .domain import Quote, parse_kst, now_kst
from .decision_cards import (require_internal_api_key, create_evidence, list_evidence, evidence_detail, mutate_evidence, save_filter, filter_detail, save_card, list_cards, card_detail, user_card_view, user_decision, evaluate_order_plan, edit_order_plan, edit_draft)
from .service import audit, evaluate_tick
from .ui import APP_HTML, AUTH_HTML
from .kis_readonly import KISReadOnlyClient
from .live_dry_run import existing_live_dry_run_receipt, persist_live_dry_run


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


class PaperSettingsIn(BaseModel):
    # Strict integer prevents bool, floats, numeric strings, and non-finite values.
    default_paper_amount: StrictInt = Field(ge=10_000, le=1_000_000_000)


class LiveDryRunIn(BaseModel):
    dry_run_key: str = Field(pattern=r"^[A-Za-z0-9_-]{8,64}$")


# Single-process contract: keyed locks serialize work without retaining every seen key.
# Registry entries count their holder and queued waiters, and disappear after the last exit.
class _LockSlot:
    def __init__(self):
        self.lock = threading.Lock()
        self.references = 0


_dry_run_locks: dict[tuple[int, int, str], _LockSlot] = {}
_dry_run_locks_guard = threading.Lock()
_quote_locks: dict[tuple[int, str], _LockSlot] = {}
_quote_locks_guard = threading.Lock()
_quote_cache: dict[tuple[int, str], tuple[float, dict]] = {}
_quote_cache_lock = threading.Lock()
QUOTE_CACHE_TTL_SECONDS = 2.0
QUOTE_CACHE_MAX_ENTRIES = 256
_default_kis_client: KISReadOnlyClient | None = None


@contextmanager
def _registered_lock(registry: dict, guard: threading.Lock, identity: tuple):
    with guard:
        slot = registry.get(identity)
        if slot is None:
            slot = _LockSlot()
            registry[identity] = slot
        slot.references += 1
    acquired = False
    try:
        slot.lock.acquire()
        acquired = True
        yield
    finally:
        if acquired:
            slot.lock.release()
        with guard:
            slot.references -= 1
            if slot.references == 0 and registry.get(identity) is slot:
                del registry[identity]


def _purge_quote_cache(now: float) -> None:
    for key, (cached_at, _) in tuple(_quote_cache.items()):
        if now - cached_at > QUOTE_CACHE_TTL_SECONDS:
            del _quote_cache[key]


def _cache_get(key: tuple[int, str], now: float) -> dict | None:
    with _quote_cache_lock:
        _purge_quote_cache(now)
        cached = _quote_cache.get(key)
        return dict(cached[1]) if cached else None


_KIS_OPTIONAL_STATUS_FIELDS = ("market_status", "halt_status", "management_status")
_KIS_APPROVED_TIMESTAMP_SOURCES = {"network_retrieved_at"}


def _valid_kis_retrieved_at(value: object) -> str | None:
    if not isinstance(value, str) or any(ord(char) < 32 or ord(char) == 127 for char in value):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return value if parsed.tzinfo is not None and parsed.utcoffset() is not None else None


def _kis_timestamp_pair_is_current(retrieved_at: str, quote_known_at: str) -> bool:
    """Accept only coherent, current network-observation timestamps.

    The KIS client stamps both fields with the same retrieval-completion instant.
    Re-parse the accepted text so alternate ISO offset formatting for that instant
    remains valid while arbitrary provider times never reach the cache.
    """
    try:
        retrieved = datetime.fromisoformat(retrieved_at.replace("Z", "+00:00"))
        known = datetime.fromisoformat(quote_known_at.replace("Z", "+00:00"))
        age = now_kst() - retrieved
    except (TypeError, ValueError):
        return False
    return known == retrieved and timedelta(0) <= age <= timedelta(minutes=5)


def _valid_kis_number(value: object, *, minimum: float) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    normalized = float(value)
    return normalized if math.isfinite(normalized) and normalized >= minimum else None


def _valid_omitted_kis_status(value: object) -> bool:
    """Validate supplied optional statuses even though no status is projected yet."""
    return isinstance(value, str) and len(value) <= 64 and not any(ord(char) < 32 or ord(char) == 127 for char in value)


def _safe_kis_quote(symbol: str, outcome: object) -> dict:
    """Project provider outcomes onto a closed schema that cannot carry raw KIS data."""
    unavailable = {"symbol": symbol, "status": "unavailable", "source": "KIS", "environment": "production"}
    if not isinstance(outcome, dict):
        return unavailable
    retrieved_at = _valid_kis_retrieved_at(outcome.get("retrieved_at"))
    timestamp_source = outcome.get("timestamp_source")
    if outcome.get("status") != "ok":
        if retrieved_at is not None:
            unavailable["retrieved_at"] = retrieved_at
        if timestamp_source in _KIS_APPROVED_TIMESTAMP_SOURCES:
            unavailable["timestamp_source"] = timestamp_source
        return unavailable
    quote_known_at = _valid_kis_retrieved_at(outcome.get("quote_known_at"))
    price = _valid_kis_number(outcome.get("price"), minimum=0.0)
    volume = _valid_kis_number(outcome.get("volume"), minimum=0.0)
    if (
        not isinstance(symbol, str)
        or re.fullmatch(r"\d{6}", symbol) is None
        or outcome.get("symbol") != symbol
        or price is None or price <= 0
        or volume is None
        or retrieved_at is None or quote_known_at is None
        or not _kis_timestamp_pair_is_current(retrieved_at, quote_known_at)
        or timestamp_source != "network_retrieved_at"
        or any(field in outcome and not _valid_omitted_kis_status(outcome[field]) for field in _KIS_OPTIONAL_STATUS_FIELDS)
    ):
        return unavailable
    return {
        "symbol": symbol,
        "price": price,
        "volume": volume,
        "quote_known_at": quote_known_at,
        "retrieved_at": retrieved_at,
        "timestamp_source": "network_retrieved_at",
        "source": "KIS",
        "environment": "production",
        "status": "ok",
    }


def _cache_success(key: tuple[int, str], quote: dict) -> dict:
    safe = dict(quote)
    with _quote_cache_lock:
        _purge_quote_cache(monotonic_time.monotonic())
        while len(_quote_cache) >= QUOTE_CACHE_MAX_ENTRIES:
            del _quote_cache[next(iter(_quote_cache))]
        _quote_cache[key] = (monotonic_time.monotonic(), safe)
    return dict(safe)

def _quote_provider():
    global _default_kis_client
    provider = getattr(app.state, "kis_quote_provider", None)
    if provider:
        return provider
    if _default_kis_client is None:
        _default_kis_client = KISReadOnlyClient()
    return _default_kis_client.current_price

def _cached_kis_quote(symbol: str) -> dict:
    provider = _quote_provider()
    key = (id(getattr(provider, "__self__", provider)), symbol)
    cached = _cache_get(key, monotonic_time.monotonic())
    if cached is not None:
        return _safe_kis_quote(symbol, cached)
    # The provider runs outside cache metadata guards.  The keyed lock makes a
    # simultaneous miss single-flight, then every waiter consumes one projection.
    with _registered_lock(_quote_locks, _quote_locks_guard, key):
        cached = _cache_get(key, monotonic_time.monotonic())
        if cached is not None:
            return _safe_kis_quote(symbol, cached)
        try:
            quote = _safe_kis_quote(symbol, provider(symbol))
        except Exception:
            quote = _safe_kis_quote(symbol, None)
        if quote["status"] == "ok":
            return _cache_success(key, quote)
        return quote


def _dry_run_lock(plan_id: int, user_id: int, key: str):
    return _registered_lock(_dry_run_locks, _dry_run_locks_guard, (plan_id, user_id, key))


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
async def signup(request: Request, response: Response):
    # Do not parse, validate, or query for signup attempts unless development
    # has explicitly enabled the route before import.
    if not SIGNUP_ENABLED:
        raise HTTPException(404, "찾을 수 없습니다")
    csrf_origin_ok(request)
    try:
        data = AuthIn.model_validate(await request.json())
    except (ValidationError, ValueError):
        raise HTTPException(422, "입력 내용을 확인하세요")
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


@app.get("/api/kis/status")
def kis_status(request: Request):
    current_user(request)
    return KISReadOnlyClient.readiness()


@app.get("/api/kis/quote/{symbol}")
def kis_quote(symbol: str, request: Request):
    current_user(request)
    if not re.fullmatch(r"\d{6}", symbol):
        raise HTTPException(422, "종목코드는 6자리 숫자입니다")
    return _cached_kis_quote(symbol)


@app.post("/api/order-plans/{plan_id}/live-dry-run")
def live_dry_run(plan_id: int, data: LiveDryRunIn, request: Request):
    csrf_origin_ok(request)
    uid = current_user(request)
    db = connect()
    try:
        plan = db.execute("SELECT * FROM order_plans WHERE id=? AND user_id=?", (plan_id, uid)).fetchone()
        if not plan:
            raise HTTPException(404, "order plan not found")
        if plan["status"] != "approved":
            raise HTTPException(409, "승인된 현재 계획만 사전점검할 수 있습니다")
        # Receipt lookup is deliberately before provider/OAuth work.
        existing = existing_live_dry_run_receipt(db, plan_id, uid, data.dry_run_key)
        if existing:
            return existing
        lock = _dry_run_lock(plan_id, uid, data.dry_run_key)
        with lock:
            # A concurrent first request may have completed while we waited.
            existing = existing_live_dry_run_receipt(db, plan_id, uid, data.dry_run_key)
            if existing:
                return existing
            quote = _cached_kis_quote(plan["symbol"])
            try:
                return persist_live_dry_run(db, dict(plan), uid, data.dry_run_key, quote)
            except sqlite3.IntegrityError:
                db.rollback()
                existing = existing_live_dry_run_receipt(db, plan_id, uid, data.dry_run_key)
                if existing:
                    return existing
                raise
    finally:
        db.close()


@app.get("/api/settings/paper")
def get_paper_settings(request: Request):
    uid = current_user(request)
    db = connect()
    try:
        row = db.execute("SELECT default_paper_amount FROM user_settings WHERE user_id=?", (uid,)).fetchone()
        return {"default_paper_amount": int(row["default_paper_amount"]) if row else 500000}
    finally:
        db.close()


@app.patch("/api/settings/paper")
def update_paper_settings(data: PaperSettingsIn, request: Request):
    csrf_origin_ok(request)
    uid = current_user(request)
    db = connect()
    try:
        db.execute("INSERT INTO user_settings(user_id,default_paper_amount) VALUES(?,?) ON CONFLICT(user_id) DO UPDATE SET default_paper_amount=excluded.default_paper_amount", (uid, data.default_paper_amount))
        db.commit()
        return {"default_paper_amount": data.default_paper_amount}
    finally:
        db.close()


@app.post("/api/plans")
def create_plan(data: PlanIn, request: Request):
    csrf_origin_ok(request)
    uid = current_user(request)
    db = connect()
    try:
        row = db.execute(
            "INSERT INTO plans(user_id,symbol,name,scheduled_at,qty,order_type,limit_price,combine_mode,status) VALUES(?,?,?,?,?,?,?,?, 'manual_only') RETURNING id",
            (uid, data.symbol, data.name, data.scheduled_at, data.qty, data.order_type, data.limit_price, data.combine_mode),
        ).fetchone()
        db.executemany(
            "INSERT INTO conditions(plan_id,kind,operator,value) VALUES(?,?,?,?)",
            [(row["id"], c.kind, c.operator, str(c.value)) for c in data.conditions],
        )
        audit(db, row["id"], "created")
        db.commit()
        return {"id": row["id"], "status": "manual_only", "executable": False}
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


@app.post('/api/internal/evidence')
async def internal_evidence_create(request: Request, _: None = Depends(require_internal_api_key)):
    data = await request.json(); db = connect()
    try: return create_evidence(db, data)
    finally: db.close()

@app.get('/api/internal/evidence')
def internal_evidence_list(symbol: str | None = None, status: str | None = None, date: str | None = None, _: None = Depends(require_internal_api_key)):
    db = connect()
    try: return list_evidence(db, symbol, status, date)
    finally: db.close()

@app.get('/api/internal/evidence/{evidence_id}')
def internal_evidence_detail(evidence_id: int, _: None = Depends(require_internal_api_key)):
    db = connect()
    try: return evidence_detail(db, evidence_id)
    finally: db.close()

@app.patch('/api/internal/evidence/{evidence_id}')
async def internal_evidence_update(evidence_id: int, request: Request, _: None = Depends(require_internal_api_key)):
    db = connect()
    try: return mutate_evidence(db, evidence_id, await request.json())
    finally: db.close()

@app.post('/api/internal/evidence/{evidence_id}/invalidate')
def internal_evidence_invalidate(evidence_id: int, _: None = Depends(require_internal_api_key)):
    db = connect()
    try: return mutate_evidence(db, evidence_id, invalidate=True)
    finally: db.close()

@app.post('/api/internal/filters')
async def internal_filter(request: Request, _: None = Depends(require_internal_api_key)):
    data=await request.json(); db=connect()
    try:return save_filter(db,data['evidence_id'],data['inputs'],data['as_of'],data['known_at'])
    finally:db.close()

@app.get('/api/internal/filters/{filter_id}')
def internal_filter_detail(filter_id: int, _: None = Depends(require_internal_api_key)):
    db=connect()
    try:return filter_detail(db,filter_id)
    finally:db.close()

@app.post('/api/internal/cards/generate')
async def internal_card_generate(request: Request, _: None = Depends(require_internal_api_key)):
    """Create no LLM output: callers receive the immutable prompt/input request to run externally."""
    data=await request.json(); db=connect()
    try:
        ev=evidence_detail(db,data['evidence_id']); fi=filter_detail(db,data['filter_id'])
        return {'prompt_version':'decision-card-v1','prompt_hash':__import__('kr_stock_autotrader.decision_cards',fromlist=['prompt_hash']).prompt_hash(),'evidence':ev,'filter':fi}
    finally:db.close()

@app.post('/api/internal/cards/results')
async def internal_card_save(request: Request, _: None = Depends(require_internal_api_key)):
    db=connect()
    try:return save_card(db,await request.json())
    finally:db.close()

@app.post('/api/internal/order-plans/{plan_id}/evaluate')
async def internal_order_evaluate(plan_id: int, request: Request, _: None = Depends(require_internal_api_key)):
    """Paper-only tick evaluator; it neither schedules nor has a live adapter."""
    db=connect()
    try:return evaluate_order_plan(db,plan_id,await request.json())
    finally:db.close()

@app.post('/api/internal/scheduler-runs/{run_key}/start')
async def scheduler_start(run_key: str, request: Request, _: None = Depends(require_internal_api_key)):
    data=await request.json(); db=connect()
    try:
        existing=db.execute("SELECT kind,status FROM scheduler_runs WHERE run_key=?",(run_key,)).fetchone()
        if existing:
            if existing['kind'] != data['kind']: raise HTTPException(409,'run_key kind conflict')
            return {'run_key':run_key,'kind':existing['kind'],'status':existing['status'],'idempotent':True}
        db.execute("INSERT INTO scheduler_runs(run_key,kind,status,started_at,detail) VALUES(?,?, 'started',?,?)",(run_key,data['kind'],__import__('kr_stock_autotrader.decision_cards',fromlist=['now']).now(),__import__('json').dumps(data)))
        db.commit(); return {'run_key':run_key,'kind':data['kind'],'status':'started','idempotent':False}
    finally: db.close()

@app.post('/api/internal/scheduler-runs/{run_key}/finish')
async def scheduler_finish(run_key: str, request: Request, _: None = Depends(require_internal_api_key)):
    data=await request.json(); db=connect()
    try:
        if not db.execute("SELECT 1 FROM scheduler_runs WHERE run_key=?",(run_key,)).fetchone(): raise HTTPException(404,'scheduler run not found')
        db.execute("UPDATE scheduler_runs SET status=?,finished_at=?,detail=? WHERE run_key=?",(data['status'],__import__('kr_stock_autotrader.decision_cards',fromlist=['now']).now(),__import__('json').dumps(data),run_key)); db.commit(); return {'run_key':run_key,'status':data['status'],'count':data.get('count',0),'detail':data.get('detail',{})}
    finally: db.close()

@app.get('/api/internal/scheduler-runs/latest')
def scheduler_latest(kind: str, date: str | None = None, _: None = Depends(require_internal_api_key)):
    db=connect()
    try:
        query="SELECT * FROM scheduler_runs WHERE kind=?"; params=[kind]
        if date:
            query+=" AND (substr(started_at,1,10)=? OR run_key LIKE ?)"; params.extend([date,f"%{date}%"])
        item=db.execute(query+" ORDER BY id DESC LIMIT 1",params).fetchone()
        if not item: raise HTTPException(404,'scheduler run not found')
        result=dict(item); result['detail']=__import__('json').loads(result['detail']); return result
    finally: db.close()

@app.get('/api/internal/cards')
def internal_cards(missing: bool = False, _: None = Depends(require_internal_api_key)):
    db=connect()
    try:return list_cards(db, missing=missing)
    finally:db.close()

@app.get('/api/internal/cards/{card_id}')
def internal_card_detail(card_id: int, _: None = Depends(require_internal_api_key)):
    db=connect()
    try:return card_detail(db,card_id)
    finally:db.close()

def _business_date(date: str | None) -> str:
    """Validate the only accepted user-facing day form (KST evidence day)."""
    if date is None:
        return now_kst().date().isoformat()
    if not isinstance(date, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
        raise HTTPException(422, "기준일은 YYYY-MM-DD 형식입니다")
    try:
        return datetime.strptime(date, "%Y-%m-%d").date().isoformat()
    except ValueError:
        raise HTTPException(422, "기준일은 YYYY-MM-DD 형식입니다")


@ app.get('/api/cards/summary')
def user_cards_summary(request: Request, date: str | None = None):
    """Current-user metrics, grouped by material_evidence.known_at KST day.

    Dashboard card/filter counts use one current item: latest active card per
    lineage and latest filter per active evidence. Detail endpoints retain all
    append-only versions.
    """
    uid = current_user(request)
    current = now_kst()
    day = _business_date(date)
    def next_run(hour):
        candidate = datetime.combine(current.date(), time(hour), tzinfo=current.tzinfo)
        if candidate <= current: candidate += timedelta(days=1)
        return candidate.isoformat()
    db=connect()
    try:
        evidence_count=db.execute("SELECT count(*) n FROM material_evidence WHERE substr(known_at,1,10)=?",(day,)).fetchone()["n"]
        active_cards = """SELECT c.* FROM decision_cards c JOIN material_evidence e ON e.id=c.evidence_id
          WHERE substr(e.known_at,1,10)=? AND c.invalidated_at IS NULL
          AND NOT EXISTS (SELECT 1 FROM decision_cards newer WHERE newer.lineage_key=c.lineage_key AND newer.version>c.version AND newer.invalidated_at IS NULL)"""
        cards=db.execute("SELECT verdict,count(*) n FROM (" + active_cards + ") GROUP BY verdict",(day,)).fetchall()
        by_verdict={x["verdict"]:x["n"] for x in cards}
        filters=db.execute("""SELECT f.verdict,count(*) n FROM deterministic_filter_results f
          JOIN material_evidence e ON e.id=f.evidence_id WHERE substr(e.known_at,1,10)=?
          AND e.status != 'invalidated' AND f.id=(SELECT MAX(f2.id) FROM deterministic_filter_results f2 WHERE f2.evidence_id=f.evidence_id)
          GROUP BY f.verdict""",(day,)).fetchall()
        by_filter={x["verdict"]:x["n"] for x in filters}
        decisions=db.execute("SELECT d.decision,count(*) n FROM user_decisions d JOIN (" + active_cards + ") c ON c.id=d.card_id WHERE d.user_id=? GROUP BY d.decision",(day,uid)).fetchall()
        by_decision={x["decision"]:x["n"] for x in decisions}
        missing=db.execute("SELECT count(*) n FROM material_evidence e WHERE substr(e.known_at,1,10)=? AND e.status!='invalidated' AND NOT EXISTS (SELECT 1 FROM decision_cards c WHERE c.evidence_id=e.id)",(day,)).fetchone()["n"]
        statuses={name: db.execute("SELECT count(*) n FROM material_evidence WHERE substr(known_at,1,10)=? AND status=?",(day,name)).fetchone()["n"] for name in ("error","invalidated","decision_pending")}
        failures=db.execute("""SELECT count(*) n FROM material_evidence e WHERE substr(e.known_at,1,10)=?
          AND (e.status='error' OR (e.status!='invalidated' AND NOT EXISTS (SELECT 1 FROM decision_cards c WHERE c.evidence_id=e.id)))""", (day,)).fetchone()["n"]
        scheduler=[]
        for kind, label in (("research","리서치"),("card","카드")):
            run=db.execute("""SELECT status,started_at,finished_at,detail FROM scheduler_runs
              WHERE kind=? AND (run_key LIKE ? OR substr(started_at,1,10)=?)
              ORDER BY id DESC LIMIT 1""",(kind,f"{kind}-{day}-%",day)).fetchone()
            import json
            detail=json.loads(run["detail"] or "{}") if run else {}
            scheduler.append({"종류":label,"상태":run["status"] if run else "미실행","건수":int(detail.get("count",0) or 0),"실패":bool(run and run["status"] in {"error","failed"}),"시각":(run["finished_at"] or run["started_at"]) if run else None})
        return {"기준일":day,"전체 근거":evidence_count,"필터 PASS":by_filter.get("PASS",0),"필터 FAIL":by_filter.get("FAIL",0),"카드 생성":sum(by_verdict.values()),"카드 미생성":missing,"판단 보류":by_verdict.get("판단 보류",0),"매수 검토 가능":by_verdict.get("매수 검토 가능",0),"관찰":by_verdict.get("관찰",0),"제외":by_verdict.get("제외",0),"승인":by_decision.get("approve",0),"보류":by_decision.get("hold",0),"거절":by_decision.get("reject",0),"오류":statuses["error"],"무효화":statuses["invalidated"],"판단 대기":statuses["decision_pending"],"실패·근거 부족":failures,"최근 실행":{"07:00":scheduler[0],"08:00":scheduler[1]},"스케줄러":scheduler,"다음 실행":{"07:00 KST":next_run(7),"08:00 KST":next_run(8)}}
    finally: db.close()


@app.get('/api/cards')
def user_cards(request: Request, date: str | None = None):
    uid=current_user(request); day=_business_date(date); db=connect()
    try:return [user_card_view(db, item["id"], uid) for item in list_cards(db, date=day)]
    finally:db.close()


@app.get('/api/cards/missing')
def user_missing_cards(request: Request, date: str | None = None):
    """Authenticated, server-filtered actionable evidence rows only."""
    current_user(request); day=_business_date(date); db=connect()
    try:return list_cards(db, missing=True, date=day)
    finally:db.close()

@app.get('/api/cards/{card_id}')
def user_card(card_id: int, request: Request):
    uid=current_user(request); db=connect()
    try:return user_card_view(db,card_id,uid)
    finally:db.close()

@app.post('/api/cards/{card_id}/decisions')
async def user_card_decision(card_id: int, request: Request):
    csrf_origin_ok(request); uid=current_user(request); data=await request.json()
    if data.get('decision') not in {'approve','hold','reject'}: raise HTTPException(422,'invalid decision')
    db=connect()
    try:return user_decision(db,card_id,uid,data['decision'],data.get('note',''))
    finally:db.close()

@app.post('/api/cards/{card_id}/order-plan-draft')
async def user_order_plan_draft(card_id: int, request: Request):
    csrf_origin_ok(request); uid=current_user(request); data=await request.json(); db=connect()
    try: return edit_draft(db,card_id,uid,data)
    finally: db.close()

@app.post('/api/order-plans/{plan_id}/edit')
async def user_order_plan_edit(plan_id: int, request: Request):
    """Any edit is authenticated and revokes the immutable approved snapshot."""
    csrf_origin_ok(request); uid=current_user(request); data=await request.json()
    db=connect()
    try:
        return edit_order_plan(db, plan_id, uid, data)
    finally: db.close()

@app.get('/api/order-plans/{plan_id}')
def user_order_plan_detail(plan_id: int, request: Request):
    uid=current_user(request); db=connect()
    try:
        plan=db.execute("SELECT * FROM order_plans WHERE id=? AND user_id=?",(plan_id,uid)).fetchone()
        if not plan: raise HTTPException(404,'order plan not found')
        out=dict(plan)
        draft=db.execute("SELECT snapshot_json,status,updated_at FROM order_plan_drafts WHERE card_id=? AND user_id=? AND status='draft'",(plan['card_id'],uid)).fetchone()
        out['draft']={**dict(draft), 'snapshot':__import__('json').loads(draft['snapshot_json'])} if draft else None
        out['events']=[dict(x) for x in db.execute("SELECT event,reason,at FROM order_events WHERE order_plan_id=? ORDER BY id",(plan_id,))]
        out['position']=dict(db.execute("SELECT qty,avg_price,status FROM positions WHERE order_plan_id=?",(plan_id,)).fetchone() or {})
        return out
    finally: db.close()

@app.post('/api/order-plans/{plan_id}/close')
async def user_order_plan_close(plan_id: int, request: Request):
    """An authenticated explicit event may close only an already-open paper position."""
    csrf_origin_ok(request); uid=current_user(request); data=await request.json(); db=connect()
    try:
        plan=db.execute("SELECT user_id FROM order_plans WHERE id=?",(plan_id,)).fetchone()
        if not plan or plan['user_id'] != uid: raise HTTPException(404,'order plan not found')
        data['manual_exit']=True
        return evaluate_order_plan(db,plan_id,data)
    finally: db.close()

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
