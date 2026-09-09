import json

from kr_stock_autotrader import db as dbmod
from kr_stock_autotrader.decision_cards import create_evidence, save_card, save_filter, user_card_view
from kr_stock_autotrader.expected_price_runtime import evaluate_and_persist_expected_price, expected_price_run_detail
from tests.test_decision_card_invariants import card, raw
from tests.test_expected_price_runtime import valid_inputs

KNOWN = "2026-09-07T07:00:00+09:00"
AS_OF = "2026-09-07T09:05:00+09:00"


def test_expected_price_persists_readbacks_is_idempotent_and_is_visible(monkeypatch, tmp_path):
    monkeypatch.setattr(dbmod, "DATABASE_PATH", str(tmp_path / "fixture.sqlite"))
    db = dbmod.connect()
    package = valid_inputs()
    package.pop("market_baseline")
    evidence = create_evidence(db, {"symbol":"005930", "name":"fixture", "kind":"disclosure", "title":"fixture", "summary":"fixture", "source":"dart", "source_url":"https://example.test", "announcement_at":KNOWN, "collected_at":KNOWN, "known_at":KNOWN, "snapshot":{"economic_terms":{"expected_price_inputs":package}}, "dedupe_key":"event-1"})
    baseline = valid_inputs()["market_baseline"]
    filt = save_filter(db, evidence["id"], raw(announcement_at=KNOWN, market_data_known_at=KNOWN, expected_price_baseline=baseline), KNOWN, KNOWN)
    saved = save_card(db, card(evidence["id"], filt["id"]))
    first = evaluate_and_persist_expected_price(db=db, run_key="expected-price-20260907-card-1", card=saved, evidence=evidence, filter_result=filt, requested_as_of=AS_OF)
    second = evaluate_and_persist_expected_price(db=db, run_key="expected-price-20260907-card-1", card=saved, evidence=evidence, filter_result=filt, requested_as_of=AS_OF)
    assert first["result"]["status"] == "COMPUTED"
    assert first["result"]["calculated_value"] == 20_900
    assert second["idempotent"] is True
    assert db.execute("SELECT count(*) FROM expected_price_runs").fetchone()[0] == 1
    assert expected_price_run_detail(db, run_key=first["run_key"])["result"] == first["result"]
    monkeypatch.setenv("INTERNAL_API_KEY", "expected-price-key")
    from fastapi.testclient import TestClient
    from app import app
    client = TestClient(app)
    api_response = client.post(f"/api/internal/cards/{saved['id']}/expected-price", json={"run_key":"expected-price-api-card-1", "as_of":AS_OF}, headers={"X-Internal-API-Key":"expected-price-key"})
    assert api_response.status_code == 200
    assert api_response.json()["result"]["calculated_value"] == 20_900
    readback = client.get("/api/internal/expected-price-runs/expected-price-api-card-1", headers={"X-Internal-API-Key":"expected-price-key"})
    assert readback.status_code == 200 and readback.json()["status"] == "COMPUTED"
    db.execute("INSERT INTO users(email,password) VALUES('expected@test.com','x')")
    assert user_card_view(db, saved["id"], 1)["expected_price"]["result"]["calculated_value"] == 20_900
    db.close()


def test_expected_price_missing_inputs_is_terminal_hold(monkeypatch, tmp_path):
    monkeypatch.setattr(dbmod, "DATABASE_PATH", str(tmp_path / "hold.sqlite"))
    db = dbmod.connect()
    evidence = create_evidence(db, {"symbol":"005930", "name":"fixture", "kind":"disclosure", "title":"fixture", "summary":"fixture", "source":"dart", "source_url":"https://example.test", "announcement_at":KNOWN, "collected_at":KNOWN, "known_at":KNOWN, "snapshot":{"economic_terms":{}}, "dedupe_key":"expected-price-hold"})
    filt = save_filter(db, evidence["id"], raw(announcement_at=KNOWN, market_data_known_at=KNOWN), KNOWN, KNOWN)
    saved = save_card(db, card(evidence["id"], filt["id"]))
    result = evaluate_and_persist_expected_price(db=db, run_key="expected-price-20260907-card-2", card=saved, evidence=evidence, filter_result=filt, requested_as_of=AS_OF)
    assert result["status"] == "HOLD_MISSING_INPUT"
    assert result["result"]["reason"] == "missing_persisted_valuation_inputs"
    db.close()
