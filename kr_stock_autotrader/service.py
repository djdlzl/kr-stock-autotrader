"""Idempotent, user-isolated quote evaluation service."""
import sqlite3
from datetime import datetime
from .broker import PaperBroker
from .domain import Condition, Quote, evaluate_conditions, fresh_quote, market_open, now_kst, parse_kst


def audit(db, plan_id: int, event: str, reason: str = "", key: str | None = None) -> None:
    db.execute("INSERT OR IGNORE INTO events(plan_id,tick_key,event,reason,at) VALUES(?,?,?,?,?)", (plan_id, key, event, reason, now_kst().isoformat()))


def evaluate_tick(db: sqlite3.Connection, user_id: int, quote: Quote, tick_key: str, broker: PaperBroker | None = None) -> None:
    broker = broker or PaperBroker()
    server_now = now_kst()
    # The submitting user's plans only: a quote is never cross-tenant input.
    plans = db.execute("SELECT * FROM plans WHERE user_id=? AND symbol=? AND status IN ('scheduled','filled')", (user_id, quote.symbol)).fetchall()
    for plan in plans:
        key = f"{plan['id']}:{tick_key}"
        if db.execute("SELECT 1 FROM events WHERE tick_key=?", (key,)).fetchone():
            continue
        if not fresh_quote(quote, server_now):
            audit(db, plan["id"], "quote_rejected_stale_or_future", "known_at must be 0–5 minutes old", key)
            continue
        if plan["status"] == "scheduled":
            due = quote.known_at >= parse_kst(plan["scheduled_at"])
            if not (due and market_open(quote.known_at)):
                audit(db, plan["id"], "buy_not_due_or_market_closed", "", key)
                continue
            if plan["order_type"] == "limit" and quote.price > plan["limit_price"]:
                audit(db, plan["id"], "buy_wait_limit", "quote exceeds limit", key)
                continue
            broker.fill(db, plan["id"], "buy", plan["qty"], quote.price)
            db.execute("UPDATE plans SET status='filled',buy_price=?,last_evaluation=? WHERE id=?", (quote.price, quote.known_at.isoformat(), plan["id"]))
            audit(db, plan["id"], "paper_buy_filled", f"{plan['qty']}@{quote.price}", key)
        current = db.execute("SELECT * FROM plans WHERE id=? AND user_id=?", (plan["id"], user_id)).fetchone()
        if current["status"] != "filled":
            continue
        conditions = [Condition(row["kind"], row["operator"], row["value"]) for row in db.execute("SELECT kind,operator,value FROM conditions WHERE plan_id=?", (plan["id"],))]
        hit, reasons = evaluate_conditions(conditions, current["combine_mode"], quote, current["buy_price"], server_now)
        db.execute("UPDATE plans SET last_evaluation=? WHERE id=?", (server_now.isoformat(), plan["id"]))
        remaining = current["qty"] - current["sold_qty"]
        if hit and remaining > 0:
            broker.fill(db, plan["id"], "sell", remaining, quote.price)
            db.execute("UPDATE plans SET status='closed', sold_qty=sold_qty+? WHERE id=?", (remaining, plan["id"]))
            audit(db, plan["id"], "paper_sell_filled", "; ".join(reasons), key + ":sell")
        else:
            audit(db, plan["id"], "condition_not_met", "자동매도 조건 없음 또는 미충족", key + ":eval")
