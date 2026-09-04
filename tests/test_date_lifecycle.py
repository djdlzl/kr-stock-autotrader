"""RED/green contracts for KST business-date card lifecycle."""
import os
import re
import subprocess
from fastapi.testclient import TestClient

from kr_stock_autotrader import db as dbmod
from kr_stock_autotrader.decision_cards import create_evidence, mutate_evidence, save_card, save_filter
from tests.test_decision_card_invariants import card, raw


def _seed(db, key, known_at, *, make_card=True, delayed_at=None, invalid=False, collected_at=None):
    evidence = create_evidence(db, {
        "symbol": "005930", "name": "삼성전자", "kind": "공시", "title": key,
        "summary": "호재", "source": "DART", "source_url": "https://example.test/e",
        "snapshot": {"key": key}, "dedupe_key": key, "known_at": known_at,
        "collected_at": collected_at or known_at,
        "announcement_at": known_at,
    })
    if invalid:
        mutate_evidence(db, evidence["id"], invalidate=True)
        return evidence, None
    if not make_card:
        return evidence, None
    filt = save_filter(db, evidence["id"], raw(
        announcement_at=known_at, market_data_known_at=known_at
    ), known_at, known_at)
    if delayed_at:
        import kr_stock_autotrader.decision_cards as decision_cards
        original_now = decision_cards.now
        decision_cards.now = lambda: delayed_at
        try:
            result = save_card(db, card(evidence["id"], filt["id"]))
        finally:
            decision_cards.now = original_now
    else:
        result = save_card(db, card(evidence["id"], filt["id"]))
    return evidence, result


def test_date_scoped_lifecycle_api_uses_evidence_kst_day_and_latest_active_card(monkeypatch, tmp_path):
    monkeypatch.setattr(dbmod, "DATABASE_PATH", str(tmp_path / "date.db"))
    db = dbmod.connect()
    day1, old_card = _seed(db, "day1", "2026-08-31T23:30:00+09:00", delayed_at="2026-09-01T08:00:00+09:00")
    # Regeneration preserves history, but dashboard count is latest active card per lineage.
    # Same immutable filter may generate a new card version; filter history is not overwritten.
    new_card = save_card(db, card(day1["id"], old_card["filter_id"]))
    _seed(db, "day2", "2026-09-01T09:00:00+09:00")
    missing, _ = _seed(db, "missing", "2026-08-31T10:00:00+09:00", make_card=False)
    invalid, _ = _seed(db, "invalid", "2026-08-31T11:00:00+09:00", invalid=True)
    db.close()

    from app import app
    first, second = TestClient(app), TestClient(app)
    assert first.post("/api/signup", json={"email": "first@test.com", "password": "long-password"}).status_code == 200
    assert second.post("/api/signup", json={"email": "second@test.com", "password": "long-password"}).status_code == 200
    assert first.post(f"/api/cards/{new_card['id']}/decisions", json={"decision": "approve"}).status_code == 200
    assert second.post(f"/api/cards/{new_card['id']}/decisions", json={"decision": "reject"}).status_code == 200

    overview = first.get("/api/cards/summary?date=2026-08-31")
    assert overview.status_code == 200
    data = overview.json()
    assert data["전체 근거"] == 3
    assert data["카드 생성"] == 1 and data["카드 미생성"] == 1
    assert data["승인"] == 1 and data["거절"] == 0
    assert data["무효화"] == 1
    assert data["필터 PASS"] == 1 and data["필터 FAIL"] == 0
    cards = first.get("/api/cards?date=2026-08-31").json()
    assert [item["id"] for item in cards] == [new_card["id"], old_card["id"]]
    assert all(item["evidence"]["known_at"].startswith("2026-08-31") for item in cards)
    assert first.get("/api/cards/missing?date=2026-08-31").json()[0]["id"] == missing["id"]
    assert all(item["id"] != invalid["id"] for item in first.get("/api/cards/missing?date=2026-08-31").json())
    assert first.get(f"/api/cards/{new_card['id']}").json()["user_state"]["decision"]["decision"] == "approve"
    assert second.get(f"/api/cards/{new_card['id']}").json()["user_state"]["decision"]["decision"] == "reject"
    assert first.get("/api/cards/summary?date=2026-9-1").status_code == 422
    assert first.get("/api/cards?date=not-a-date").status_code == 422
    assert first.get("/api/cards/summary?date=2026-08-30").json()["전체 근거"] == 0


def test_all_date_cards_show_only_current_active_versions_and_user_fill_states(monkeypatch, tmp_path):
    monkeypatch.setattr(dbmod, "DATABASE_PATH", str(tmp_path / "all-date-cards.db"))
    db = dbmod.connect()
    day1, old = _seed(db, "lineage", "2026-08-31T10:00:00+09:00")
    current = save_card(db, card(day1["id"], old["filter_id"]))
    _, day2 = _seed(db, "day2", "2026-09-01T10:00:00+09:00")
    db.close()

    from app import app
    owner, other = TestClient(app), TestClient(app)
    assert owner.post("/api/signup", json={"email": "owner@test.com", "password": "long-password"}).status_code == 200
    assert other.post("/api/signup", json={"email": "other@test.com", "password": "long-password"}).status_code == 200

    db = dbmod.connect()
    def plan_with_fills(card_row, fills):
        plan_id = db.execute("""INSERT INTO order_plans(card_id,card_version,user_id,approved_at,valid_until,symbol,price_cap,max_amount,max_qty,split_json,order_type,stop_loss,take_profit_json,evidence_invalidation,expires_at,status,version_hash)
          VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'closed',?) RETURNING id""", (
            card_row["id"], card_row["version"], 1, "2026-08-31T09:00:00+09:00", "2026-09-02T10:00:00+09:00", "005930", 100, 1000, 10,
            "[]", "limit", 80, "[]", "{}", "2026-09-02T10:00:00+09:00", card_row["id"],
        )).fetchone()["id"]
        db.executemany("INSERT INTO order_fills(order_plan_id,event_key,side,qty,price,filled_at) VALUES(?,?,?,?,?,?)", [
            (plan_id, f"{plan_id}-{index}", side, qty, price, filled_at) for index, (side, qty, price, filled_at) in enumerate(fills)
        ])

    plan_with_fills(current, [("buy", 2, 90, "2026-08-31T10:00:00+09:00")])
    plan_with_fills(day2, [("buy", 1, 91, "2026-09-01T10:00:00+09:00"), ("sell", 1, 95, "2026-09-01T11:00:00+09:00")])
    db.commit(); db.close()

    all_dates = owner.get("/api/cards")
    assert all_dates.status_code == 200
    visible = {item["id"]: item for item in all_dates.json()}
    assert set(visible) == {current["id"], day2["id"]}
    assert visible[current["id"]]["fill_summary"]["fill_state"] == "bought"
    assert visible[day2["id"]]["fill_summary"]["fill_state"] == "sold_complete"
    assert "snapshot" not in visible[current["id"]]["evidence"]
    assert other.get("/api/cards").json()[0]["fill_summary"]["fill_state"] == "unfilled"
    assert [item["id"] for item in owner.get("/api/cards?date=2026-08-31").json()] == [current["id"], old["id"]]
    assert [item["id"] for item in owner.get("/api/cards?date=2026-09-01").json()] == [day2["id"]]
    assert owner.get("/api/cards?date=not-a-date").status_code == 422


def test_date_ui_has_server_date_picker_lifecycle_notice_and_mobile_constraints(monkeypatch, tmp_path):
    monkeypatch.setattr(dbmod, "DATABASE_PATH", str(tmp_path / "ui.db"))
    from app import app
    client = TestClient(app)
    assert client.post("/api/signup", json={"email": "ui-date@test.com", "password": "long-password"}).status_code == 200
    html = client.get("/app").text
    for text in ("재료 업무일", "카드 생성시각", "원문 발표시각", "카드 버전", "무효", "기준일", "type=\"date\"", "fresh quote/tick", "동결 조건 재검증", "overflow-x:hidden"):
        assert text in html
    assert "cards/summary'+q" in html and "cards/missing'+q" in html
    assert "api('cards'+q)" in html and "api('cards')" not in html
    assert "c.evidence.collected_at" in html and "c.evidence.known_at?.slice" not in html
    assert "모든 기준일의 현재 카드" not in html and "요약과 카드 미생성 목록에만 적용" not in html


def test_previous_business_day_is_timezone_independent():
    """The browser's local zone must not move a KST calendar date back two days."""
    from kr_stock_autotrader.ui import APP_HTML
    helper = re.search(r"const previousBusinessDate=(day=>\{.*?\});", APP_HTML).group(1)
    for zone in ("Pacific/Kiritimati", "Asia/Seoul", "America/Los_Angeles"):
        output = subprocess.check_output(
            ["node", "-e", f"console.log(({helper})('2026-09-01'))"],
            text=True, env={**os.environ, "TZ": zone},
        ).strip()
        assert output == "2026-08-31"


def test_missing_cards_expose_only_safe_evidence_fields(monkeypatch, tmp_path):
    monkeypatch.setattr(dbmod, "DATABASE_PATH", str(tmp_path / "safe-missing.db"))
    db = dbmod.connect()
    missing, _ = _seed(db, "LEAK", "2026-08-31T10:00:00+09:00", make_card=False)
    db.execute("UPDATE material_evidence SET title=?, snapshot=?, audit_json=?, created_by=?, dedupe_key=? WHERE id=?", (
        "safe title", '{"secret":"LEAK"}', '["LEAK"]', "LEAK", "LEAK", missing["id"]
    ))
    db.commit(); db.close()
    from app import app
    anonymous = TestClient(app)
    assert anonymous.get("/api/cards/missing?date=2026-08-31").status_code == 401
    client = TestClient(app)
    assert client.post("/api/signup", json={"email": "safe-missing@test.com", "password": "long-password"}).status_code == 200
    response = client.get("/api/cards/missing?date=2026-08-31")
    assert response.status_code == 200
    item = response.json()[0]
    assert set(item) <= {"id", "symbol", "name", "kind", "title", "summary", "source", "source_url", "announcement_at", "collected_at", "known_at", "status", "version", "snapshot_available"}
    assert "LEAK" not in response.text
    assert not {"snapshot", "audit_json", "created_by", "dedupe_key", "newness", "updated_at", "invalidated_at"} & set(item)


def test_summary_uses_latest_active_lineage_card_when_newer_version_invalidated(monkeypatch, tmp_path):
    monkeypatch.setattr(dbmod, "DATABASE_PATH", str(tmp_path / "active-lineage.db"))
    db = dbmod.connect()
    evidence, v1 = _seed(db, "lineage", "2026-08-31T10:00:00+09:00")
    v2 = save_card(db, card(evidence["id"], v1["filter_id"]))
    db.execute("UPDATE decision_cards SET invalidated_at=NULL WHERE id=?", (v1["id"],))
    db.execute("UPDATE decision_cards SET invalidated_at=? WHERE id=?", ("2026-08-31T11:00:00+09:00", v2["id"]))
    db.execute("INSERT INTO users(email,password) VALUES(?,?)", ("active-lineage@test.com", "p"))
    db.commit(); db.close()
    from app import app
    client = TestClient(app)
    client.cookies.set("session", __import__("kr_stock_autotrader.auth", fromlist=["issue_session"]).issue_session(1))
    assert client.post(f"/api/cards/{v1['id']}/decisions", json={"decision": "hold"}).status_code == 200
    summary = client.get("/api/cards/summary?date=2026-08-31").json()
    assert summary["카드 생성"] == 1 and summary["보류"] == 1
    assert [item["id"] for item in client.get("/api/cards?date=2026-08-31").json()] == [v2["id"], v1["id"]]


def test_historical_summary_uses_only_selected_business_day_scheduler_runs(monkeypatch, tmp_path):
    monkeypatch.setattr(dbmod, "DATABASE_PATH", str(tmp_path / "scheduler-day.db"))
    db = dbmod.connect()
    for day, status in (("2026-08-31", "success"), ("2026-09-01", "failed")):
        for kind, hour in (("research", "0700"), ("card", "0800")):
            db.execute("INSERT INTO scheduler_runs(run_key,kind,status,started_at,finished_at,detail) VALUES(?,?,?,?,?,?)", (
                f"{kind}-{day}-{hour}-kst", kind, status, f"{day}T{hour[:2]}:00:00+09:00", f"{day}T{hour[:2]}:01:00+09:00", '{"count": 3}'
            ))
    db.execute("INSERT INTO users(email,password) VALUES(?,?)", ("scheduler-day@test.com", "p")); db.commit(); db.close()
    from app import app
    client = TestClient(app); client.cookies.set("session", __import__("kr_stock_autotrader.auth", fromlist=["issue_session"]).issue_session(1))
    historical = client.get("/api/cards/summary?date=2026-08-31").json()
    assert historical["최근 실행"]["07:00"]["상태"] == "success"
    assert historical["최근 실행"]["08:00"]["상태"] == "success"


def test_card_fill_summary_is_user_scoped_across_all_plan_generations(monkeypatch, tmp_path):
    """Only actual fills for this card and this user define the display state."""
    monkeypatch.setattr(dbmod, "DATABASE_PATH", str(tmp_path / "fill-summary.db"))
    db = dbmod.connect()
    _, card_row = _seed(db, "fills", "2026-08-31T10:00:00+09:00")
    db.executemany("INSERT INTO users(email,password) VALUES(?,?)", [("one@test", "p"), ("two@test", "p")])
    base = (card_row["id"], 1, "2026-08-31T09:00:00+09:00", "2026-09-01T10:00:00+09:00", "005930", 100, 1000, 10, "[]", "limit", 80, "[]", "{}", "2026-09-01T10:00:00+09:00", "hash")
    # Two user-one generations prove aggregation isn't limited to the newest plan.
    for user_id, suffix in ((1, "old"), (1, "new"), (2, "other")):
        db.execute("""INSERT INTO order_plans(card_id,card_version,user_id,approved_at,valid_until,symbol,price_cap,max_amount,max_qty,split_json,order_type,stop_loss,take_profit_json,evidence_invalidation,expires_at,status,version_hash)
          VALUES(?,?,?, ?,?,?, ?,?,?, ?,?,?, ?,?,?, 'closed',?)""", base[:2] + (user_id,) + base[2:-1] + (base[-1] + suffix,))
    plans = [row["id"] for row in db.execute("SELECT id FROM order_plans ORDER BY id")]
    db.commit()
    from kr_stock_autotrader.decision_cards import user_card_view
    assert user_card_view(db, card_row["id"], 1)["fill_summary"] == {"first_buy_at": None, "last_full_sell_at": None, "fill_state": "unfilled"}
    db.execute("INSERT INTO order_fills(order_plan_id,event_key,side,qty,price,filled_at) VALUES(?,?,?,?,?,?)", (plans[0], "buy-old", "buy", 2, 90, "2026-08-31T10:01:00+09:00"))
    db.execute("INSERT INTO order_fills(order_plan_id,event_key,side,qty,price,filled_at) VALUES(?,?,?,?,?,?)", (plans[1], "buy-new", "buy", 3, 91, "2026-08-31T10:02:00+09:00"))
    db.execute("INSERT INTO order_fills(order_plan_id,event_key,side,qty,price,filled_at) VALUES(?,?,?,?,?,?)", (plans[1], "partial", "sell", 4, 95, "2026-08-31T11:00:00+09:00"))
    db.execute("INSERT INTO order_fills(order_plan_id,event_key,side,qty,price,filled_at) VALUES(?,?,?,?,?,?)", (plans[2], "other-buy", "buy", 9, 90, "2026-08-31T09:00:00+09:00"))
    db.commit()
    partial = user_card_view(db, card_row["id"], 1)["fill_summary"]
    assert partial == {"first_buy_at": "2026-08-31T10:01:00+09:00", "last_full_sell_at": None, "fill_state": "bought"}
    assert user_card_view(db, card_row["id"], 2)["fill_summary"] == {"first_buy_at": "2026-08-31T09:00:00+09:00", "last_full_sell_at": None, "fill_state": "bought"}
    db.execute("INSERT INTO order_fills(order_plan_id,event_key,side,qty,price,filled_at) VALUES(?,?,?,?,?,?)", (plans[1], "final", "sell", 1, 96, "2026-08-31T12:00:00+09:00")); db.commit()
    complete = user_card_view(db, card_row["id"], 1)["fill_summary"]
    assert complete == {"first_buy_at": "2026-08-31T10:01:00+09:00", "last_full_sell_at": "2026-08-31T12:00:00+09:00", "fill_state": "sold_complete"}
    assert set(user_card_view(db, card_row["id"], 1)["fill_summary"]) == {"first_buy_at", "last_full_sell_at", "fill_state"}


def test_fill_summary_replays_chronological_state_and_ignores_oversells(monkeypatch, tmp_path):
    monkeypatch.setattr(dbmod, "DATABASE_PATH", str(tmp_path / "fill-replay.db"))
    db = dbmod.connect()
    _, card_row = _seed(db, "fill-replay", "2026-08-31T10:00:00+09:00")
    db.executemany("INSERT INTO users(email,password) VALUES(?,?)", [("owner@test", "p"), ("other@test", "p")])
    values = (card_row["id"], 1, 1, "2026-08-31T09:00:00+09:00", "2026-09-01T10:00:00+09:00", "005930", 100, 1000, 10, "[]", "limit", 80, "[]", "{}", "2026-09-01T10:00:00+09:00")
    plans = []
    for suffix in ("one", "two"):
        plans.append(db.execute("""INSERT INTO order_plans(card_id,card_version,user_id,approved_at,valid_until,symbol,price_cap,max_amount,max_qty,split_json,order_type,stop_loss,take_profit_json,evidence_invalidation,expires_at,status,version_hash)
          VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'closed',?) RETURNING id""", values + (suffix,)).fetchone()["id"])
    other_values = (card_row["id"], 1, 2) + values[3:]
    other = db.execute("""INSERT INTO order_plans(card_id,card_version,user_id,approved_at,valid_until,symbol,price_cap,max_amount,max_qty,split_json,order_type,stop_loss,take_profit_json,evidence_invalidation,expires_at,status,version_hash)
      VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'closed',?) RETURNING id""", other_values + ("other",)).fetchone()["id"]
    # IDs are deliberately not chronological: the replay must use filled_at then id.
    fills = [
        (plans[1], "late-buy", "buy", 2, "2026-08-31T12:00:00+09:00"),
        (plans[0], "first-buy", "buy", 3, "2026-08-31T10:00:00+09:00"),
        (plans[0], "close-first", "sell", 9, "2026-08-31T11:00:00+09:00"),
        (plans[1], "close-second", "sell", 2, "2026-08-31T13:00:00+09:00"),
        (plans[1], "oversell-after-close", "sell", 8, "2026-08-31T14:00:00+09:00"),
        (other, "other-user", "buy", 99, "2026-08-31T09:00:00+09:00"),
    ]
    db.executemany("INSERT INTO order_fills(order_plan_id,event_key,side,qty,price,filled_at) VALUES(?,?,?, ?,1,?)", fills)
    db.commit()
    from kr_stock_autotrader.decision_cards import user_card_view
    assert user_card_view(db, card_row["id"], 1)["fill_summary"] == {
        "first_buy_at": "2026-08-31T10:00:00+09:00", "last_full_sell_at": "2026-08-31T13:00:00+09:00", "fill_state": "sold_complete"
    }
    assert user_card_view(db, card_row["id"], 2)["fill_summary"] == {
        "first_buy_at": "2026-08-31T09:00:00+09:00", "last_full_sell_at": None, "fill_state": "bought"
    }
