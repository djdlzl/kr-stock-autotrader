"""RED/green contracts for KST business-date card lifecycle."""
from fastapi.testclient import TestClient

from kr_stock_autotrader import db as dbmod
from kr_stock_autotrader.decision_cards import create_evidence, mutate_evidence, save_card, save_filter
from tests.test_decision_card_invariants import card, raw


def _seed(db, key, known_at, *, make_card=True, delayed_at=None, invalid=False):
    evidence = create_evidence(db, {
        "symbol": "005930", "name": "삼성전자", "kind": "공시", "title": key,
        "summary": "호재", "source": "DART", "source_url": "https://example.test/e",
        "snapshot": {"key": key}, "dedupe_key": key, "known_at": known_at,
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
    result = save_card(db, card(evidence["id"], filt["id"]))
    if delayed_at:
        db.execute("UPDATE decision_cards SET generated_at=? WHERE id=?", (delayed_at, result["id"]))
        db.commit()
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


def test_date_ui_has_server_date_picker_lifecycle_notice_and_mobile_constraints(monkeypatch, tmp_path):
    monkeypatch.setattr(dbmod, "DATABASE_PATH", str(tmp_path / "ui.db"))
    from app import app
    client = TestClient(app)
    assert client.post("/api/signup", json={"email": "ui-date@test.com", "password": "long-password"}).status_code == 200
    html = client.get("/app").text
    for text in ("재료 업무일", "카드 생성시각", "원문 발표시각", "카드 버전", "무효", "기준일", "type=\"date\"", "fresh quote/tick", "동결 조건 재검증", "overflow-x:hidden"):
        assert text in html
    assert "cards/summary'+q" in html and "cards/missing'+q" in html
