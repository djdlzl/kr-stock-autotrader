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
from .decision_cards import (require_internal_api_key, create_evidence, list_evidence, evidence_detail, mutate_evidence, save_filter, filter_detail, current_filter_head, save_card, list_cards, card_detail, user_card_view, user_decision, evaluate_order_plan, edit_order_plan, edit_draft)
from .service import audit, evaluate_tick
from .ui import APP_HTML, AUTH_HTML, PROTOTYPE_HTML
from .kis_readonly import KISReadOnlyClient
from .market_data import build_premarket_snapshot, filter_inputs_from_snapshot
from .live_dry_run import existing_live_dry_run_receipt, persist_live_dry_run
from .event_scenarios import create as create_scenario_set, detail as scenario_set_detail, observe as observe_scenario


# Public prototype-only source pages.  These deliberately have no connection to
# accounts, orders, APIs, or production data; the allowlist makes the mock scope explicit.
MOCK_SOURCES = {
    "hanbit-disclosure": ("한빛반도체 공시 목업", "납기 범위는 4분기까지입니다."),
    "monobio-clinical": ("모노바이오 임상 발표 목업", "후속 결과는 추후 공개합니다."),
    "donghae-official": ("동해전기 공식 자료 목업", "양산은 10월에 시작합니다."),
    "donghae-press": ("동해전기 보도자료 목업", "양산은 11월에 시작합니다."),
}


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


def _cache_get_validated(key: tuple[int, str], symbol: str, now: float) -> dict | None:
    """Return a safe cached quote, evicting a malformed hit while holding the lock.

    A non-``None`` result always represents a cache hit: valid hits project to
    ``ok`` and tampered hits project to the closed unavailable schema.  The
    latter must not fall through to the provider in this request.
    """
    with _quote_cache_lock:
        _purge_quote_cache(now)
        cached = _quote_cache.get(key)
        if cached is None:
            return None
        safe = _safe_kis_quote(symbol, dict(cached[1]))
        if safe["status"] != "ok":
            # The entry cannot be replaced between validation and this delete:
            # both operations are protected by the same cache lock.
            del _quote_cache[key]
        return safe


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

def _safe_kis_orderbook(symbol: str, outcome: object) -> dict:
    """Closed KIS top-of-book projection; timestamps are retrieval, not exchange time."""
    unavailable = {"symbol": symbol, "source": "KIS", "environment": "production", "status": "unavailable", "market_context_status": "UNAVAILABLE"}
    if not isinstance(outcome, dict) or outcome.get("status") != "ok" or outcome.get("symbol") != symbol:
        return unavailable
    retrieved_at = _valid_kis_retrieved_at(outcome.get("retrieved_at")); quote_known_at = _valid_kis_retrieved_at(outcome.get("quote_known_at"))
    values = [_valid_kis_number(outcome.get(key), minimum=0.0) for key in ("last_price", "best_bid", "best_ask", "top_bid_qty", "top_ask_qty")]
    if (not isinstance(symbol, str) or re.fullmatch(r"\d{6}", symbol) is None or not retrieved_at or not quote_known_at or outcome.get("timestamp_source") != "network_retrieved_at" or not _kis_timestamp_pair_is_current(retrieved_at, quote_known_at) or any(value is None or value <= 0 for value in values)):
        return unavailable
    last, bid, ask, bid_qty, ask_qty = values
    if bid > ask or bid_qty + ask_qty <= 0: return unavailable
    return {"symbol":symbol, "last_price":last, "best_bid":bid, "best_ask":ask, "top_bid_qty":bid_qty, "top_ask_qty":ask_qty, "spread_pct":(ask-bid)/last*100, "imbalance":(bid_qty-ask_qty)/(bid_qty+ask_qty), "quote_known_at":quote_known_at, "retrieved_at":retrieved_at, "timestamp_source":"network_retrieved_at", "source":"KIS", "environment":"production", "market_context_status":"UNAVAILABLE", "status":"ok"}


def _tracking_health(quote: dict, result: dict, *, invalidated: bool = False) -> dict:
    """Closed, defensive projection separating quote transport from decision data."""
    attempted = now_kst()
    required = ("retrieved_at", "quote_known_at", "source", "timestamp_source", "last_price", "best_bid", "best_ask", "top_bid_qty", "top_ask_qty", "spread_pct", "imbalance", "market_context_status")
    safe = isinstance(quote, dict) and all(key in quote for key in required)
    try:
        age = max(0, int((attempted - parse_kst(quote["retrieved_at"])).total_seconds())) if safe else None
    except (TypeError, ValueError, OverflowError):
        safe, age = False, None
    context = quote.get("market_context_status") if isinstance(quote, dict) else None
    quote_health = "HEALTHY" if safe and age is not None else "UNAVAILABLE"
    context_ready = quote_health == "HEALTHY" and context == "VERIFIED" and not invalidated
    context_readiness = "READY" if context_ready else "BLOCKED"
    labels = {"PASS": "통과", "BLOCKED": "차단", "NOT_EVALUATED": "평가 안 함", "OBSERVED": "관측만", "OUT_OF_RANGE": "차단", "EVALUATED": "통과"}
    gate_specs = [
        ("quote_validity_freshness", "호가 유효성", "PASS" if quote_health == "HEALTHY" else "BLOCKED", "timestamp_symbol_source_prerequisite"),
        ("market_context", "시장 자료", "PASS" if context_ready else "BLOCKED", "scenario_selection_prerequisite"),
        ("price_range", "가격 범위", "NOT_EVALUATED" if not context_ready else ("PASS" if result.get("active_scenario_label") else "OUT_OF_RANGE"), "selects_label_after_verified_context"),
        ("market_sector_volume", "시장·섹터·거래량", "NOT_EVALUATED" if not context_ready else "EVALUATED", "changes_good_action_strength_only"),
        ("spread_imbalance", "스프레드·불균형", "OBSERVED", "integrity_context_not_label_selector"),
        ("business_invalidation", "사업 무효화", "BLOCKED" if invalidated else "PASS", "authoritative_exit_override"),
    ]
    gates = [{"gate": gate, "label": label, "status": status, "display_status": labels[status], "role": role} for gate, label, status, role in gate_specs]
    status = "TRACKING_STOPPED" if invalidated else ("QUOTE_UNAVAILABLE" if quote_health != "HEALTHY" else ("HEALTHY" if context_ready else "QUOTE_OK_CONTEXT_MISSING"))
    return {
        "quote_health": quote_health, "context_readiness": context_readiness, "status": status,
        "last_attempted_at": attempted.isoformat(), "last_success_at": quote.get("retrieved_at") if safe else None,
        "quote_age_seconds": age, "freshness": "FRESH" if quote_health == "HEALTHY" else "UNAVAILABLE",
        "source": quote.get("source") if safe else None, "timestamp_source": quote.get("timestamp_source") if safe else None,
        "quote_known_at": quote.get("quote_known_at") if safe else None, "price_krw": quote.get("last_price") if safe else None,
        "best_bid": quote.get("best_bid") if safe else None, "best_ask": quote.get("best_ask") if safe else None,
        "top_bid_qty": quote.get("top_bid_qty") if safe else None, "top_ask_qty": quote.get("top_ask_qty") if safe else None,
        "spread_pct": quote.get("spread_pct") if safe else None, "imbalance": quote.get("imbalance") if safe else None,
        "market_context_status": context if safe else "UNAVAILABLE", "match": result.get("match", "UNOBSERVED"),
        "action": result.get("action", "NO_ACTION"), "active_scenario_label": result.get("active_scenario_label"), "gate_results": gates,
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
    cached = _cache_get_validated(key, symbol, monotonic_time.monotonic())
    if cached is not None:
        return cached
    # The provider runs outside cache metadata guards.  The keyed lock makes a
    # simultaneous miss single-flight, then every waiter consumes one projection.
    with _registered_lock(_quote_locks, _quote_locks_guard, key):
        cached = _cache_get_validated(key, symbol, monotonic_time.monotonic())
        if cached is not None:
            return cached
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

@app.get("/api/kis/orderbook/{symbol}")
def kis_orderbook(symbol: str, request: Request):
    current_user(request)
    if not re.fullmatch(r"\d{6}", symbol): raise HTTPException(422, "종목코드는 6자리 숫자입니다")
    provider = getattr(app.state, "kis_orderbook_provider", None)
    if provider is None:
        global _default_kis_client
        if _default_kis_client is None: _default_kis_client = KISReadOnlyClient()
        provider = _default_kis_client.orderbook
    try: outcome = provider(symbol)
    except Exception: outcome = None
    return _safe_kis_orderbook(symbol, outcome)


def _kis_orderbook_provider():
    provider = getattr(app.state, "kis_orderbook_provider", None)
    if provider is None:
        global _default_kis_client
        if _default_kis_client is None:
            _default_kis_client = KISReadOnlyClient()
        provider = _default_kis_client.orderbook
    return provider


def _card_tracking_scenario(db, card_id: int):
    """Resolve only server-owned active card/scenario identity for polling."""
    scenario = db.execute("SELECT * FROM event_scenario_sets WHERE card_id=? ORDER BY version DESC,id DESC LIMIT 1", (card_id,)).fetchone()
    card = db.execute("SELECT e.symbol,c.invalidated_at FROM decision_cards c JOIN material_evidence e ON e.id=c.evidence_id WHERE c.id=?", (card_id,)).fetchone()
    if not scenario or not card or card["invalidated_at"] or scenario["symbol"] != card["symbol"]:
        raise HTTPException(409, {"code": "TRACKING_STOPPED", "message": "추적할 현재 시나리오가 없습니다"})
    return scenario


@app.get("/api/cards/{card_id}/tracking-health")
def card_tracking_health(card_id: int, request: Request):
    """Authenticated read-only KIS health projection; never observes or persists."""
    current_user(request)
    db = connect()
    try:
        scenario = _card_tracking_scenario(db, card_id)
        try:
            outcome = _kis_orderbook_provider()(scenario["symbol"])
        except Exception:
            outcome = None
        quote = _safe_kis_orderbook(scenario["symbol"], outcome)
        if quote.get("status") != "ok":
            raise HTTPException(503, {"code": "QUOTE_UNAVAILABLE", "message": "호가를 안전하게 확인하지 못했습니다"})
        # KIS top-of-book alone is never sufficient to select a scenario.
        result = {"match": "OUT_OF_RANGE", "action": "NO_ACTION", "active_scenario_label": None}
        return {"tracking_health": _tracking_health(quote, result)}
    finally:
        db.close()


@app.post("/api/cards/{card_id}/scenario-observations")
async def card_scenario_observation(card_id: int, request: Request):
    """Explicit compatibility observation route; UI polling must use GET health."""
    csrf_origin_ok(request); current_user(request)
    db = connect()
    try:
        scenario = db.execute("SELECT * FROM event_scenario_sets WHERE card_id=? ORDER BY version DESC,id DESC LIMIT 1", (card_id,)).fetchone()
        card = db.execute("SELECT e.symbol,c.invalidated_at FROM decision_cards c JOIN material_evidence e ON e.id=c.evidence_id WHERE c.id=?", (card_id,)).fetchone()
        if not scenario or not card or card["invalidated_at"] or scenario["symbol"] != card["symbol"]: raise HTTPException(409, "active scenario unavailable")
        try: body = await request.json()
        except ValueError: raise HTTPException(422, "invalid observation body")
        if not isinstance(body, dict): raise HTTPException(422, "invalid observation body")
        forbidden = {"symbol", "scenario", "match", "action", "active_scenario_label", "trusted_business_invalidation", "price", "price_krw", "best_bid", "best_ask", "top_bid_qty", "top_ask_qty", "spread_pct", "imbalance", "provider", "source", "source_receipt", "known_at", "quote_known_at", "retrieved_at", "timestamp_source", "benchmark_excess_pct", "sector_excess_pct", "volume_ratio"}
        if forbidden.intersection(body): raise HTTPException(422, "server-owned observation fields")
        provider = getattr(app.state, "kis_orderbook_provider", None)
        if provider is None:
            global _default_kis_client
            if _default_kis_client is None: _default_kis_client = KISReadOnlyClient()
            provider = _default_kis_client.orderbook
        try: outcome = provider(scenario["symbol"])
        except Exception: outcome = None
        quote = _safe_kis_orderbook(scenario["symbol"], outcome)
        if quote.get("status") != "ok":
            raise HTTPException(503, {"code": "QUOTE_UNAVAILABLE", "message": "호가를 안전하게 확인하지 못했습니다"})
        observation = {
            "provider": "KIS", "source": "KIS", "source_receipt": f"KIS:{quote['quote_known_at']}",
            "symbol": scenario["symbol"], "known_at": quote["quote_known_at"], "retrieved_at": quote["retrieved_at"],
            "price_krw": quote["last_price"], "best_bid": quote["best_bid"], "best_ask": quote["best_ask"],
            "top_bid_qty": quote["top_bid_qty"], "top_ask_qty": quote["top_ask_qty"],
            "idempotency_key": body.get("idempotency_key", quote["quote_known_at"]),
            "volume_ratio": 1.0, "benchmark_excess_pct": 0.0, "sector_excess_pct": 0.0,
            "market_context_status": "UNAVAILABLE",
        }
        result = observe_scenario(db, scenario["event_identity"], observation)
        return {"tracking_health": _tracking_health(quote, result), "observation": result}
    finally: db.close()

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


@app.post('/api/internal/scenario-sets')
async def internal_scenario_set_create(request: Request, _: None = Depends(require_internal_api_key)):
    """Freeze a scenario set; conditional sets never create trading artifacts."""
    data = await request.json(); db=connect()
    try:
        if isinstance(data, dict) and data.get("kind") == "CONDITIONAL":
            return __import__("kr_stock_autotrader.conditional_scenarios", fromlist=["create"]).create(db, data)
        return create_scenario_set(db, data)
    finally: db.close()

@app.get('/api/internal/scenario-sets/{identity}')
def internal_scenario_set_read(identity: str, _: None = Depends(require_internal_api_key)):
    db=connect()
    try: return __import__("kr_stock_autotrader.event_scenarios", fromlist=["detail_by_event_identity"]).detail_by_event_identity(db, identity)
    finally: db.close()

@app.get('/api/internal/scenario-sets')
def internal_scenario_set_list(_: None = Depends(require_internal_api_key)):
    db=connect()
    try: return [scenario_set_detail(db, row['id']) for row in db.execute('SELECT id FROM event_scenario_sets ORDER BY id DESC')]
    finally: db.close()

@app.post('/api/internal/scenario-sets/{identity}/observations')
async def internal_scenario_observation_append(identity: str, request: Request, _: None = Depends(require_internal_api_key)):
    db=connect()
    try: return observe_scenario(db, identity, await request.json())
    finally: db.close()

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

@app.post('/api/internal/market-snapshots/{symbol}')
async def internal_market_snapshot(symbol: str, request: Request, _: None = Depends(require_internal_api_key)):
    """Scheduler-only pre-market snapshot; returns safe data plus ready filter inputs."""
    try:
        data = await request.json()
        as_of = parse_kst(data["as_of"])
    except (KeyError, TypeError, ValueError):
        raise HTTPException(422, "as_of must be KST ISO-8601")
    provider = getattr(app.state, "kis_daily_snapshot_provider", None)
    if provider is None:
        global _default_kis_client
        if _default_kis_client is None:
            _default_kis_client = KISReadOnlyClient()
        provider = _default_kis_client.daily_snapshot
    announcement_at = data.get("announcement_at") if isinstance(data.get("announcement_at"), str) else None
    snapshot = build_premarket_snapshot(symbol, as_of, provider, announcement_at)
    return {'snapshot': snapshot, 'filter_inputs': filter_inputs_from_snapshot(snapshot)}


@app.post('/api/internal/filters')
async def internal_filter(request: Request, _: None = Depends(require_internal_api_key)):
    data=await request.json(); db=connect()
    try:return save_filter(db,data['evidence_id'],data['inputs'],data['as_of'],data['known_at'],data.get('parent_filter_id'))
    finally:db.close()

@app.get('/api/internal/filters/head')
def internal_filter_head(evidence_id: int, as_of: str, known_at: str, _: None = Depends(require_internal_api_key)):
    db=connect()
    try:return current_filter_head(db, evidence_id, as_of, known_at)
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

def _business_date(value: str | None) -> str:
    """Validate a KST calendar day without assigning a timestamp axis."""
    if value is None:
        return now_kst().date().isoformat()
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise HTTPException(422, "기준일은 YYYY-MM-DD 형식입니다")
    try:
        return datetime.strptime(value, "%Y-%m-%d").date().isoformat()
    except ValueError:
        raise HTTPException(422, "기준일은 YYYY-MM-DD 형식입니다")


def _date_axis(date: str | None, operation_date: str | None) -> tuple[str, bool]:
    """Legacy `date` remains known_at; operation_date selects operational axes."""
    if date is not None and operation_date is not None:
        raise HTTPException(422, "date and operation_date cannot be combined")
    return _business_date(operation_date if operation_date is not None else date), operation_date is not None


@app.get('/api/cards/summary')
def user_cards_summary(request: Request, date: str | None = None, operation_date: str | None = None):
    """Summarize legacy known_at history or explicit operation timestamp axes."""
    uid = current_user(request)
    day, operational = _date_axis(date, operation_date)
    current = now_kst()
    def next_run(hour):
        candidate = datetime.combine(current.date(), time(hour), tzinfo=current.tzinfo)
        if candidate <= current: candidate += timedelta(days=1)
        return candidate.isoformat()
    evidence_axis = 'collected_at' if operational else 'known_at'
    card_axis = 'c.generated_at' if operational else 'e.known_at'
    filter_axis = 'f.created_at' if operational else 'e.known_at'
    db=connect()
    try:
        evidence_count=db.execute(f"SELECT count(*) n FROM material_evidence WHERE date({evidence_axis},'+9 hours')=?",(day,)).fetchone()['n']
        if operational:
            # Operation date deliberately reports immutable processing history.
            selected_cards = "SELECT c.* FROM decision_cards c WHERE date(c.generated_at,'+9 hours')=?"
            filters=db.execute("SELECT f.verdict,count(*) n FROM deterministic_filter_results f WHERE date(f.created_at,'+9 hours')=? GROUP BY f.verdict",(day,)).fetchall()
        else:
            selected_cards = f"""SELECT c.* FROM decision_cards c JOIN material_evidence e ON e.id=c.evidence_id
              WHERE date({card_axis},'+9 hours')=? AND c.invalidated_at IS NULL
              AND NOT EXISTS (SELECT 1 FROM decision_cards newer WHERE newer.lineage_key=c.lineage_key AND newer.version>c.version AND newer.invalidated_at IS NULL)"""
            filters=db.execute(f"""SELECT f.verdict,count(*) n FROM deterministic_filter_results f
              JOIN material_evidence e ON e.id=f.evidence_id WHERE date({filter_axis},'+9 hours')=?
              AND e.status != 'invalidated' AND f.evidence_version=e.version
              AND f.id=(SELECT head.id FROM deterministic_filter_results head
                WHERE head.evidence_id=f.evidence_id AND head.evidence_version=e.version
                  AND NOT EXISTS (SELECT 1 FROM deterministic_filter_results child WHERE child.parent_filter_id=head.id)
                 ORDER BY head.as_of DESC,head.known_at DESC,head.lineage_version DESC LIMIT 1)
              GROUP BY f.verdict""",(day,)).fetchall()
        cards=db.execute("SELECT verdict,count(*) n FROM (" + selected_cards + ") GROUP BY verdict",(day,)).fetchall()
        by_verdict={x['verdict']:x['n'] for x in cards}
        by_filter={x['verdict']:x['n'] for x in filters}
        decisions=db.execute("SELECT d.decision,count(*) n FROM (" + selected_cards + ") c JOIN user_decisions d ON d.card_id=c.id WHERE d.user_id=? GROUP BY d.decision",(day,uid)).fetchall()
        by_decision={x['decision']:x['n'] for x in decisions}
        missing=db.execute(f"SELECT count(*) n FROM material_evidence e WHERE date(e.{evidence_axis},'+9 hours')=? AND e.status!='invalidated' AND NOT EXISTS (SELECT 1 FROM decision_cards c WHERE c.evidence_id=e.id)",(day,)).fetchone()['n']
        statuses={name: db.execute(f"SELECT count(*) n FROM material_evidence WHERE date({evidence_axis},'+9 hours')=? AND status=?",(day,name)).fetchone()['n'] for name in ('error','invalidated','decision_pending')}
        failures=db.execute(f"""SELECT count(*) n FROM material_evidence e WHERE date(e.{evidence_axis},'+9 hours')=?
          AND (e.status='error' OR (e.status!='invalidated' AND NOT EXISTS (SELECT 1 FROM decision_cards c WHERE c.evidence_id=e.id)))""", (day,)).fetchone()['n']
        scheduler=[]
        for kind, label in (('research','리서치'),('card','카드')):
            run=db.execute("""SELECT status,started_at,finished_at,detail FROM scheduler_runs
              WHERE kind=? AND (run_key LIKE ? OR substr(started_at,1,10)=?) ORDER BY id DESC LIMIT 1""",(kind,f"{kind}-{day}-%",day)).fetchone()
            import json
            detail=json.loads(run['detail'] or '{}') if run else {}
            scheduler.append({'종류':label,'상태':run['status'] if run else '미실행','건수':int(detail.get('count',0) or 0),'실패':bool(run and run['status'] in {'error','failed'}),'시각':(run['finished_at'] or run['started_at']) if run else None})
        return {'기준일':day,'날짜 축':'운영 처리 이력' if operational else '근거 확인일','전체 근거':evidence_count,'필터 PASS':by_filter.get('PASS',0),'필터 FAIL':by_filter.get('FAIL',0),'카드 생성':sum(by_verdict.values()),'카드 미생성':missing,'판단 보류':by_verdict.get('판단 보류',0),'매수 검토 가능':by_verdict.get('매수 검토 가능',0),'관찰':by_verdict.get('관찰',0),'제외':by_verdict.get('제외',0),'승인':by_decision.get('approve',0),'보류':by_decision.get('hold',0),'거절':by_decision.get('reject',0),'오류':statuses['error'],'무효화':statuses['invalidated'],'판단 대기':statuses['decision_pending'],'실패·근거 부족':failures,'최근 실행':{'07:00':scheduler[0],'08:00':scheduler[1]},'스케줄러':scheduler,'다음 실행':{'07:00 KST':next_run(7),'08:00 KST':next_run(8)}}
    finally: db.close()


@app.get('/api/cards')
def user_cards(request: Request, date: str | None = None, operation_date: str | None = None):
    uid=current_user(request); day, operational=_date_axis(date, operation_date); db=connect()
    try:return [user_card_view(db, item['id'], uid) for item in list_cards(db, date=day if (date is not None or operation_date is not None) else None, current_only=date is None and operation_date is None, operation_date=operational)]
    finally:db.close()


@app.get('/api/cards/missing')
def user_missing_cards(request: Request, date: str | None = None, operation_date: str | None = None):
    """Authenticated evidence; operation date is collected_at."""
    current_user(request); day, operational=_date_axis(date, operation_date); db=connect()
    try:return list_cards(db, missing=True, date=day, operation_date=operational)
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


@app.get("/prototype", response_class=HTMLResponse)
def prototype():
    """Public, static Release 0 interaction mockup with no product data or APIs."""
    return HTMLResponse(PROTOTYPE_HTML)


@app.get("/prototype/mock-source/{source_id}", response_class=HTMLResponse)
def mock_source(source_id: str):
    """Serve only an allowlisted, honestly labelled prototype source document."""
    source = MOCK_SOURCES.get(source_id)
    if source is None:
        raise HTTPException(404, "mock source not found")
    name, quote = source
    return HTMLResponse(
        "<!doctype html><html lang='ko'><head><meta charset='utf-8'>"
        f"<title>{name} · 목업 원문</title></head><body>"
        "<p><strong>목업 원문 · 실제 자료 아님</strong></p>"
        f"<h1>{name}</h1><p>“{quote}”</p>"
        "<p>Release 0 상호작용 검증용 정적 목업이며 실제 출처, 투자 정보 또는 주문 데이터가 아닙니다.</p>"
        "</body></html>"
    )


@app.get("/app", response_class=HTMLResponse)
def application(request: Request):
    """Browser navigation redirects unauthenticated visitors to the auth gate."""
    try:
        current_user(request)
    except HTTPException:
        return RedirectResponse("/", status_code=303)
    return HTMLResponse(APP_HTML)
