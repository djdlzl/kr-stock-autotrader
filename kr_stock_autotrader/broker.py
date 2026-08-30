"""Paper-only broker port. No live implementation exists."""
from typing import Protocol
import sqlite3
from .domain import now_kst


class BrokerPort(Protocol):
    def fill(self, db: sqlite3.Connection, plan_id: int, side: str, qty: int, price: float) -> None: ...


class PaperBroker:
    """Records fully filled paper orders; fee, tax, and slippage are deliberately 0."""
    def fill(self, db: sqlite3.Connection, plan_id: int, side: str, qty: int, price: float) -> None:
        if side not in {"buy", "sell"} or qty <= 0 or price <= 0:
            raise ValueError("invalid paper fill")
        db.execute(
            "INSERT INTO fills(plan_id,side,qty,price,filled_at,fee,tax,slippage) VALUES(?,?,?,?,?,?,?,?)",
            (plan_id, side, qty, price, now_kst().isoformat(), 0, 0, 0),
        )
