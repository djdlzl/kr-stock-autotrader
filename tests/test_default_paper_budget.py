"""TDD coverage for per-user, immutable paper-budget defaults."""
import pytest
from fastapi.testclient import TestClient

from kr_stock_autotrader import db as dbmod
from kr_stock_autotrader.decision_cards import create_evidence, edit_draft, edit_order_plan, save_card, save_filter, user_card_view, user_decision
from tests.test_decision_card_invariants import AS_OF, card, raw


def make_buy_card(db, key="budget-card", verdict="매수 검토 가능"):
    evidence = create_evidence(db, {
        "symbol": "005930", "name": "삼성전자", "kind": "공시", "title": "실적", "summary": "호재",
        "source": "DART", "source_url": "https://example.test/e", "snapshot": {"safe": True},
        "dedupe_key": key, "known_at": "2026-08-31T09:00:00+09:00",
    })
    filtered = save_filter(db, evidence["id"], raw(), AS_OF, "2026-08-31T09:00:00+09:00")
    return save_card(db, card(evidence["id"], filtered["id"], max_amount=250, verdict=verdict))


def client_for(monkeypatch, tmp_path):
    monkeypatch.setattr(dbmod, "DATABASE_PATH", str(tmp_path / "budget.db"))
    from app import app
    return TestClient(app)


def signup(client, email):
    assert client.post("/api/signup", json={"email": email, "password": "long-password"}).status_code == 200


def test_settings_api_auth_default_bounds_types_csrf_and_user_isolation(monkeypatch, tmp_path):
    client = client_for(monkeypatch, tmp_path)
    assert client.get("/api/settings/paper").status_code == 401
    signup(client, "one@example.test")
    assert client.get("/api/settings/paper").json() == {"default_paper_amount": 500000}
    assert client.patch("/api/settings/paper", json={"default_paper_amount": 750000}).json() == {"default_paper_amount": 750000}
    for invalid in (True, 1.5, "750000", 9999, 1_000_000_001, None):
        assert client.patch("/api/settings/paper", json={"default_paper_amount": invalid}).status_code == 422
    assert client.patch("/api/settings/paper", json={"default_paper_amount": 800000}, headers={"Origin": "https://evil.example"}).status_code == 403
    other = client_for(monkeypatch, tmp_path)
    signup(other, "two@example.test")
    assert other.get("/api/settings/paper").json() == {"default_paper_amount": 500000}
    assert "user_id" not in str(other.get("/api/settings/paper").json())


def test_default_freezes_on_approval_and_never_rewrites_drafts_or_plans(monkeypatch, tmp_path):
    monkeypatch.setattr(dbmod, "DATABASE_PATH", str(tmp_path / "freeze.db"))
    db = dbmod.connect()
    db.execute("INSERT INTO users(email,password) VALUES('u','p')")
    db.execute("INSERT INTO users(email,password) VALUES('v','p')")
    db.commit()
    buy = make_buy_card(db)
    db.execute("INSERT INTO user_settings(user_id,default_paper_amount) VALUES(?,?)", (1, 700000))
    db.commit()
    approved = user_decision(db, buy["id"], 1, "approve")
    assert db.execute("SELECT max_amount FROM order_plans WHERE id=?", (approved["order_plan_id"],)).fetchone()[0] == 700000
    db.execute("UPDATE user_settings SET default_paper_amount=900000 WHERE user_id=1")
    db.commit()
    assert db.execute("SELECT max_amount FROM order_plans WHERE id=?", (approved["order_plan_id"],)).fetchone()[0] == 700000

    next_buy = make_buy_card(db, "draft-card")
    edit_draft(db, next_buy["id"], 1, {"max_amount": 800000})
    db.execute("UPDATE user_settings SET default_paper_amount=600000 WHERE user_id=1")
    db.commit()
    assert user_decision(db, next_buy["id"], 1, "approve")["order_plan_id"]
    assert db.execute("SELECT max_amount FROM order_plans WHERE card_id=? AND user_id=1 ORDER BY id DESC", (next_buy["id"],)).fetchone()[0] == 800000

    non_buy = make_buy_card(db, "non-buy", verdict="관찰")
    with pytest.raises(Exception):
        user_decision(db, non_buy["id"], 1, "approve")
    assert db.execute("SELECT count(*) FROM order_plans WHERE card_id=?", (non_buy["id"],)).fetchone()[0] == 0


def test_safe_card_default_is_the_visible_and_approved_fresh_buy_budget(monkeypatch, tmp_path):
    monkeypatch.setattr(dbmod, "DATABASE_PATH", str(tmp_path / "visible-default.db"))
    db = dbmod.connect()
    db.execute("INSERT INTO users(email,password) VALUES('u','p')")
    db.execute("INSERT INTO user_settings(user_id,default_paper_amount) VALUES(1,700000)")
    db.commit()
    buy = make_buy_card(db, "visible-default")
    view = user_card_view(db, buy["id"], 1)
    assert view["user_state"] == {"decision": None, "order_plan": None, "draft": None, "default_paper_amount": 700000}
    approved = user_decision(db, buy["id"], 1, "approve")
    assert db.execute("SELECT max_amount FROM order_plans WHERE id=?", (approved["order_plan_id"],)).fetchone()[0] == 700000


def test_partial_drafts_keep_existing_snapshot_before_plan_or_changed_default(monkeypatch, tmp_path):
    monkeypatch.setattr(dbmod, "DATABASE_PATH", str(tmp_path / "partial-drafts.db"))
    db = dbmod.connect()
    db.execute("INSERT INTO users(email,password) VALUES('u','p')")
    db.execute("INSERT INTO user_settings(user_id,default_paper_amount) VALUES(1,700000)")
    db.commit()
    buy = make_buy_card(db, "partial-drafts")
    edit_draft(db, buy["id"], 1, {"max_amount": 800000, "max_qty": 2})
    db.execute("UPDATE user_settings SET default_paper_amount=600000 WHERE user_id=1")
    db.commit()
    merged = edit_draft(db, buy["id"], 1, {"price_cap": 91})["draft"]
    assert (merged["price_cap"], merged["max_amount"], merged["max_qty"]) == (91, 800000, 2)
    approved = user_decision(db, buy["id"], 1, "approve")
    frozen = db.execute("SELECT price_cap,max_amount,max_qty FROM order_plans WHERE id=?", (approved["order_plan_id"],)).fetchone()
    assert tuple(frozen) == (91.0, 800000.0, 2)

    first_plan = approved["order_plan_id"]
    first = edit_order_plan(db, first_plan, 1, {"price_cap": 92})["draft"]
    second = edit_order_plan(db, first_plan, 1, {"max_qty": 1})["draft"]
    assert first["price_cap"] == second["price_cap"] == 92
    assert (second["max_amount"], second["max_qty"]) == (800000, 1)
    reapproved = user_decision(db, buy["id"], 1, "approve")
    frozen = db.execute("SELECT price_cap,max_amount,max_qty FROM order_plans WHERE id=?", (reapproved["order_plan_id"],)).fetchone()
    assert tuple(frozen) == (92.0, 800000.0, 1)


def test_default_paper_amount_rejects_non_integer_sqlite_storage(monkeypatch, tmp_path):
    monkeypatch.setattr(dbmod, "DATABASE_PATH", str(tmp_path / "settings-types.db"))
    db = dbmod.connect()
    db.execute("INSERT INTO users(email,password) VALUES('u','p')")
    db.commit()
    for value in (700000.5, "700000.5", "not-a-number"):
        with pytest.raises(Exception):
            db.execute("INSERT INTO user_settings(user_id,default_paper_amount) VALUES(1,?)", (value,))
        db.rollback()
    db.execute("INSERT INTO user_settings(user_id,default_paper_amount) VALUES(1,700000)")
    db.commit()
    for value in (700000.5, "700000.5", "not-a-number"):
        with pytest.raises(Exception):
            db.execute("UPDATE user_settings SET default_paper_amount=? WHERE user_id=1", (value,))
        db.rollback()
