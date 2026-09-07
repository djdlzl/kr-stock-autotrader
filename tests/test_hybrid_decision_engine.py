"""Hybrid second-stage decision engine contracts."""

import os
import tempfile
from copy import deepcopy
from datetime import datetime, timedelta
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
BASE_CASE_AT = datetime(2026, 9, 8, 15, 30, tzinfo=ZoneInfo("Asia/Seoul"))


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


def _market_context(app, client, card_id, *, symbol="005930", last_price=100.0, best_bid=None, best_ask=None, top_bid_qty=20.0, top_ask_qty=20.0):
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
    best_bid = last_price - 0.04 if best_bid is None else best_bid
    best_ask = last_price if best_ask is None else best_ask
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
        "last_price": last_price,
        "best_bid": best_bid,
        "best_ask": best_ask,
        "top_bid_qty": top_bid_qty,
        "top_ask_qty": top_ask_qty,
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


def _case_row(*, exchange_at, known_at, open_krw=100.0, high_krw=112.0, low_krw=99.0, close_krw=110.0, volume=1000.0, symbol="005930"):
    return {
        "exchange_at": exchange_at,
        "known_at": known_at,
        "open_krw": open_krw,
        "high_krw": high_krw,
        "low_krw": low_krw,
        "close_krw": close_krw,
        "volume": volume,
        "symbol": symbol,
    }


def _case_timestamp(offset_days: int) -> str:
    return (BASE_CASE_AT + timedelta(days=offset_days)).isoformat()


def _register_case(app, client, *, index, key="hybrid-cohort", version=1, event_identity_prefix="DART:2026-09-03:hybrid", known_at=None, row=None):
    db = dbmod.connect()
    evidence, filt, saved = _make_lineage(db, key=f"{key}-{index}")
    db.commit()
    db.close()
    scenario = client.post(
        "/api/internal/scenario-sets",
        headers=INTERNAL,
        json=_scenario_payload(evidence["id"], saved["id"], version=version),
    )
    assert scenario.status_code == 200, scenario.text
    body = scenario.json()
    outcome_row = row or _case_row(
        exchange_at=known_at or _case_timestamp(index),
        known_at=known_at or _case_timestamp(index),
    )
    outcome = client.post(
        f"/api/internal/scenario-sets/{body['event_identity']}/hybrid-outcomes",
        headers=INTERNAL,
        json={
            "idempotency_key": f"case-{index}",
            "observation_cutoff_at": "2026-10-15T15:30:00+09:00",
            "bars": [outcome_row],
        },
    )
    assert outcome.status_code == 200, outcome.text
    return body, outcome.json()


def _create_plan(client, scenario_set_id, *, idempotency_key="plan-a", frozen_at="2026-09-07T09:00:00+09:00", is_start="2026-09-08T00:00:00+09:00", is_end="2026-09-18T00:00:00+09:00", oos_start="2026-09-18T00:00:00+09:00", oos_end="2026-10-08T00:00:00+09:00", cutoff_at="2026-10-15T15:30:00+09:00"):
    payload = {
        "idempotency_key": idempotency_key,
        "scenario_set_id": scenario_set_id,
        "policy_identity": POLICY_ID,
        "policy_version": 1,
        "holdout_key": "holdout-a",
        "is_window": {"start": is_start, "end": is_end},
        "oos_window": {"start": oos_start, "end": oos_end},
        "cutoff_at": cutoff_at,
    }
    response = client.post("/api/internal/hybrid/calibration-plans", headers=INTERNAL, json=payload)
    return response


def _create_snapshot(client, plan_id, *, idempotency_key="snapshot-a"):
    response = client.post("/api/internal/hybrid/calibrations", headers=INTERNAL, json={"plan_id": plan_id, "idempotency_key": idempotency_key})
    return response


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


def test_hybrid_missing_scenario_and_empty_bars_are_controlled_4xx(app_client):
    app, client = app_client
    missing = client.post("/api/internal/scenario-sets/DOES-NOT-EXIST/hybrid-outcomes", headers=INTERNAL, json={"idempotency_key": "missing", "observation_cutoff_at": "2026-09-11T15:30:00+09:00", "bars": [_case_row(exchange_at="2026-09-08T15:30:00+09:00", known_at="2026-09-08T15:30:00+09:00")]})
    assert missing.status_code == 404
    db = dbmod.connect()
    try:
        before = {table: db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in ("hybrid_outcome_ledger", "hybrid_calibration_plans", "hybrid_calibration_snapshots", "hybrid_second_stage_evaluations", "order_plans", "order_fills", "positions", "order_events", "live_dry_run_receipts")}
    finally:
        db.close()
    assert before == {"hybrid_outcome_ledger": 0, "hybrid_calibration_plans": 0, "hybrid_calibration_snapshots": 0, "hybrid_second_stage_evaluations": 0, "order_plans": 0, "order_fills": 0, "positions": 0, "order_events": 0, "live_dry_run_receipts": 0}
    db = dbmod.connect()
    evidence, filt, saved = _make_lineage(db, key="hybrid-empty-bars")
    db.commit()
    db.close()
    scenario = client.post("/api/internal/scenario-sets", headers=INTERNAL, json=_scenario_payload(evidence["id"], saved["id"]))
    assert scenario.status_code == 200
    out = scenario.json()
    empty = client.post(f"/api/internal/scenario-sets/{out['event_identity']}/hybrid-outcomes", headers=INTERNAL, json={"idempotency_key": "empty", "observation_cutoff_at": "2026-09-11T15:30:00+09:00", "bars": []})
    assert empty.status_code == 422
    db = dbmod.connect()
    try:
        after = {table: db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in ("hybrid_outcome_ledger", "hybrid_calibration_plans", "hybrid_calibration_snapshots", "hybrid_second_stage_evaluations", "order_plans", "order_fills", "positions", "order_events", "live_dry_run_receipts")}
    finally:
        db.close()
    assert after == before


def test_hybrid_calibration_plan_idempotency_and_collision(app_client):
    app, client = app_client
    db = dbmod.connect()
    evidence, filt, saved = _make_lineage(db, key="hybrid-plan")
    db.commit()
    db.close()
    scenario = client.post("/api/internal/scenario-sets", headers=INTERNAL, json=_scenario_payload(evidence["id"], saved["id"]))
    assert scenario.status_code == 200, scenario.text
    out = scenario.json()
    first = _create_plan(client, out["id"], idempotency_key="plan-key")
    assert first.status_code == 200, first.text
    duplicate = _create_plan(client, out["id"], idempotency_key="plan-key")
    assert duplicate.status_code == 200
    assert duplicate.json()["id"] == first.json()["id"]
    collision = _create_plan(client, out["id"], idempotency_key="plan-key", is_start="2026-09-09T00:00:00+09:00")
    assert collision.status_code == 409


def test_hybrid_calibration_rejects_posthoc_windows_and_target_case_reuse(app_client, monkeypatch):
    app, client = app_client
    db = dbmod.connect()
    evidence, filt, saved = _make_lineage(db, key="hybrid-preregister")
    db.commit()
    db.close()
    scenario = client.post("/api/internal/scenario-sets", headers=INTERNAL, json=_scenario_payload(evidence["id"], saved["id"]))
    out = scenario.json()

    prefreeze_at = "2026-09-08T00:00:01+09:00"
    prefreeze_case, _ = _register_case(app, client, index=1, key="hybrid-preregister", known_at=prefreeze_at)
    assert prefreeze_case["id"]
    import kr_stock_autotrader.hybrid_recommendations as hy

    monkeypatch.setattr(hy, "now", lambda: "2026-09-09T00:00:00+09:00")

    prereg = _create_plan(client, out["id"], idempotency_key="prereg-a")
    assert prereg.status_code == 200, prereg.text
    plan = prereg.json()

    snapshot = _create_snapshot(client, plan["id"])
    assert snapshot.status_code == 422

    replacement = client.post(
        "/api/internal/hybrid/calibrations",
        headers=INTERNAL,
        json={"plan_id": plan["id"], "idempotency_key": "snapshot-a", "oos_window": {"start": "2026-09-01T00:00:00+09:00", "end": "2026-09-02T00:00:00+09:00"}},
    )
    assert replacement.status_code == 422


def test_hybrid_independent_cohort_cases_and_target_exclusion(app_client):
    app, client = app_client
    db = dbmod.connect()
    evidence, filt, saved = _make_lineage(db, key="hybrid-independent")
    db.commit()
    db.close()
    target = client.post("/api/internal/scenario-sets", headers=INTERNAL, json=_scenario_payload(evidence["id"], saved["id"]))
    target_body = target.json()

    target_outcome = client.post(
        f"/api/internal/scenario-sets/{target_body['event_identity']}/hybrid-outcomes",
        headers=INTERNAL,
        json={
            "idempotency_key": "target",
            "observation_cutoff_at": "2026-10-15T15:30:00+09:00",
            "bars": [_case_row(exchange_at="2026-09-09T15:30:00+09:00", known_at="2026-09-09T15:30:00+09:00")],
        },
    )
    assert target_outcome.status_code == 200, target_outcome.text

    case_ids = []
    for index in range(1, 4):
        case, _ = _register_case(app, client, index=index, key="hybrid-independent", known_at=f"2026-09-{8 + index:02d}T15:30:00+09:00")
        case_ids.append(case["id"])

    plan = _create_plan(client, target_body["id"], idempotency_key="cohort-plan")
    assert plan.status_code == 200, plan.text
    snapshot = _create_snapshot(client, plan.json()["id"])
    assert snapshot.status_code == 200, snapshot.text
    body = snapshot.json()
    assert body["counts"]["overall"] == 3
    assert target_body["id"] not in body["lineage"]["case_ids"]
    assert sorted(body["lineage"]["case_ids"]) == sorted(case_ids)


def test_hybrid_requires_30_distinct_cases_not_30_rows_from_one_case(app_client):
    app, client = app_client
    db = dbmod.connect()
    evidence, filt, saved = _make_lineage(db, key="hybrid-distinct")
    db.commit()
    db.close()
    scenario = client.post("/api/internal/scenario-sets", headers=INTERNAL, json=_scenario_payload(evidence["id"], saved["id"]))
    out = scenario.json()
    accepted = 0
    rejected = 0
    for index in range(30):
        response = client.post(
            f"/api/internal/scenario-sets/{out['event_identity']}/hybrid-outcomes",
            headers=INTERNAL,
            json={
                "idempotency_key": f"row-{index}",
                "observation_cutoff_at": "2026-10-15T15:30:00+09:00",
                "bars": [_case_row(exchange_at=_case_timestamp(index), known_at=_case_timestamp(index))],
            },
        )
        if index == 0:
            assert response.status_code == 200, response.text
            accepted += 1
        else:
            assert response.status_code == 409
            rejected += 1
    plan = _create_plan(client, out["id"], idempotency_key="distinct-plan")
    assert plan.status_code == 200, plan.text
    snapshot = _create_snapshot(client, plan.json()["id"])
    assert snapshot.status_code == 200, snapshot.text
    body = snapshot.json()
    assert body["counts"]["overall"] == 0
    assert body["eligible"] is False
    assert accepted == 1 and rejected == 29
    assert "insufficient overall sample" in body["failure_reasons"]


def test_hybrid_costs_can_downgrade_gross_good_to_net_base(app_client):
    app, client = app_client
    db = dbmod.connect()
    evidence, filt, saved = _make_lineage(db, key="hybrid-cost")
    db.commit()
    db.close()
    scenario = client.post("/api/internal/scenario-sets", headers=INTERNAL, json=_scenario_payload(evidence["id"], saved["id"]))
    out = scenario.json()
    good_band = next(item["per_share_value_range_krw"] for item in out["scenarios"] if item["label"] == "GOOD")
    gross_price = good_band["low"] + 0.05
    response = client.post(
        f"/api/internal/scenario-sets/{out['event_identity']}/hybrid-outcomes",
        headers=INTERNAL,
        json={
            "idempotency_key": "cost-case",
            "observation_cutoff_at": "2026-10-15T15:30:00+09:00",
            "bars": [
                _case_row(
                    exchange_at="2026-09-08T15:30:00+09:00",
                    known_at="2026-09-08T15:30:00+09:00",
                    open_krw=gross_price,
                    high_krw=gross_price,
                    low_krw=good_band["low"] - 0.5,
                    close_krw=gross_price,
                )
            ],
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["gross_label"] == "GOOD"
    assert body["realized_label"] in {"BASE", "BAD"}
    assert body["net_return_bps"] < body["gross_return_bps"]


def test_hybrid_policy_hash_drift_and_recommendation_only_false_are_rejected(app_client, monkeypatch):
    app, client = app_client
    db = dbmod.connect()
    evidence, filt, saved = _make_lineage(db, key="hybrid-drift")
    db.commit()
    db.close()
    scenario = client.post("/api/internal/scenario-sets", headers=INTERNAL, json=_scenario_payload(evidence["id"], saved["id"]))
    out = scenario.json()
    plan = _create_plan(client, out["id"], idempotency_key="drift-plan")
    assert plan.status_code == 200, plan.text
    snapshot = _create_snapshot(client, plan.json()["id"])
    assert snapshot.status_code == 200, snapshot.text
    forged = client.post(
        f"/api/internal/cards/{saved['id']}/hybrid-evaluation",
        headers=INTERNAL,
        json={"policy_identity": POLICY_ID, "policy_version": 1, "calibration_snapshot_id": snapshot.json()["id"], "recommendation_only": False},
    )
    assert forged.status_code == 422
    import kr_stock_autotrader.hybrid_recommendations as hy

    original = hy._policy_spec()
    mutated = {**original, "market_context_requirements": {**original["market_context_requirements"], "max_spread_pct": 0.04}}
    monkeypatch.setattr(hy, "_policy_spec", lambda: mutated)
    drift = _create_plan(client, out["id"], idempotency_key="drift-plan-2")
    assert drift.status_code == 409


def test_hybrid_min_top_of_book_qty_blocks_buy_review(app_client):
    app, client = app_client
    db = dbmod.connect()
    evidence, filt, saved = _make_lineage(db, key="hybrid-topbook")
    db.commit()
    db.close()
    scenario = client.post("/api/internal/scenario-sets", headers=INTERNAL, json=_scenario_payload(evidence["id"], saved["id"]))
    out = scenario.json()
    for index in range(30):
        case, _ = _register_case(app, client, index=index + 1, key="hybrid-topbook", known_at=_case_timestamp(index))
        assert case["id"]
    _market_context(app, client, saved["id"], symbol="005930", top_bid_qty=0.1, top_ask_qty=0.1)
    plan = _create_plan(client, out["id"], idempotency_key="topbook-plan")
    assert plan.status_code == 200, plan.text
    snapshot = _create_snapshot(client, plan.json()["id"])
    assert snapshot.status_code == 200, snapshot.text
    evaluation = client.post(
        f"/api/internal/cards/{saved['id']}/hybrid-evaluation",
        headers=INTERNAL,
        json={"policy_identity": POLICY_ID, "policy_version": 1, "calibration_snapshot_id": snapshot.json()["id"], "recommendation_only": True},
    )
    assert evaluation.status_code == 200, evaluation.text
    assert evaluation.json()["recommendation"] != "BUY_REVIEW"


def test_hybrid_normal_persisted_market_context_exposes_top_book_depth_and_allows_buy_review(app_client):
    app, client = app_client
    db = dbmod.connect()
    evidence, filt, saved = _make_lineage(db, key="hybrid-topbook-positive")
    db.commit()
    db.close()
    scenario = client.post("/api/internal/scenario-sets", headers=INTERNAL, json=_scenario_payload(evidence["id"], saved["id"]))
    out = scenario.json()
    good_band = next(item["per_share_value_range_krw"] for item in out["scenarios"] if item["label"] == "GOOD")
    for index in range(30):
        good_price = good_band["low"] + 0.5
        row = _case_row(
            exchange_at=_case_timestamp(index),
            known_at=_case_timestamp(index),
            open_krw=good_price,
            high_krw=good_price + 0.1,
            low_krw=good_price - 0.1,
            close_krw=good_price,
            volume=1000.0,
        )
        case, _ = _register_case(app, client, index=index + 1, key="hybrid-topbook-positive", known_at=_case_timestamp(index), row=row)
        assert case["id"]
    price = (good_band["low"] + good_band["high"]) / 2
    market_context = _market_context(app, client, saved["id"], symbol="005930", last_price=price, best_bid=price - 0.04, best_ask=price, top_bid_qty=20.0, top_ask_qty=20.0)
    assert market_context["metrics"]["top_bid_qty"] == 20.0
    assert market_context["metrics"]["top_ask_qty"] == 20.0
    plan = _create_plan(client, out["id"], idempotency_key="topbook-positive-plan")
    assert plan.status_code == 200, plan.text
    snapshot = _create_snapshot(client, plan.json()["id"])
    assert snapshot.status_code == 200, snapshot.text
    evaluation = client.post(
        f"/api/internal/cards/{saved['id']}/hybrid-evaluation",
        headers=INTERNAL,
        json={"policy_identity": POLICY_ID, "policy_version": 1, "calibration_snapshot_id": snapshot.json()["id"], "recommendation_only": True},
    )
    assert evaluation.status_code == 200, evaluation.text
    body = evaluation.json()
    assert body["recommendation"] == "BUY_REVIEW"
    assert body["final_state"] == "GOOD"
    assert body["structural_good_compatibility"]["status"] == "GOOD_COMPATIBLE"
    assert body["structural_good_compatibility"]["inputs"]["top_bid_qty"] == 20.0
    assert body["structural_good_compatibility"]["inputs"]["top_ask_qty"] == 20.0


def test_hybrid_cost_adjusted_entry_outside_frozen_good_band_blocks_buy_review(app_client):
    app, client = app_client
    db = dbmod.connect()
    evidence, filt, saved = _make_lineage(db, key="hybrid-cost-guardrail")
    db.commit()
    db.close()
    scenario = client.post("/api/internal/scenario-sets", headers=INTERNAL, json=_scenario_payload(evidence["id"], saved["id"]))
    out = scenario.json()
    good_band = next(item["per_share_value_range_krw"] for item in out["scenarios"] if item["label"] == "GOOD")
    for index in range(30):
        good_price = good_band["low"] + 0.5
        row = _case_row(
            exchange_at=_case_timestamp(index),
            known_at=_case_timestamp(index),
            open_krw=good_price,
            high_krw=good_price + 0.1,
            low_krw=good_price - 0.1,
            close_krw=good_price,
            volume=1000.0,
        )
        case, _ = _register_case(app, client, index=index + 1, key="hybrid-cost-guardrail", known_at=_case_timestamp(index), row=row)
        assert case["id"]
    price = good_band["high"] - 0.001
    market_context = _market_context(app, client, saved["id"], symbol="005930", last_price=price, best_bid=price - 0.04, best_ask=price, top_bid_qty=20.0, top_ask_qty=20.0)
    assert market_context["metrics"]["top_bid_qty"] == 20.0
    assert market_context["metrics"]["top_ask_qty"] == 20.0
    plan = _create_plan(client, out["id"], idempotency_key="cost-guardrail-plan")
    assert plan.status_code == 200, plan.text
    snapshot = _create_snapshot(client, plan.json()["id"])
    assert snapshot.status_code == 200, snapshot.text
    evaluation = client.post(
        f"/api/internal/cards/{saved['id']}/hybrid-evaluation",
        headers=INTERNAL,
        json={"policy_identity": POLICY_ID, "policy_version": 1, "calibration_snapshot_id": snapshot.json()["id"], "recommendation_only": True},
    )
    assert evaluation.status_code == 200, evaluation.text
    body = evaluation.json()
    assert body["recommendation"] != "BUY_REVIEW"
    assert body["final_state"] != "GOOD"
    assert body["structural_good_compatibility"]["status"] != "GOOD_COMPATIBLE"
    assert "cost_adjusted_entry_outside_frozen_good_band" in body["structural_good_compatibility"]["reasons"]


def test_hybrid_invalidation_dominates_and_forbidden_tables_remain_zero(app_client):
    app, client = app_client
    db = dbmod.connect()
    evidence, filt, saved = _make_lineage(db, key="hybrid-invalidation")
    db.commit()
    db.close()
    scenario = client.post("/api/internal/scenario-sets", headers=INTERNAL, json=_scenario_payload(evidence["id"], saved["id"]))
    out = scenario.json()
    for index in range(30):
        _register_case(app, client, index=index + 1, key="hybrid-invalidation", known_at=_case_timestamp(index))
    _market_context(app, client, saved["id"], symbol="005930")
    plan = _create_plan(client, out["id"], idempotency_key="invalid-plan")
    assert plan.status_code == 200, plan.text
    snapshot = _create_snapshot(client, plan.json()["id"])
    assert snapshot.status_code == 200, snapshot.text
    db = dbmod.connect()
    db.execute("UPDATE decision_cards SET invalidated_at=? WHERE id=?", ("2026-09-07T09:06:00+09:00", saved["id"]))
    db.commit()
    db.close()
    evaluation = client.post(
        f"/api/internal/cards/{saved['id']}/hybrid-evaluation",
        headers=INTERNAL,
        json={"policy_identity": POLICY_ID, "policy_version": 1, "calibration_snapshot_id": snapshot.json()["id"], "recommendation_only": True},
    )
    assert evaluation.status_code == 200
    assert evaluation.json()["final_state"] == "BAD"
    db = dbmod.connect()
    try:
        assert {table: db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in ("order_plans", "order_fills", "positions", "order_events", "live_dry_run_receipts")} == {"order_plans": 0, "order_fills": 0, "positions": 0, "order_events": 0, "live_dry_run_receipts": 0}
    finally:
        db.close()
