"""Regression tests for isolation, known-at, and paper fills (RED before repair)."""
from datetime import datetime, timedelta
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
    assert tick(c, "deadline").status_code == 200
    assert c.get("/api/plans").json()[0]["status"] == "closed"
