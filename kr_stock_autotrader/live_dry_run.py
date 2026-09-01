"""Pure, receipt-only live dry-run evaluation.  It never imports a broker."""
from __future__ import annotations
import json
from datetime import datetime
from .domain import fresh_quote, market_open, now_kst, parse_kst

BROKER_MODE = "read_only_dry_run"

def _reason(code: str) -> str:
    return {"plan_not_approved":"승인된 현재 계획이 아닙니다", "card_invalidated":"결정 카드가 무효화되었습니다", "expired":"계획 유효기간이 지났습니다", "stale_or_future_quote":"시세 시간이 현재와 맞지 않거나 오래되었습니다", "market_or_window_closed":"장중 또는 주문 가능 시간이 아닙니다", "price_cap":"현재가가 가격 상한을 넘었습니다", "remaining_limit":"남은 수량 또는 금액 한도가 없습니다", "quote_unavailable":"안전한 현재가를 받지 못했습니다", "symbol_mismatch":"계획 종목과 시세 종목이 다릅니다", "trusted_conflicting_disclosure":"신뢰된 공시 충돌이 있습니다"}.get(code, "사전점검 조건을 확인할 수 없습니다")

def evaluate_live_dry_run(plan: dict, card_invalidated: bool, quote: dict, *, server_now: datetime | None = None, trusted_conflicting_disclosure: bool = False) -> dict:
    """Return WOULD_* and Korean reasons without changing plan, paper, or broker state."""
    now = server_now or now_kst(); codes=[]
    if plan.get("status") != "approved": codes.append("plan_not_approved")
    if card_invalidated: codes.append("card_invalidated")
    try:
        if now > parse_kst(plan["valid_until"]) or now > parse_kst(plan["expires_at"]): codes.append("expired")
    except (KeyError, TypeError, ValueError): codes.append("expired")
    try:
        if quote.get("status") != "ok" or quote.get("symbol") != plan["symbol"]: raise ValueError
        known=parse_kst(quote["quote_known_at"]); price=float(quote["price"])
        if not fresh_quote(type("Q", (), {"known_at": known})(), now): codes.append("stale_or_future_quote")
        if price <= 0: raise ValueError
    except (KeyError, TypeError, ValueError):
        codes.append("quote_unavailable"); known=None; price=None
    if quote.get("status") == "ok" and quote.get("symbol") != plan.get("symbol"): codes.append("symbol_mismatch")
    if not codes and (not market_open(now) or now < parse_kst(plan["window_start"]) or now > parse_kst(plan["window_end"])): codes.append("market_or_window_closed")
    if not codes and price > float(plan["price_cap"]): codes.append("price_cap")
    remaining_qty=int(plan["max_qty"])-int(plan.get("bought_qty",0))
    remaining_amount=float(plan["max_amount"])-float(plan.get("bought_amount",0))
    if not codes and (remaining_qty <= 0 or remaining_amount < price): codes.append("remaining_limit")
    if trusted_conflicting_disclosure: codes.append("trusted_conflicting_disclosure")
    # Time/window issues wait; invariant failures reject. No implied order is ever made.
    result = "WOULD_WAIT" if codes and set(codes) <= {"market_or_window_closed", "stale_or_future_quote", "quote_unavailable"} else ("WOULD_REJECT" if codes else "WOULD_SUBMIT")
    return {"result":result,"reasons":[_reason(c) for c in codes] or ["모든 사전점검 조건을 통과했습니다"],"reason_codes":codes,"plan_id":plan["id"],"card_id":plan["card_id"],"card_version":plan["card_version"],"plan_version":plan["version_hash"],"quote_price":price,"quote_known_at":known.isoformat() if known else None,"quote_retrieved_at":quote.get("retrieved_at"),"at":now.isoformat(),"broker_mode":BROKER_MODE,"network_order_calls":0}

def persist_live_dry_run(db, plan: dict, user_id: int, dry_run_key: str, quote: dict, *, server_now=None, trusted_conflicting_disclosure=False) -> dict:
    existing=db.execute("SELECT * FROM live_dry_run_receipts WHERE order_plan_id=? AND user_id=? AND dry_run_key=?",(plan["id"],user_id,dry_run_key)).fetchone()
    if existing:
        result=dict(existing); result["reasons"]=json.loads(result.pop("reasons_json")); result["idempotent"]=True; return result
    card=db.execute("SELECT invalidated_at FROM decision_cards WHERE id=?",(plan["card_id"],)).fetchone()
    result=evaluate_live_dry_run(plan, bool(card and card["invalidated_at"]), quote, server_now=server_now, trusted_conflicting_disclosure=trusted_conflicting_disclosure)
    db.execute("INSERT INTO live_dry_run_receipts(order_plan_id,user_id,dry_run_key,result,reasons_json,plan_version,card_id,card_version,quote_price,quote_known_at,quote_retrieved_at,evaluated_at,broker_mode,network_order_calls) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",(plan["id"],user_id,dry_run_key,result["result"],json.dumps(result["reasons"],ensure_ascii=False),result["plan_version"],result["card_id"],result["card_version"],result["quote_price"],result["quote_known_at"],result["quote_retrieved_at"],result["at"],BROKER_MODE,0))
    db.commit(); result["dry_run_key"]=dry_run_key; result["idempotent"]=False; return result
