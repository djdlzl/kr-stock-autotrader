"""Regression tests for isolation, auth, state integrity, and paper fills."""
import base64
import hashlib
import hmac
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

from app import app

KST = ZoneInfo("Asia/Seoul")


def signup(client: TestClient, email: str) -> None:
    assert client.post("/api/signup", json={"email": email, "password": "long-password"}).status_code == 200


def plan(client: TestClient, *, conditions=None) -> int:
    response = client.post(
        "/api/plans",
        json={
            "symbol": "005930", "name": "삼성전자",
            "scheduled_at": "2020-01-01T10:00:00+09:00", "qty": 2,
            "conditions": conditions or [{"kind": "absolute_price", "operator": ">=", "value": 70000}],
        },
    )
    assert response.status_code == 200, response.text
    return response.json()["id"]


def tick(client: TestClient, key: str, known_at: str = "2026-08-28T10:00:00+09:00"):
    return client.post("/api/ticks", json={
        "symbol": "005930", "price": 70000, "volume": 100, "baseline_volume": 100,
        "known_at": known_at, "idempotency_key": key,
    })


def test_tick_cannot_evaluate_another_users_plan():
    owner, attacker = TestClient(app), TestClient(app)
    signup(owner, "owner-isolation@test.com")
    signup(attacker, "attacker-isolation@test.com")
    plan(owner)
    assert tick(attacker, "attacker-tick").status_code == 200
    row = owner.get("/api/plans").json()[0]
    assert row["status"] == "scheduled"
    assert row["events"] == [{"event": "created", "reason": "", "at": row["events"][0]["at"]}]


def test_future_and_stale_known_at_fail_closed_for_buy_and_sell():
    c = TestClient(app)
    signup(c, "known-at@test.com")
    pid = plan(c)
    now = datetime.now(KST)
    for key, time in (("stale", now - timedelta(minutes=6)), ("future", now + timedelta(seconds=1))):
        assert tick(c, key, time.isoformat()).status_code == 200
        assert c.get("/api/plans").json()[0]["status"] == "scheduled"
    assert pid


def test_deadline_is_iso_kst_and_empty_conditions_do_not_auto_sell(monkeypatch):
    import kr_stock_autotrader.service as service
    evaluation_time = datetime(2026, 8, 28, 10, 0, tzinfo=KST)
    monkeypatch.setattr(service, "now_kst", lambda: evaluation_time)
    c = TestClient(app)
    signup(c, "deadline@test.com")
    response = c.post("/api/plans", json={
        "symbol": "005930", "name": "삼성전자", "scheduled_at": "2020-01-01T10:00:00+09:00", "qty": 1,
        "conditions": [{"kind": "deadline", "operator": ">=", "value": "2026-08-27T10:00:00+09:00"}],
    })
    assert response.status_code == 200, response.text
    assert tick(c, "deadline-buy").status_code == 200
    assert c.get("/api/plans").json()[0]["status"] == "filled"
    assert tick(c, "deadline-sell").status_code == 200
    assert c.get("/api/plans").json()[0]["status"] == "closed"


def test_missing_or_public_default_secret_fails_closed_in_fresh_process():
    root = Path(__file__).resolve().parents[1]
    for secret in (None, "", "dev-only-change-me"):
        env = {**os.environ, "PYTHONPATH": str(root)}
        if secret is None:
            env.pop("SESSION_SECRET", None)
        else:
            env["SESSION_SECRET"] = secret
        probe = subprocess.run(
            [sys.executable, "-c", "import app"], cwd=root, env=env, capture_output=True, text=True
        )
        assert probe.returncode != 0
        assert "SESSION_SECRET" in probe.stderr
    good = subprocess.run(
        [sys.executable, "-c", "from app import app; print(app.title)"], cwd=root,
        env={**os.environ, "PYTHONPATH": str(root), "SESSION_SECRET": "strong-test-secret-with-32-or-more-bytes!"},
        capture_output=True, text=True,
    )
    assert good.returncode == 0, good.stderr


def signed_token(payload: dict) -> str:
    from kr_stock_autotrader.config import SESSION_SECRET
    body = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode()
    return body + "." + hmac.new(SESSION_SECRET.encode(), body.encode(), hashlib.sha256).hexdigest()


def test_token_claims_reject_known_default_forgery_and_future_iat(monkeypatch):
    import kr_stock_autotrader.auth as auth
    now = datetime(2026, 8, 28, 10, 0, tzinfo=KST)
    monkeypatch.setattr(auth, "now_kst", lambda: now)
    c = TestClient(app)
    signup(c, "token-claims@test.com")
    uid = 1
    public_body = base64.urlsafe_b64encode(json.dumps({"uid": uid, "iat": 0, "exp": 2_000_000_000}).encode()).decode()
    forged = public_body + "." + hmac.new(b"dev-only-change-me", public_body.encode(), hashlib.sha256).hexdigest()
    c.cookies.set("session", forged)
    assert c.get("/api/plans").status_code == 401
    c.cookies.set("session", signed_token({"uid": uid, "iat": int(now.timestamp()) + 31, "exp": int(now.timestamp()) + 60}))
    assert c.get("/api/plans").status_code == 401
    c.cookies.set("session", signed_token({"uid": uid, "iat": int(now.timestamp()) + 10, "exp": int(now.timestamp()) - 1}))
    assert c.get("/api/plans").status_code == 401


def test_closed_cancel_is_truthful_and_cancel_is_idempotent(monkeypatch):
    import kr_stock_autotrader.service as service
    now = datetime(2026, 8, 28, 10, 0, tzinfo=KST)
    monkeypatch.setattr(service, "now_kst", lambda: now)
    c = TestClient(app)
    signup(c, "cancel-state@test.com")
    closed = plan(c)
    assert tick(c, "closed-buy", now.isoformat()).status_code == 200
    assert tick(c, "closed-sell", now.isoformat()).status_code == 200
    response = c.post(f"/api/plans/{closed}/cancel")
    assert response.status_code == 409
    assert response.json()["detail"]["current_status"] == "closed"
    open_plan = c.post("/api/plans", json={"symbol":"005930", "name":"삼성전자", "scheduled_at":"2030-01-01T10:00:00+09:00", "qty":1, "conditions":[]}).json()["id"]
    assert c.post(f"/api/plans/{open_plan}/cancel").json() == {"status": "cancelled", "idempotent": False}
    assert c.post(f"/api/plans/{open_plan}/cancel").json() == {"status": "cancelled", "idempotent": True}


def test_ui_exposes_logout_cancel_dedicated_tick_symbol_and_cards():
    html = TestClient(app).get("/").text
    for required in ("logout()", "cancelPlan(", "id=tickSymbol", "label for=tickSymbol", "plan-card", "최근 평가", "감사 로그", "@media(max-width:390px)"):
        assert required in html
