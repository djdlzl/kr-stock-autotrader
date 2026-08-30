"""Decision-card UI/API regression coverage."""
import tempfile
from fastapi.testclient import TestClient
from kr_stock_autotrader import db as dbmod
from kr_stock_autotrader.decision_cards import create_evidence, save_filter, save_card
from tests.test_decision_card_invariants import AS_OF, card, raw


def test_pass_filter_allows_nonbuy_card_but_only_buy_review_can_be_approved(monkeypatch, tmp_path):
    monkeypatch.setattr(dbmod, "DATABASE_PATH", str(tmp_path / "cards.db"))
    db = dbmod.connect()
    ev = create_evidence(db, {"symbol":"005930","name":"삼성전자","kind":"공시","title":"실적","summary":"호재","source":"DART","source_url":"https://example.test/e","snapshot":{"safe":True},"dedupe_key":"pass-nonbuy","known_at":"2026-08-31T09:00:00+09:00"})
    passed = save_filter(db, ev["id"], raw(), AS_OF, "2026-08-31T09:00:00+09:00")
    observed = save_card(db, card(ev["id"], passed["id"], filter_verdict="PASS", verdict="관찰"))
    assert observed["verdict"] == "관찰"
    db.close()


def test_authenticated_card_summary_and_safe_detail(monkeypatch, tmp_path):
    monkeypatch.setattr(dbmod, "DATABASE_PATH", str(tmp_path / "api.db"))
    from app import app
    client = TestClient(app)
    assert client.post("/api/signup", json={"email":"cards-ui@test.com","password":"long-password"}).status_code == 200
    summary = client.get("/api/cards/summary?date=2026-08-31")
    assert summary.status_code == 200
    data = summary.json()
    for key in ("기준일", "전체 근거", "필터 PASS", "카드 생성", "카드 미생성", "다음 실행", "스케줄러"):
        assert key in data
    assert "detail" not in str(data).lower() and "raw_inputs" not in str(data)
    html = client.get("/app").text
    for text in ("결정 카드", "매수 검토 가능", "카드 미생성", "기존 승인 무효화·재승인 필요", "min-width:0", "max-width:100%", "@media(max-width:390px)"):
        assert text in html
    assert "새 매수 계획" not in html
    assert client.get("/api/cards/missing").status_code == 200
    assert client.get("/api/cards/summary?date=2026-99-99").status_code == 422
