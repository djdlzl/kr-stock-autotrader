import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import threading
from zoneinfo import ZoneInfo
import tempfile

from fastapi.testclient import TestClient

from kr_stock_autotrader import db as dbmod
from kr_stock_autotrader.decision_cards import create_evidence, save_card, save_filter
from kr_stock_autotrader.intraday_market_context import evaluate_intraday_market_context, persist_intraday_market_context_run

from tests.test_decision_card_invariants import card, raw


KST = ZoneInfo("Asia/Seoul")
AS_OF = "2026-09-07T09:05:00+09:00"
KNOWN = "2026-09-07T08:00:00+09:00"


class Response:
    def __init__(self, payload, status_code=200):
        self.payload = payload
        self.status_code = status_code

    def json(self):
        return self.payload


class FakeTransport:
    def __init__(self, responses=None):
        self.calls = []
        self.responses = list(responses or [])

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if self.responses:
            return self.responses.pop(0)
        return Response({"rt_cd": "0", "output1": {"access_token": "x"}})


def _minute_rows(*, prices, volumes, requested_as_of):
    if isinstance(requested_as_of, datetime):
        requested_dt = requested_as_of
    else:
        requested_dt = datetime.fromisoformat(str(requested_as_of).replace("Z", "+00:00"))
    requested_day = requested_dt.astimezone(KST).strftime("%Y%m%d")
    rows = []
    running_value = 0
    for hhmmss, price, volume in zip(("090000", "090100", "090200", "090300", "090400", "090500"), prices, volumes):
        running_value += int(price) * int(volume)
        rows.append(
            {
                "stck_bsop_date": requested_day,
                "stck_cntg_hour": hhmmss,
                "stck_prpr": str(price),
                "stck_oprc": str(prices[0]),
                "stck_hgpr": str(price),
                "stck_lwpr": str(price),
                "cntg_vol": str(volume),
                "acml_tr_pbmn": str(running_value),
            }
        )
    return rows


def _intraday_snapshot(symbol, *, prices, volumes, retrieved_at, requested_as_of=None):
    from kr_stock_autotrader.kis_readonly import project_intraday_minute_snapshot

    requested_as_of = requested_as_of or retrieved_at
    rows = _minute_rows(prices=prices, volumes=volumes, requested_as_of=requested_as_of)
    if isinstance(requested_as_of, datetime):
        requested_dt = requested_as_of
    else:
        requested_dt = datetime.fromisoformat(str(requested_as_of).replace("Z", "+00:00"))
    return project_intraday_minute_snapshot(
        {"rt_cd": "0", "output2": rows},
        symbol=symbol,
        requested_as_of=requested_dt,
        retrieved_at=retrieved_at,
        provider="KIS",
        tr_id="FHKST03010200",
    )


def _seed_lineage(db):
    evidence = create_evidence(
        db,
        {
            "symbol": "005930",
            "name": "삼성전자",
            "kind": "disclosure",
            "title": "intraday context",
            "summary": "fixture",
            "source": "dart",
            "source_url": "https://example.test/e",
            "announcement_at": KNOWN,
            "collected_at": KNOWN,
            "known_at": KNOWN,
            "snapshot": {},
            "dedupe_key": "intraday-context-evidence",
        },
    )
    filt = save_filter(db, evidence["id"], raw(announcement_at=KNOWN, market_data_known_at=KNOWN), KNOWN, KNOWN)
    saved = save_card(db, card(evidence["id"], filt["id"]))
    return evidence, filt, saved


def _patch_lineage_context(db, *, filter_id, benchmark_symbol="229200", previous_close_krw=None):
    payload = db.execute("SELECT computed_outputs FROM deterministic_filter_results WHERE id=?", (filter_id,)).fetchone()[0]
    computed = json.loads(payload)
    computed.setdefault("computed", {})
    computed["computed"]["benchmark_symbol"] = benchmark_symbol
    if previous_close_krw is not None:
        computed["computed"]["previous_close_krw"] = previous_close_krw
    db.execute("UPDATE deterministic_filter_results SET computed_outputs=? WHERE id=?", (json.dumps(computed, ensure_ascii=False, separators=(',', ':'), sort_keys=True), filter_id))


def _persist_history_run(db, *, run_key, evidence, filt, saved, as_of, stock_volumes, benchmark_volumes, previous_close_krw=69500.0):
    snapshot_at = datetime.fromisoformat(as_of.replace("Z", "+00:00")).replace(hour=9, minute=5, second=2, microsecond=0)
    stock_snapshot = _intraday_snapshot(
        evidence["symbol"],
        prices=[70000, 70000, 70000, 70000, 70000, 70000],
        volumes=stock_volumes,
        retrieved_at=snapshot_at,
        requested_as_of=as_of,
    )
    benchmark_snapshot = _intraday_snapshot(
        "229200",
        prices=[50000, 50000, 50000, 50000, 50000, 50000],
        volumes=benchmark_volumes,
        retrieved_at=snapshot_at,
        requested_as_of=as_of,
    )
    orderbook = {
        "status": "ok",
        "symbol": evidence["symbol"],
        "last_price": 70000.0,
        "best_bid": 70000.0,
        "best_ask": 70000.0,
        "top_bid_qty": 10.0,
        "top_ask_qty": 10.0,
        "quote_known_at": snapshot_at.isoformat(),
        "retrieved_at": snapshot_at.isoformat(),
        "timestamp_source": "network_retrieved_at",
        "source": "KIS",
        "environment": "production",
        "status": "ok",
    }
    result = evaluate_intraday_market_context(
        stock_snapshot=stock_snapshot,
        benchmark_snapshot=benchmark_snapshot,
        orderbook=orderbook,
        same_time_history=[],
        previous_close_krw=previous_close_krw,
    )
    result["known_at"] = snapshot_at.isoformat()
    result["retrieved_at"] = snapshot_at.isoformat()
    persist_intraday_market_context_run(
        db,
        run_key=run_key,
        card=saved,
        evidence=evidence,
        filter_result=filt,
        requested_as_of=as_of,
        stock_snapshot=stock_snapshot,
        benchmark_snapshot=benchmark_snapshot,
        orderbook=orderbook,
        result=result,
    )


def _prepare_client(monkeypatch, tmp_path, *, benchmark_symbol="229200", previous_close_krw=None):
    monkeypatch.setattr(dbmod, "DATABASE_PATH", str(tmp_path))
    monkeypatch.setenv("INTERNAL_API_KEY", "market-context-key")
    from app import app

    client = TestClient(app)
    assert client.post("/api/signup", json={"email": f"market-context-{tmp_path.stem}@test.com", "password": "long-password"}).status_code == 200
    db = dbmod.connect()
    evidence, filt, saved = _seed_lineage(db)
    _patch_lineage_context(db, filter_id=filt["id"], benchmark_symbol=benchmark_symbol, previous_close_krw=previous_close_krw)
    db.commit()
    db.close()
    return app, client, evidence, filt, saved


def test_0905_market_context_route_persists_reads_back_and_keeps_zero_order_side_effects(monkeypatch, tmp_path):
    monkeypatch.setattr(dbmod, "DATABASE_PATH", str(tmp_path / "market-context.db"))
    monkeypatch.setenv("INTERNAL_API_KEY", "market-context-key")
    from app import app

    client = TestClient(app)
    assert client.post("/api/signup", json={"email": "market-context@test.com", "password": "long-password"}).status_code == 200
    db = dbmod.connect()
    evidence, filt, saved = _seed_lineage(db)
    _patch_lineage_context(db, filter_id=filt["id"], benchmark_symbol="229200")
    db.commit()
    db.close()

    def intraday_provider(symbol, as_of):
        snapshot_at = datetime(2026, 9, 7, 9, 5, 2, tzinfo=KST)
        if symbol == "005930":
            return _intraday_snapshot(symbol, prices=[70000, 70000, 70000, 70000, 70000, 70000], volumes=[10, 10, 10, 10, 10, 10], retrieved_at=snapshot_at, requested_as_of=as_of)
        if symbol == "229200":
            return _intraday_snapshot(symbol, prices=[70000, 70000, 70000, 70000, 70000, 70000], volumes=[10, 10, 10, 10, 10, 10], retrieved_at=snapshot_at, requested_as_of=as_of)
        raise AssertionError(symbol)

    app.state.kis_orderbook_provider = lambda symbol: {"status": "ok", "symbol": symbol, "last_price": 70000.0, "best_bid": 70000.0, "best_ask": 70000.0, "top_bid_qty": 10.0, "top_ask_qty": 10.0, "quote_known_at": "2026-09-07T09:05:02+09:00", "retrieved_at": "2026-09-07T09:05:02+09:00", "timestamp_source": "network_retrieved_at", "source": "KIS", "environment": "production", "status": "ok"}
    app.state.kis_intraday_minute_provider = intraday_provider

    run_key = "market-context-2026-09-07-0905-kst-topic7923"
    response = client.post(f"/api/internal/cards/{saved['id']}/market-context", headers={"X-Internal-API-Key": "market-context-key"}, json={"run_key": run_key, "as_of": AS_OF})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["run_key"] == run_key
    assert body["idempotent"] is False
    assert body["card_id"] == saved["id"]
    assert body["evidence_id"] == evidence["id"]
    assert body["filter_id"] == filt["id"]
    assert body["source_topic"] == "mac:7923"
    assert body["expected_price"]["run_key"] == run_key + "-expected-price"
    assert body["expected_price"]["card_id"] == saved["id"]
    assert body["expected_price"]["evidence_id"] == evidence["id"]
    assert body["expected_price"]["filter_id"] == filt["id"]
    assert body["expected_price"]["requested_as_of"] == AS_OF
    assert body["expected_price"]["status"] == "HOLD_MISSING_INPUT"
    assert body["expected_price"]["result"]["status"] == "HOLD_MISSING_INPUT"
    assert body["market_context_status"] == "MARKET_CONTEXT_HOLD"
    assert body["metrics"]["open_price_krw"] == 70000.0
    assert body["metrics"]["last_price_krw"] == 70000.0
    assert body["metrics"]["open_to_current_return_pct"] == 0.0
    assert body["metrics"]["benchmark_excess_pct"] == 0.0
    assert body["metrics"]["spread_pct"] == 0.0
    assert body["metrics"]["top_of_book_imbalance"] == 0.0
    assert body["metrics"]["same_time_baseline_status"] == "INSUFFICIENT_HISTORY"
    assert body["metrics"]["same_time_baseline_volume_ratio"] is None
    assert body["metrics"]["previous_close_gap_status"] == "INSUFFICIENT_LINEAGE"
    assert body["metrics"]["previous_close_gap_pct"] is None
    assert body["metrics"]["volume_window_status"] == "IN_PROGRESS_CURRENT_MINUTE"
    assert body["metrics"]["cumulative_volume_0900_to_as_of"] == 50.0
    assert body["metrics"]["latest_completed_interval_velocity"] == 0.0
    assert body["metrics"]["latest_completed_interval_acceleration"] == 0.0

    readback = client.get(f"/api/internal/market-context-runs/{run_key}", headers={"X-Internal-API-Key": "market-context-key"})
    assert readback.status_code == 200
    assert readback.json()["run_key"] == run_key
    assert readback.json()["expected_price"] == {key: value for key, value in body["expected_price"].items() if key != "idempotent"}
    card_detail = client.get(f"/api/cards/{saved['id']}")
    assert card_detail.status_code == 200
    assert card_detail.json()["market_context"]["run_key"] == run_key
    assert card_detail.json()["market_context"]["market_context_status"] == "MARKET_CONTEXT_HOLD"

    retry = client.post(f"/api/internal/cards/{saved['id']}/market-context", headers={"X-Internal-API-Key": "market-context-key"}, json={"run_key": run_key, "as_of": AS_OF})
    assert retry.status_code == 200 and retry.json()["idempotent"] is True
    assert retry.json()["expected_price"]["idempotent"] is True
    mismatch = client.post(f"/api/internal/cards/{saved['id']}/market-context", headers={"X-Internal-API-Key": "market-context-key"}, json={"run_key": run_key, "as_of": "2026-09-07T09:06:00+09:00"})
    assert mismatch.status_code == 409

    db = dbmod.connect()
    try:
        assert db.execute("SELECT COUNT(*) FROM intraday_market_context_runs").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM expected_price_runs").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM intraday_market_context_observations").fetchone()[0] >= 3
        assert {table: db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in ("order_plans", "order_fills", "positions", "order_events")} == {"order_plans": 0, "order_fills": 0, "positions": 0, "order_events": 0}
    finally:
        db.close()

    from kr_stock_autotrader.domain import previous_krx_business_dates
    from kr_stock_autotrader.kis_readonly import DailySnapshot

    def daily_provider(symbol, as_of):
        dates = previous_krx_business_dates(as_of.astimezone(KST).date(), 30)
        rows = tuple(
            {
                "stck_bsop_date": day.strftime("%Y%m%d"),
                "stck_clpr": str(100000 - idx),
                "acml_vol": "1000",
                "acml_tr_pbmn": "100000000",
            }
            for idx, day in enumerate(dates)
        )
        return DailySnapshot(summary_market_cap_100m=25.0, bars=rows, retrieved_at=datetime(2026, 9, 7, 8, 0, 3, tzinfo=KST))

    app.state.kis_daily_snapshot_provider = daily_provider
    legacy = client.post("/api/internal/market-snapshots/005930", headers={"X-Internal-API-Key": "market-context-key"}, json={"as_of": KNOWN, "announcement_at": KNOWN})
    assert legacy.status_code == 200
    assert legacy.json()["snapshot"]["status"] == "ok"


def test_0905_market_context_route_fails_closed_without_benchmark_alignment(monkeypatch, tmp_path):
    monkeypatch.setattr(dbmod, "DATABASE_PATH", str(tmp_path / "market-context-unavailable.db"))
    monkeypatch.setenv("INTERNAL_API_KEY", "market-context-key")
    from app import app

    client = TestClient(app)
    assert client.post("/api/signup", json={"email": "market-context-unavailable@test.com", "password": "long-password"}).status_code == 200
    db = dbmod.connect()
    evidence, filt, saved = _seed_lineage(db)
    _patch_lineage_context(db, filter_id=filt["id"], benchmark_symbol="229200")
    db.commit()
    db.close()

    app.state.kis_orderbook_provider = lambda symbol: {"status": "ok", "symbol": symbol, "last_price": 70000.0, "best_bid": 70000.0, "best_ask": 70000.0, "top_bid_qty": 10.0, "top_ask_qty": 10.0, "quote_known_at": "2026-09-07T09:05:02+09:00", "retrieved_at": "2026-09-07T09:05:02+09:00", "timestamp_source": "network_retrieved_at", "source": "KIS", "environment": "production", "status": "ok"}
    def broken_intraday(symbol, as_of):
        from kr_stock_autotrader.kis_readonly import project_intraday_minute_snapshot

        snapshot_at = datetime(2026, 9, 7, 9, 5, 2, tzinfo=KST)
        if symbol == "005930":
            return _intraday_snapshot(symbol, prices=[70000, 70000, 70000, 70000, 70000, 70000], volumes=[10, 10, 10, 10, 10, 10], retrieved_at=snapshot_at, requested_as_of=as_of)
        return project_intraday_minute_snapshot(
            {"rt_cd": "0", "output2": _minute_rows(prices=[70000, 70000, 70000], volumes=[10, 10, 10], requested_as_of=as_of)},
            symbol=symbol,
            requested_as_of=datetime.fromisoformat(as_of.replace("Z", "+00:00")),
            retrieved_at=snapshot_at,
            provider="KIS",
            tr_id="FHKST03010200",
        )
    app.state.kis_intraday_minute_provider = broken_intraday

    response = client.post(f"/api/internal/cards/{saved['id']}/market-context", headers={"X-Internal-API-Key": "market-context-key"}, json={"run_key": "market-context-bad-benchmark", "as_of": AS_OF})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["market_context_status"] == "MARKET_CONTEXT_UNAVAILABLE"
    assert "benchmark" in body["reason"].lower() or "alignment" in body["reason"].lower()
    assert body["metrics"]["benchmark_excess_pct"] is None
    assert body["metrics"]["same_time_baseline_status"] == "INSUFFICIENT_HISTORY"


def test_0905_market_context_operational_window_boundaries_and_late_retrieval(monkeypatch, tmp_path):
    app, client, evidence, filt, saved = _prepare_client(monkeypatch, tmp_path / "window.db")

    def provider_factory(retrieved_at):
        def intraday_provider(symbol, as_of):
            if symbol == evidence["symbol"]:
                return _intraday_snapshot(symbol, prices=[70000, 70000, 70000, 70000, 70000, 70000], volumes=[10, 10, 10, 10, 10, 10], retrieved_at=retrieved_at)
            return _intraday_snapshot(symbol, prices=[50000, 50000, 50000, 50000, 50000, 50000], volumes=[10, 10, 10, 10, 10, 10], retrieved_at=retrieved_at)
        return intraday_provider

    def orderbook_factory(retrieved_at):
        return lambda symbol: {"status": "ok", "symbol": symbol, "last_price": 70000.0, "best_bid": 70000.0, "best_ask": 70000.0, "top_bid_qty": 10.0, "top_ask_qty": 10.0, "quote_known_at": retrieved_at.isoformat(), "retrieved_at": retrieved_at.isoformat(), "timestamp_source": "network_retrieved_at", "source": "KIS", "environment": "production", "status": "ok"}

    accepted_at = datetime(2026, 9, 7, 9, 24, 59, 999999, tzinfo=KST)
    app.state.kis_orderbook_provider = orderbook_factory(accepted_at)
    app.state.kis_intraday_minute_provider = provider_factory(accepted_at)
    accepted = client.post(f"/api/internal/cards/{saved['id']}/market-context", headers={"X-Internal-API-Key": "market-context-key"}, json={"run_key": "market-context-window-ok", "as_of": accepted_at.isoformat()})
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["market_context_status"] == "MARKET_CONTEXT_HOLD"
    assert accepted.json()["requested_as_of"] == accepted_at.isoformat()
    assert {item["retrieved_at"] for item in accepted.json()["observations"]} == {accepted_at.isoformat()}

    for as_of, label in (
        ("2026-09-07T09:04:59+09:00", "backdated"),
        ("2026-09-07T09:25:00+09:00", "future"),
        ("2026-09-06T09:05:00+09:00", "nonbusiness"),
    ):
        response = client.post(
            f"/api/internal/cards/{saved['id']}/market-context",
            headers={"X-Internal-API-Key": "market-context-key"},
            json={"run_key": f"market-context-{label}", "as_of": as_of},
        )
        assert response.status_code == 409

    late_at = datetime(2026, 9, 7, 10, 0, tzinfo=KST)
    app.state.kis_orderbook_provider = orderbook_factory(late_at)
    app.state.kis_intraday_minute_provider = provider_factory(late_at)
    late = client.post(
        f"/api/internal/cards/{saved['id']}/market-context",
        headers={"X-Internal-API-Key": "market-context-key"},
        json={"run_key": "market-context-late", "as_of": accepted_at.isoformat()},
    )
    assert late.status_code == 409
    assert "operational window" in late.text.lower()


def test_0905_market_context_same_time_history_and_previous_close_are_authoritative(monkeypatch, tmp_path):
    app, client, evidence, filt, saved = _prepare_client(monkeypatch, tmp_path / "history.db", previous_close_krw=69500.0)
    db = dbmod.connect()
    try:
        for day, volumes in (
            ("2026-09-02T09:05:00+09:00", [2, 4, 6, 8, 10, 12]),
            ("2026-09-03T09:05:00+09:00", [4, 6, 8, 10, 12, 14]),
            ("2026-09-04T09:05:00+09:00", [6, 8, 10, 12, 14, 16]),
        ):
            _persist_history_run(
                db,
                run_key=f"history-{day[:10]}",
                evidence=evidence,
                filt=filt,
                saved=saved,
                as_of=day,
                stock_volumes=volumes,
                benchmark_volumes=volumes,
            )
        db.commit()
    finally:
        db.close()
    accepted_at = datetime(2026, 9, 7, 9, 5, tzinfo=KST)
    app.state.kis_orderbook_provider = lambda symbol: {"status": "ok", "symbol": symbol, "last_price": 70000.0, "best_bid": 70000.0, "best_ask": 70000.0, "top_bid_qty": 10.0, "top_ask_qty": 10.0, "quote_known_at": accepted_at.isoformat(), "retrieved_at": accepted_at.isoformat(), "timestamp_source": "network_retrieved_at", "source": "KIS", "environment": "production", "status": "ok"}
    app.state.kis_intraday_minute_provider = lambda symbol, as_of: _intraday_snapshot(symbol, prices=[70000, 70000, 70000, 70000, 70000, 70000], volumes=[10, 10, 10, 10, 10, 10] if symbol == evidence["symbol"] else [10, 10, 10, 10, 10, 10], retrieved_at=accepted_at, requested_as_of=as_of)
    response = client.post(
        f"/api/internal/cards/{saved['id']}/market-context",
        headers={"X-Internal-API-Key": "market-context-key"},
        json={"run_key": "market-context-history", "as_of": accepted_at.isoformat()},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["market_context_status"] == "VERIFIED"
    assert body["metrics"]["same_time_baseline_status"] == "READY"
    assert body["metrics"]["same_time_baseline_sample_count"] == 3
    assert body["metrics"]["same_time_baseline_cumulative_volume_0900_to_as_of"] == 40.0
    assert body["metrics"]["same_time_baseline_volume_ratio"] == 1.25
    assert body["metrics"]["previous_close_gap_status"] == "VERIFIED"
    assert body["metrics"]["previous_close_gap_pct"] == 0.71942446
    readback = client.get(f"/api/internal/market-context-runs/{body['run_key']}", headers={"X-Internal-API-Key": "market-context-key"})
    assert readback.status_code == 200
    assert readback.json()["metrics"]["same_time_baseline_volume_ratio"] == 1.25
    card_detail = client.get(f"/api/cards/{saved['id']}")
    assert card_detail.status_code == 200
    assert card_detail.json()["market_context"]["market_context_status"] == "VERIFIED"


def test_0905_market_context_idempotency_is_atomic_under_concurrency(monkeypatch, tmp_path):
    app, client, evidence, filt, saved = _prepare_client(monkeypatch, tmp_path / "concurrency.db")
    accepted_at = datetime(2026, 9, 7, 9, 5, tzinfo=KST)
    app.state.kis_orderbook_provider = lambda symbol: {"status": "ok", "symbol": symbol, "last_price": 70000.0, "best_bid": 70000.0, "best_ask": 70000.0, "top_bid_qty": 10.0, "top_ask_qty": 10.0, "quote_known_at": accepted_at.isoformat(), "retrieved_at": accepted_at.isoformat(), "timestamp_source": "network_retrieved_at", "source": "KIS", "environment": "production", "status": "ok"}
    app.state.kis_intraday_minute_provider = lambda symbol, as_of: _intraday_snapshot(symbol, prices=[70000, 70000, 70000, 70000, 70000, 70000], volumes=[10, 10, 10, 10, 10, 10], retrieved_at=accepted_at, requested_as_of=as_of)
    barrier = threading.Barrier(2)

    def go():
        barrier.wait()
        with TestClient(app) as local_client:
            return local_client.post(
                f"/api/internal/cards/{saved['id']}/market-context",
                headers={"X-Internal-API-Key": "market-context-key"},
                json={"run_key": "market-context-concurrent", "as_of": accepted_at.isoformat()},
            )

    with ThreadPoolExecutor(2) as pool:
        responses = list(pool.map(lambda _: go(), range(2)))
    assert sorted(response.status_code for response in responses) == [200, 200]
    assert {response.json()["idempotent"] for response in responses} == {False, True}
    assert len({response.json()["id"] for response in responses}) == 1
    db = dbmod.connect()
    try:
        assert db.execute("SELECT COUNT(*) FROM intraday_market_context_runs").fetchone()[0] == 1
    finally:
        db.close()


def test_0905_market_context_conflicting_run_key_and_as_of_fails_closed(monkeypatch, tmp_path):
    app, client, evidence, filt, saved = _prepare_client(monkeypatch, tmp_path / "conflict.db")
    accepted_at = datetime(2026, 9, 7, 9, 5, tzinfo=KST)
    app.state.kis_orderbook_provider = lambda symbol: {"status": "ok", "symbol": symbol, "last_price": 70000.0, "best_bid": 70000.0, "best_ask": 70000.0, "top_bid_qty": 10.0, "top_ask_qty": 10.0, "quote_known_at": accepted_at.isoformat(), "retrieved_at": accepted_at.isoformat(), "timestamp_source": "network_retrieved_at", "source": "KIS", "environment": "production", "status": "ok"}
    app.state.kis_intraday_minute_provider = lambda symbol, as_of: _intraday_snapshot(symbol, prices=[70000, 70000, 70000, 70000, 70000, 70000], volumes=[10, 10, 10, 10, 10, 10], retrieved_at=accepted_at, requested_as_of=as_of)
    first = client.post(
        f"/api/internal/cards/{saved['id']}/market-context",
        headers={"X-Internal-API-Key": "market-context-key"},
        json={"run_key": "market-context-conflict", "as_of": accepted_at.isoformat()},
    )
    assert first.status_code == 200
    second = client.post(
        f"/api/internal/cards/{saved['id']}/market-context",
        headers={"X-Internal-API-Key": "market-context-key"},
        json={"run_key": "market-context-conflict", "as_of": "2026-09-07T09:05:30+09:00"},
    )
    assert second.status_code == 409
