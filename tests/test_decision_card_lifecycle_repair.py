"""Lifecycle invariants for immutable decision-card paper plans."""
import tempfile
import pytest
from kr_stock_autotrader import db as dbmod
from tests.test_decision_card_invariants import make_plan, tick, evidence, raw, card
from kr_stock_autotrader.decision_cards import evaluate_order_plan, edit_order_plan, edit_draft, user_decision, save_filter, save_card


@pytest.fixture
def db(monkeypatch):
    path = tempfile.mktemp(suffix=".db")
    monkeypatch.setattr(dbmod, "DATABASE_PATH", path)
    d = dbmod.connect()
    yield d
    d.close()


def test_preapproval_draft_is_validated_and_exactly_frozen_on_approval(db):
    e=evidence(db); f=save_filter(db,e['id'],raw(),"2026-08-31T10:00:00+09:00","2026-08-31T09:00:00+09:00")
    c=save_card(db,card(e['id'],f['id'])); db.execute("INSERT INTO users(email,password) VALUES('u','p')"); db.commit()
    assert edit_draft(db,c['id'],1,{"price_cap":91,"max_amount":180,"max_qty":2})["status"] == "draft"
    approved=user_decision(db,c['id'],1,"approve")
    frozen=db.execute("SELECT price_cap,max_amount,max_qty FROM order_plans WHERE id=?",(approved['order_plan_id'],)).fetchone()
    assert tuple(frozen)==(91.0,180.0,2)


def test_actual_notional_weighted_average_and_partial_buy_cap(db):
    _, plan = make_plan(db)
    db.execute("UPDATE order_plans SET max_qty=50, max_amount=1000, price_cap=1000, take_profit_json='[{\"price\":999,\"qty\":1}]' WHERE id=?", (plan,)); db.commit()
    assert evaluate_order_plan(db, plan, tick("b1", price=100, fill_qty=5))["fills"] == [{"side":"buy","qty":5,"price":100}]
    # At 200 only two more shares fit: actual cumulative amount is 900, not qty * latest price.
    assert evaluate_order_plan(db, plan, tick("b2", price=200, fill_qty=50))["fills"] == [{"side":"buy","qty":2,"price":200}]
    assert evaluate_order_plan(db, plan, tick("b3", price=101, fill_qty=50))["fills"] == []
    plan_row = db.execute("SELECT bought_qty,bought_amount FROM order_plans WHERE id=?", (plan,)).fetchone()
    position = db.execute("SELECT qty,avg_price FROM positions WHERE order_plan_id=?", (plan,)).fetchone()
    assert (plan_row["bought_qty"], plan_row["bought_amount"]) == (7, 900)
    assert (position["qty"], position["avg_price"]) == (7, 900 / 7)


def test_exit_ignores_entry_gates_after_expiry_or_invalidation_but_split_caps(db):
    _, plan = make_plan(db)
    db.execute("UPDATE order_plans SET max_amount=1000 WHERE id=?", (plan,)); db.commit()
    assert evaluate_order_plan(db, plan, tick("buy", fill_qty=3))["fills"]
    # An invalidated entry still manages its live position; exit only needs a fresh quote/rule.
    db.execute("UPDATE order_plans SET status='entry_invalidated', expires_at='2026-08-31T09:00:00+09:00' WHERE id=?", (plan,)); db.commit()
    out = evaluate_order_plan(db, plan, tick("tp", price=110, fill_qty=99, gap_pct=99, liquidity=0, trading_value=0, server_now="2026-08-31T14:03:00+09:00", known_at="2026-08-31T14:00:00+09:00"))
    assert out["fills"] == [{"side":"sell","qty":1,"price":110}]
    assert evaluate_order_plan(db, plan, tick("tp-again", price=110, fill_qty=99, server_now="2026-08-31T14:04:00+09:00", known_at="2026-08-31T14:01:00+09:00"))["fills"] == []
    # The stop rule is a full remaining-position exit.
    assert evaluate_order_plan(db, plan, tick("stop", price=79, fill_qty=99, server_now="2026-09-01T10:05:00+09:00", known_at="2026-09-01T10:02:00+09:00"))["fills"] == [{"side":"sell","qty":2,"price":79}]
    assert db.execute("SELECT qty,status FROM positions WHERE order_plan_id=?", (plan,)).fetchone()["status"] == "closed"


def test_draft_freeze_postapproval_edit_reapproval_and_exit_management(db):
    card, plan = make_plan(db)
    draft = edit_order_plan(db, plan, 1, {"price_cap": 91, "max_amount": 180, "max_qty": 2})
    assert draft["reapproval_required"] and db.execute("SELECT status FROM order_plans WHERE id=?", (plan,)).fetchone()["status"] == "entry_invalidated"
    # This was an empty position: reapproval freezes draft rather than mutable card data.
    approved = user_decision(db, card["id"], 1, "approve")
    new = db.execute("SELECT price_cap,max_amount,max_qty,status FROM order_plans WHERE id=?", (approved["order_plan_id"],)).fetchone()
    assert tuple(new) == (91.0, 180.0, 2, "approved")


def test_take_profit_stage_is_restart_safe_and_manual_close_is_full_remaining(db):
    _, plan = make_plan(db)
    db.execute("UPDATE order_plans SET max_amount=1000 WHERE id=?", (plan,)); db.commit()
    evaluate_order_plan(db, plan, tick("buy", fill_qty=3))
    assert evaluate_order_plan(db, plan, tick("tp", price=110, fill_qty=99))["fills"][0]["qty"] == 1
    # Simulate a restarted evaluator with an independent tick key: lineage still blocks this stage.
    assert evaluate_order_plan(db, plan, tick("tp2", price=110, fill_qty=99))["fills"] == []
    assert evaluate_order_plan(db, plan, tick("manual", price=90, fill_qty=1, manual_exit=True))["fills"] == [{"side":"sell","qty":2,"price":90}]
