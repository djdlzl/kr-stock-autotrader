"""RED regressions: legacy known_at date versus operation-date axes."""
from fastapi.testclient import TestClient

from kr_stock_autotrader import db as dbmod
from kr_stock_autotrader.decision_cards import create_evidence, save_card, save_filter
from tests.test_decision_card_invariants import card, raw


def test_legacy_known_at_and_operation_date_are_separate_axes(monkeypatch, tmp_path):
    monkeypatch.setattr(dbmod, "DATABASE_PATH", str(tmp_path / "axes.db"))
    import kr_stock_autotrader.decision_cards as cards
    monkeypatch.setattr(cards, "now", lambda: "2026-09-04T08:00:04+09:00")
    db = dbmod.connect()
    evidence = create_evidence(db, {
        "symbol": "005930", "name": "삼성전자", "kind": "공시", "title": "overnight",
        "summary": "fixture", "source": "DART", "source_url": "https://example.test/e",
        "snapshot": {}, "dedupe_key": "axes", "announcement_at": "2026-09-03T17:15:00+09:00",
        "known_at": "2026-09-03T17:15:00+09:00", "collected_at": "2026-09-03T22:00:00+00:00",
    })
    filt = save_filter(db, evidence["id"], raw(announcement_at="2026-09-03T17:15:00+09:00", market_data_known_at="2026-09-04T08:00:03+09:00"), "2026-09-04T08:00:03+09:00", "2026-09-04T08:00:03+09:00")
    saved = save_card(db, card(evidence["id"], filt["id"]))
    db.close()

    from app import app
    client = TestClient(app)
    assert client.post("/api/signup", json={"email": "axes@test.com", "password": "long-password"}).status_code == 200
    assert [x["id"] for x in client.get("/api/cards?date=2026-09-03").json()] == [saved["id"]]
    assert client.get("/api/cards?date=2026-09-04").json() == []
    assert [x["id"] for x in client.get("/api/cards?operation_date=2026-09-04").json()] == [saved["id"]]
    assert client.get("/api/cards?operation_date=2026-09-03").json() == []
    summary = client.get("/api/cards/summary?operation_date=2026-09-04").json()
    assert (summary["전체 근거"], summary["필터 PASS"], summary["카드 생성"], summary["카드 미생성"]) == (1, 1, 1, 0)
    assert client.get("/api/cards/summary?operation_date=2026-09-03").json()["전체 근거"] == 0
    assert client.get("/api/cards?date=2026-09-03&operation_date=2026-09-04").status_code == 422
    assert client.get("/api/cards/missing?date=2026-09-03&operation_date=2026-09-04").status_code == 422
    assert client.get("/api/cards/summary?date=2026-09-03&operation_date=2026-09-04").status_code == 422


def test_ui_load_uses_operation_date_for_all_three_requests(monkeypatch, tmp_path):
    monkeypatch.setattr(dbmod, "DATABASE_PATH", str(tmp_path / "ui.db"))
    from app import app
    client = TestClient(app)
    assert client.post("/api/signup", json={"email": "ui-axes@test.com", "password": "long-password"}).status_code == 200
    html = client.get("/app").text
    assert "q=`?operation_date=${day}`" in html
    assert "api('cards/summary'+q),api('cards'+q),api('cards/missing'+q)" in html
    assert "선택 운영일" in html
