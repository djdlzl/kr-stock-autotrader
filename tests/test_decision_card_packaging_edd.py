"""Full HTTP/SQLite decision-card paper lifecycle and container asset regression."""
import hashlib
import shutil
import socket
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from kr_stock_autotrader import db as dbmod
from kr_stock_autotrader.config import LIVE_TRADING
from kr_stock_autotrader.decision_cards import PROMPT_PATH, prompt_hash
from tests.test_decision_card_invariants import card, raw


REPO = Path(__file__).parents[1]
INTERNAL = {"X-Internal-API-Key": "test-internal-key"}
AS_OF = "2026-08-31T10:00:00+09:00"
KNOWN = "2026-08-31T09:00:00+09:00"
NONBUY_UNAVAILABLE_ORDER_FIELDS = {
    "price_cap": None, "window": None, "max_amount": None, "max_qty": None,
    "stop_loss": None, "take_profit": [], "evidence_invalidation": "",
    "holding_until": None, "review_at": None, "valid_until": None, "expires": None,
    "order_type": None,
}


def evidence_payload(key, *, title="계약", symbol="005930"):
    return {
        "symbol": symbol, "name": "삼성전자", "kind": "disclosure", "title": title,
        "summary": "계약 체결", "source": "dart", "source_url": "https://dart.fss.or.kr",
        "snapshot": {"notice": key}, "dedupe_key": key, "known_at": KNOWN,
    }


def tick(key, *, price=90, quantity=1, known_at="2026-08-31T10:00:00+09:00"):
    return {
        "tick_key": key, "known_at": known_at, "server_now": "2026-08-31T10:03:00+09:00",
        "price": price, "liquidity": 1, "trading_value": 100_000_000,
        "gap_pct": 1, "fill_qty": quantity,
    }


def create_card(client, evidence_id, filter_id, *, lineage, **overrides):
    payload = card(evidence_id, filter_id)
    payload["lineage_key"] = lineage
    payload["card"].update(overrides)
    response = client.post("/api/internal/cards/results", json=payload, headers=INTERNAL)
    assert response.status_code == 200, response.text
    return response.json()


def test_container_copy_contract_and_runtime_prompt_asset(tmp_path):
    """Docker context must carry every root asset imported by the runtime."""
    dockerfile = (REPO / "Dockerfile").read_text()
    assert "COPY app.py ./" in dockerfile
    assert "COPY kr_stock_autotrader ./kr_stock_autotrader" in dockerfile
    assert "COPY prompts ./prompts" in dockerfile

    # Model exactly the Docker COPY destinations; this catches a prompt omission
    # before a daemon/image build is available on the developer workstation.
    runtime = tmp_path / "app"
    runtime.mkdir()
    shutil.copy2(REPO / "app.py", runtime / "app.py")
    shutil.copytree(REPO / "kr_stock_autotrader", runtime / "kr_stock_autotrader")
    shutil.copytree(REPO / "prompts", runtime / "prompts")
    packaged_prompt = runtime / "prompts" / "decision-card-v1.md"
    assert packaged_prompt.is_file()
    assert hashlib.sha256(packaged_prompt.read_bytes()).hexdigest() == prompt_hash()
    assert PROMPT_PATH.is_file()


@pytest.mark.usefixtures("monkeypatch")
def test_full_api_db_paper_lifecycle_edd(monkeypatch, tmp_path):
    """EDD: internal research through authenticated paper entry/exit leaves only paper rows."""
    # Evaluation card: normal PASS lifecycle; hostile controls (FAIL card,
    # legacy manual-only, unapproved/price/stale/duplicate ticks); no broker/network.
    monkeypatch.setenv("INTERNAL_API_KEY", INTERNAL["X-Internal-API-Key"])
    monkeypatch.setattr(dbmod, "DATABASE_PATH", str(tmp_path / "paper-lifecycle.db"))
    from app import app
    # The complete in-process HTTP journey must not need a network broker.
    monkeypatch.setattr(socket, "create_connection", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("network broker forbidden")))
    client = TestClient(app)

    assert LIVE_TRADING is False
    assert client.post("/api/internal/evidence", json=evidence_payload("no-key")).status_code == 403
    assert client.post("/api/internal/scheduler-runs/research-2026-08-31/start", json={"kind": "research"}, headers=INTERNAL).status_code == 200

    evidence = client.post("/api/internal/evidence", json=evidence_payload("pass-evidence"), headers=INTERNAL)
    assert evidence.status_code == 200
    evidence = evidence.json()
    assert client.post("/api/internal/evidence", json=evidence_payload("pass-evidence"), headers=INTERNAL).status_code == 409
    listed = client.get("/api/internal/evidence?date=2026-08-31", headers=INTERNAL)
    assert [row["id"] for row in listed.json()] == [evidence["id"]]
    assert client.get(f"/api/internal/evidence/{evidence['id']}", headers=INTERNAL).json()["dedupe_key"] == "pass-evidence"
    assert client.post("/api/internal/scheduler-runs/research-2026-08-31/finish", json={"status": "done", "count": 1}, headers=INTERNAL).status_code == 200

    filter_response = client.post("/api/internal/filters", json={"evidence_id": evidence["id"], "inputs": raw(), "as_of": AS_OF, "known_at": KNOWN}, headers=INTERNAL)
    assert filter_response.status_code == 200
    filter_result = filter_response.json()
    assert filter_result["verdict"] == "PASS"
    generated = client.post("/api/internal/cards/generate", json={"evidence_id": evidence["id"], "filter_id": filter_result["id"]}, headers=INTERNAL)
    assert generated.status_code == 200 and generated.json()["prompt_hash"] == prompt_hash()
    primary = create_card(client, evidence["id"], filter_result["id"], lineage="edd-primary")

    # A visible FAIL card exists but its state cannot create a draft or approval.
    failing_evidence = client.post("/api/internal/evidence", json=evidence_payload("fail-evidence", title="불확실 공시"), headers=INTERNAL).json()
    failing_filter = client.post("/api/internal/filters", json={"evidence_id": failing_evidence["id"], "inputs": raw(baseline_volume=0), "as_of": AS_OF, "known_at": KNOWN}, headers=INTERNAL).json()
    fail_card = create_card(client, failing_evidence["id"], failing_filter["id"], lineage="edd-fail", filter_verdict="FAIL", verdict="관찰", **NONBUY_UNAVAILABLE_ORDER_FIELDS)
    assert fail_card["card"]["price_cap"] is None and fail_card["card"]["take_profit"] == []

    assert client.post("/api/signup", json={"email": "edd@example.test", "password": "password-for-edd"}).status_code == 200
    assert "결정 카드 운영" in client.get("/app").text
    visible = client.get("/api/cards")
    assert {row["id"] for row in visible.json()} >= {primary["id"], fail_card["id"]}
    assert client.get(f"/api/cards/{primary['id']}").json()["prompt_hash"] == prompt_hash()
    assert client.post(f"/api/cards/{fail_card['id']}/order-plan-draft", json={}).status_code == 409
    assert client.post(f"/api/cards/{fail_card['id']}/decisions", json={"decision": "approve"}).status_code == 409

    draft = client.post(f"/api/cards/{primary['id']}/order-plan-draft", json={"max_amount": 270, "max_qty": 4, "price_cap": 100})
    assert draft.status_code == 200 and draft.json()["status"] == "draft"
    approved = client.post(f"/api/cards/{primary['id']}/decisions", json={"decision": "approve", "note": "EDD approval"})
    assert approved.status_code == 200 and approved.json()["idempotent"] is False
    plan_id = approved.json()["order_plan_id"]
    assert client.get(f"/api/order-plans/{plan_id}").json()["status"] == "approved"

    # Legacy UI plans are permanently manual-only, so legacy ticks never create paper fills.
    legacy = client.post("/api/plans", json={"symbol": "005930", "name": "legacy", "scheduled_at": AS_OF, "qty": 1, "order_type": "market"}).json()
    assert legacy["status"] == "manual_only"
    assert client.post("/api/ticks", json={"symbol": "005930", "price": 90, "volume": 10, "baseline_volume": 1, "known_at": "2026-08-31T10:00:00+09:00", "idempotency_key": "legacy-zero"}).json()["live_trading"] is False

    # A separately approved then edited plan becomes unapproved; it cannot fill.
    second_evidence = client.post("/api/internal/evidence", json=evidence_payload("secondary-evidence", title="별도 계약"), headers=INTERNAL).json()
    second_filter = client.post("/api/internal/filters", json={"evidence_id": second_evidence["id"], "inputs": raw(), "as_of": AS_OF, "known_at": KNOWN}, headers=INTERNAL).json()
    secondary = create_card(client, second_evidence["id"], second_filter["id"], lineage="edd-secondary")
    second_plan = client.post(f"/api/cards/{secondary['id']}/decisions", json={"decision": "approve"}).json()["order_plan_id"]
    assert client.post(f"/api/order-plans/{second_plan}/edit", json={"max_qty": 2}).json()["status"] == "entry_invalidated"
    rejected = client.post(f"/api/internal/order-plans/{second_plan}/evaluate", json=tick("unapproved"), headers=INTERNAL)
    assert rejected.json()["fills"] == []

    evaluate = lambda key, **kwargs: client.post(f"/api/internal/order-plans/{plan_id}/evaluate", json=tick(key, **kwargs), headers=INTERNAL).json()
    assert evaluate("price-cap", price=101)["fills"] == []
    assert evaluate("stale", known_at="2026-08-31T09:55:00+09:00")["fills"] == []
    assert evaluate("buy", quantity=3)["fills"] == [{"side": "buy", "qty": 3, "price": 90}]
    assert evaluate("buy", quantity=3)["fills"] == []  # exact tick idempotency
    assert evaluate("take-profit", price=110, quantity=99)["fills"] == [{"side": "sell", "qty": 1, "price": 110}]
    assert evaluate("take-profit-again", price=110, quantity=99)["fills"] == []  # frozen split stage
    closed = client.post(f"/api/order-plans/{plan_id}/close", json={"tick_key": "manual-close", "known_at": "2026-08-31T10:04:00+09:00", "server_now": "2026-08-31T10:05:00+09:00", "price": 90})
    assert closed.status_code == 200 and closed.json()["fills"] == [{"side": "sell", "qty": 2, "price": 90}]
    final = client.get(f"/api/order-plans/{plan_id}").json()
    assert final["status"] == "closed" and final["position"] == {"qty": 0, "avg_price": 90.0, "status": "closed"}

    # Error and missing-card evidence are one union denominator in the dashboard.
    unresolved = client.post("/api/internal/evidence", json=evidence_payload("unresolved-error", title="오류"), headers=INTERNAL).json()
    client.patch(f"/api/internal/evidence/{unresolved['id']}", json={"status": "error"}, headers=INTERNAL)
    summary = client.get("/api/cards/summary?date=2026-08-31").json()
    assert summary["실패·근거 부족"] == 1  # same unresolved row is error AND card-missing, counted once

    db = dbmod.connect()
    try:
        counts = {table: db.execute(f"SELECT count(*) FROM {table}").fetchone()[0] for table in (
            "material_evidence", "deterministic_filter_results", "decision_cards", "user_decisions",
            "order_plans", "order_evaluations", "order_events", "order_fills", "positions",
            "exit_lineage", "scheduler_runs", "audit_logs",
        )}
        assert counts["material_evidence"] == 4
        assert counts["deterministic_filter_results"] == 3
        assert counts["decision_cards"] == 3
        assert counts["user_decisions"] == 2
        assert counts["order_plans"] == 2
        assert counts["order_evaluations"] == 7
        assert counts["order_events"] == 7
        assert counts["order_fills"] == 3
        assert counts["positions"] == 1 and counts["exit_lineage"] == 2
        assert counts["scheduler_runs"] == 1 and counts["audit_logs"] >= 10
    finally:
        db.close()
