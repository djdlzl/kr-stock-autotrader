"""Hybrid second-stage decision engine contracts."""

import os
import tempfile
from copy import deepcopy
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from kr_stock_autotrader import db as dbmod
from kr_stock_autotrader.decision_cards import create_evidence, save_card, save_filter
from tests.test_event_scenarios import _lineage, payload as scenario_payload
from tests.test_intraday_market_context import _intraday_snapshot, _patch_lineage_context, _seed_lineage
from tests.test_decision_card_invariants import raw, card as card_payload


INTERNAL = {"X-Internal-API-Key": "hybrid-key"}
POLICY_ID = "giraffe-hybrid-good-base-bad-v1"
KNOWN = "2026-09-03T08:00:00+09:00"
AS_OF = "2026-09-07T09:05:00+09:00"


@pytest.fixture
def app_client(monkeypatch, tmp_path):
    monkeypatch.setenv("INTERNAL_API_KEY", INTERNAL["X-Internal-API-Key"])
    monkeypatch.setattr(dbmod, "DATABASE_PATH", str(tmp_path / "hybrid.db"))
    from app import app

    client = TestClient(app)
    assert client.post("/api/signup", json={"email": "hybrid@test.com", "password": "long-password"}).status_code == 200
    return app, client


def _make_lineage(db, *, symbol="005930", name="삼성전자", key="hybrid-evidence"):
    evidence = create_evidence(
        db,
        {
            "symbol": symbol,
            "name": name,
            "kind": "disclosure",
            "title": "hybrid",
            "summary": "fixture",
            "source": "dart",
            "source_url": "https://example.test/e",
            "announcement_at": KNOWN,
            "collected_at": KNOWN,
            "known_at": KNOWN,
            "snapshot": {"benchmark_symbol": "229200", "previous_close_krw": 69500.0},
            "dedupe_key": key,
        },
    )
    filt = save_filter(db, evidence["id"], raw(announcement_at=KNOWN, market_data_known_at=KNOWN), KNOWN, KNOWN)
    payload = card_payload(evidence["id"], filt["id"])
    payload["card"]["source_evidence"] = [{"id": evidence["id"], "source": "dart", "url": "https://example.test/e"}]
    saved = save_card(db, payload)
    _patch_lineage_context(db, filter_id=filt["id"], benchmark_symbol="229200", previous_close_krw=69500.0)
    return evidence, filt, saved


def _scenario_payload(evidence_id, card_id, *, version=1):
    payload = deepcopy(scenario_payload(evidence_id, card_id))
    payload["event_identity"] = "DART:2026-09-03:hybrid"
    payload["version"] = version
    payload["symbol"] = "005930"
    return payload


def _market_context(app, client, card_id, *, symbol="005930"):
    import kr_stock_autotrader.intraday_market_context as mc
    from kr_stock_autotrader.intraday_market_context import evaluate_intraday_market_context, persist_intraday_market_context_run, resolve_intraday_lineage_context, same_time_history_for_symbol

    db = dbmod.connect()
    card = db.execute("SELECT * FROM decision_cards WHERE id=?", (card_id,)).fetchone()
    evidence = db.execute("SELECT * FROM material_evidence WHERE id=?", (card["evidence_id"],)).fetchone()
    filter_result = db.execute("SELECT * FROM deterministic_filter_results WHERE id=?", (card["filter_id"],)).fetchone()
    filter_result = dict(filter_result)
    filter_result["computed_outputs"] = __import__("json").loads(filter_result["computed_outputs"])
    card = dict(card)
    evidence = dict(evidence)
    benchmark_symbol, previous_close = resolve_intraday_lineage_context(card=card, evidence=evidence, filter_result=filter_result)
    db.close()

    snapshot_at = datetime(2026, 9, 7, 9, 5, 2, tzinfo=ZoneInfo("Asia/Seoul"))
    stock_snapshot = _intraday_snapshot(
        symbol,
        prices=[100, 100, 100, 100, 100, 100],
        volumes=[10, 10, 10, 10, 10, 10],
        retrieved_at=snapshot_at,
        requested_as_of=AS_OF,
    )
    benchmark_snapshot = _intraday_snapshot(
        benchmark_symbol,
        prices=[100, 100, 100, 100, 100, 100],
        volumes=[10, 10, 10, 10, 10, 10],
        retrieved_at=snapshot_at,
        requested_as_of=AS_OF,
    )
    orderbook = {
        "status": "ok",
        "symbol": symbol,
        "last_price": 100.0,
        "best_bid": 99.96,
        "best_ask": 100.0,
        "top_bid_qty": 20.0,
        "top_ask_qty": 20.0,
        "quote_known_at": snapshot_at.isoformat(),
        "retrieved_at": snapshot_at.isoformat(),
        "timestamp_source": "network_retrieved_at",
        "source": "KIS",
        "environment": "production",
    }
    same_time_history = [
        {"run_id": 1, "session_date": "2026-09-01", "requested_as_of": AS_OF, "status": "VERIFIED", "completed_interval_count": 5, "cumulative_volume_0900_to_as_of": 100.0},
        {"run_id": 2, "session_date": "2026-09-02", "requested_as_of": AS_OF, "status": "VERIFIED", "completed_interval_count": 5, "cumulative_volume_0900_to_as_of": 110.0},
        {"run_id": 3, "session_date": "2026-09-03", "requested_as_of": AS_OF, "status": "VERIFIED", "completed_interval_count": 5, "cumulative_volume_0900_to_as_of": 120.0},
    ]
    result = evaluate_intraday_market_context(stock_snapshot=stock_snapshot, benchmark_snapshot=benchmark_snapshot, orderbook=orderbook, same_time_history=same_time_history, previous_close_krw=previous_close)
    result["known_at"] = snapshot_at.isoformat()
    result["retrieved_at"] = snapshot_at.isoformat()
    db = dbmod.connect()
    response = persist_intraday_market_context_run(
        db,
        run_key="hybrid-market-context-0905",
        card=dict(card),
        evidence=dict(evidence),
        filter_result=filter_result,
        requested_as_of=AS_OF,
        stock_snapshot=stock_snapshot,
        benchmark_snapshot=benchmark_snapshot,
        orderbook=orderbook,
        result=result,
    )
    db.close()
    return response


def _seed_outcomes(client, scenario_identity, rows):
    created = []
    for index, payload in enumerate(rows, 1):
        body = {
            "idempotency_key": f"row-{index}",
            "observation_cutoff_at": "2026-09-11T15:30:00+09:00",
            "bars": payload,
        }
        response = client.post(f"/api/internal/scenario-sets/{scenario_identity}/hybrid-outcomes", headers=INTERNAL, json=body)
        assert response.status_code == 200, response.text
        created.append(response.json())
    return created


def _calibration_payload(scenario_set_id, *, holdout_key="holdout-a", is_start="2026-09-08T00:00:00+09:00", is_end="2026-09-09T00:00:00+09:00", oos_start="2026-09-09T00:00:00+09:00", oos_end="2026-09-11T00:00:00+09:00"):
    return {
        "scenario_set_id": scenario_set_id,
        "policy_identity": POLICY_ID,
        "policy_version": 1,
        "holdout_key": holdout_key,
        "is_window": {"start": is_start, "end": is_end},
        "oos_window": {"start": oos_start, "end": oos_end},
        "cutoff_at": "2026-09-11T23:59:59+09:00",
    }


def _evaluate_payload(calibration_id):
    return {"policy_identity": POLICY_ID, "policy_version": 1, "calibration_snapshot_id": calibration_id, "recommendation_only": True}


def test_hybrid_outcomes_exclude_future_rows_and_close_on_same_bar_ambiguity(app_client):
    app, client = app_client
    db = dbmod.connect()
    evidence, filt, saved = _make_lineage(db)
    db.commit()
    db.close()

    scenario = client.post("/api/internal/scenario-sets", headers=INTERNAL, json=_scenario_payload(evidence["id"], saved["id"]))
    assert scenario.status_code == 200, scenario.text
    out = scenario.json()

    future_row = {
        "exchange_at": "2026-09-12T15:30:00+09:00",
        "known_at": "2026-09-12T15:30:00+09:00",
        "open_krw": 100.0,
        "high_krw": 120.0,
        "low_krw": 95.0,
        "close_krw": 110.0,
        "volume": 1000.0,
        "symbol": "005930",
    }
    response = client.post(f"/api/internal/scenario-sets/{out['event_identity']}/hybrid-outcomes", headers=INTERNAL, json={"idempotency_key": "future", "observation_cutoff_at": "2026-09-11T15:30:00+09:00", "bars": [future_row]})
    assert response.status_code == 422

    ambiguous = {
        "exchange_at": "2026-09-08T15:30:00+09:00",
        "known_at": "2026-09-08T15:30:00+09:00",
        "open_krw": 100.0,
        "high_krw": 120.0,
        "low_krw": 80.0,
        "close_krw": 100.0,
        "volume": 1000.0,
        "symbol": "005930",
    }
    response = client.post(f"/api/internal/scenario-sets/{out['event_identity']}/hybrid-outcomes", headers=INTERNAL, json={"idempotency_key": "ambiguous", "observation_cutoff_at": "2026-09-11T15:30:00+09:00", "bars": [ambiguous]})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["realized_label"] == "BAD"
    assert body["realized_reason"] == "same_bar_ambiguity_closed_bad"
    duplicate = client.post(f"/api/internal/scenario-sets/{out['event_identity']}/hybrid-outcomes", headers=INTERNAL, json={"idempotency_key": "ambiguous", "observation_cutoff_at": "2026-09-11T15:30:00+09:00", "bars": [dict(ambiguous)]})
    assert duplicate.status_code == 200 and duplicate.json()["idempotent"] is True
    collision = client.post(f"/api/internal/scenario-sets/{out['event_identity']}/hybrid-outcomes", headers=INTERNAL, json={"idempotency_key": "ambiguous", "observation_cutoff_at": "2026-09-11T15:30:00+09:00", "bars": [{**ambiguous, "close_krw": 101.0}]})
    assert collision.status_code == 409


def test_hybrid_calibration_chronology_rejects_forgery_and_holdout_reuse(app_client):
    app, client = app_client
    db = dbmod.connect()
    evidence, filt, saved = _make_lineage(db, key="hybrid-calibration")
    db.commit()
    db.close()

    scenario = client.post("/api/internal/scenario-sets", headers=INTERNAL, json=_scenario_payload(evidence["id"], saved["id"]))
    out = scenario.json()
    verified = _market_context(app, client, saved["id"])
    assert verified["market_context_status"] == "VERIFIED"

    _seed_outcomes(
        client,
        out["event_identity"],
        [
            [{
                "exchange_at": "2026-09-08T15:30:00+09:00",
                "known_at": "2026-09-08T15:30:00+09:00",
                "open_krw": 100.0,
                "high_krw": 112.0,
                "low_krw": 99.0,
                "close_krw": 110.0,
                "volume": 1000.0,
                "symbol": "005930",
            }],
            [{
                "exchange_at": "2026-09-09T15:30:00+09:00",
                "known_at": "2026-09-09T15:30:00+09:00",
                "open_krw": 100.0,
                "high_krw": 108.0,
                "low_krw": 99.0,
                "close_krw": 101.0,
                "volume": 1000.0,
                "symbol": "005930",
            }],
            [{
                "exchange_at": "2026-09-10T15:30:00+09:00",
                "known_at": "2026-09-10T15:30:00+09:00",
                "open_krw": 100.0,
                "high_krw": 110.0,
                "low_krw": 99.0,
                "close_krw": 109.0,
                "volume": 1000.0,
                "symbol": "005930",
            }],
        ],
    )

    forbidden = client.post(
        "/api/internal/hybrid/calibrations",
        headers=INTERNAL,
        json={**_calibration_payload(out["id"]), "policy_hash": "forged", "good_probability": 0.99},
    )
    assert forbidden.status_code == 422

    created = client.post("/api/internal/hybrid/calibrations", headers=INTERNAL, json=_calibration_payload(out["id"]))
    assert created.status_code == 200, created.text
    snapshot = created.json()
    assert snapshot["eligible"] is True
    assert snapshot["oos"]["count"] == 2
    assert snapshot["is"]["count"] == 1
    assert snapshot["holdout_reuse_count"] == 1

    reused = client.post("/api/internal/hybrid/calibrations", headers=INTERNAL, json=_calibration_payload(out["id"], holdout_key="holdout-a"))
    assert reused.status_code == 200
    reused_body = reused.json()
    assert reused_body["holdout_reuse_count"] == 2
    assert reused_body["eligible"] is False
    assert "holdout reused" in reused_body["failure_reasons"]


def test_hybrid_evaluation_happy_path_invalidated_dominates_and_readback_has_no_side_effects(app_client):
    app, client = app_client
    db = dbmod.connect()
    evidence, filt, saved = _make_lineage(db, key="hybrid-eval")
    db.commit()
    db.close()

    scenario = client.post("/api/internal/scenario-sets", headers=INTERNAL, json=_scenario_payload(evidence["id"], saved["id"]))
    out = scenario.json()
    verified = _market_context(app, client, saved["id"])
    assert verified["market_context_status"] == "VERIFIED"

    _seed_outcomes(
        client,
        out["event_identity"],
        [
            [{
                "exchange_at": "2026-09-08T15:30:00+09:00",
                "known_at": "2026-09-08T15:30:00+09:00",
                "open_krw": 100.0,
                "high_krw": 112.0,
                "low_krw": 99.0,
                "close_krw": 110.0,
                "volume": 1000.0,
                "symbol": "005930",
            }],
            [{
                "exchange_at": "2026-09-09T15:30:00+09:00",
                "known_at": "2026-09-09T15:30:00+09:00",
                "open_krw": 100.0,
                "high_krw": 112.0,
                "low_krw": 99.0,
                "close_krw": 110.0,
                "volume": 1000.0,
                "symbol": "005930",
            }],
            [{
                "exchange_at": "2026-09-10T15:30:00+09:00",
                "known_at": "2026-09-10T15:30:00+09:00",
                "open_krw": 100.0,
                "high_krw": 105.0,
                "low_krw": 99.0,
                "close_krw": 101.0,
                "volume": 1000.0,
                "symbol": "005930",
            }],
        ],
    )

    calibration = client.post("/api/internal/hybrid/calibrations", headers=INTERNAL, json=_calibration_payload(out["id"]))
    assert calibration.status_code == 200, calibration.text
    snapshot = calibration.json()
    assert snapshot["good"]["probability"] >= snapshot["good"]["lower_bound"]

    evaluation = client.post(f"/api/internal/cards/{saved['id']}/hybrid-evaluation", headers=INTERNAL, json=_evaluate_payload(snapshot["id"]))
    assert evaluation.status_code == 200, evaluation.text
    body = evaluation.json()
    assert body["final_state"] == "GOOD"
    assert body["recommendation"] == "BUY_REVIEW"
    assert body["recommendation_only"] is True
    assert body["policy"]["identity"] == POLICY_ID
    assert body["market_context"]["market_context_status"] == "VERIFIED"
    assert body["calibration_snapshot"]["eligible"] is True

    db = dbmod.connect()
    try:
        assert {table: db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in ("order_plans", "order_fills", "positions", "order_events", "live_dry_run_receipts")} == {"order_plans": 0, "order_fills": 0, "positions": 0, "order_events": 0, "live_dry_run_receipts": 0}
    finally:
        db.close()

    db = dbmod.connect()
    db.execute("UPDATE decision_cards SET invalidated_at=? WHERE id=?", ("2026-09-07T09:06:00+09:00", saved["id"]))
    db.commit()
    db.close()
    invalidated = client.post(f"/api/internal/cards/{saved['id']}/hybrid-evaluation", headers=INTERNAL, json=_evaluate_payload(snapshot["id"]))
    assert invalidated.status_code == 200
    assert invalidated.json()["final_state"] == "BAD"
    assert invalidated.json()["recommendation"] == "REDUCE_REVIEW"

    readback = client.get(f"/api/cards/{saved['id']}")
    assert readback.status_code == 200
    assert readback.json()["hybrid_decision"]["final_state"] == "BAD"
    assert readback.json()["hybrid_decision"]["recommendation_only"] is True


def test_hybrid_insufficient_samples_holds_without_buy_fallback(app_client):
    app, client = app_client
    db = dbmod.connect()
    evidence, filt, saved = _make_lineage(db, key="hybrid-short")
    db.commit()
    db.close()

    scenario = client.post("/api/internal/scenario-sets", headers=INTERNAL, json=_scenario_payload(evidence["id"], saved["id"]))
    out = scenario.json()
    _market_context(app, client, saved["id"])
    _seed_outcomes(
        client,
        out["event_identity"],
        [[{
            "exchange_at": "2026-09-08T15:30:00+09:00",
            "known_at": "2026-09-08T15:30:00+09:00",
            "open_krw": 100.0,
            "high_krw": 112.0,
            "low_krw": 99.0,
            "close_krw": 110.0,
            "volume": 1000.0,
            "symbol": "005930",
        }]],
    )
    calibration = client.post("/api/internal/hybrid/calibrations", headers=INTERNAL, json=_calibration_payload(out["id"], oos_start="2026-09-10T00:00:00+09:00", oos_end="2026-09-11T00:00:00+09:00"))
    assert calibration.status_code == 200, calibration.text
    snapshot = calibration.json()
    assert snapshot["eligible"] is False
    assert snapshot["failure_reasons"]
    evaluation = client.post(f"/api/internal/cards/{saved['id']}/hybrid-evaluation", headers=INTERNAL, json=_evaluate_payload(snapshot["id"]))
    assert evaluation.status_code == 200
    body = evaluation.json()
    assert body["final_state"] == "HOLD"
    assert body["recommendation"] == "HOLD_INSUFFICIENT_EVIDENCE"
