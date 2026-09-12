import hashlib
import json
import os
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

os.environ.setdefault("DATABASE_PATH", tempfile.mktemp(suffix=".db"))
os.environ.setdefault("INTERNAL_API_KEY", "test-key")
os.environ.setdefault("RESEARCH_CONTROL_KEY", "test-control-key")
os.environ.setdefault("SESSION_SECRET", "test-session-secret-that-is-at-least-thirty-two-bytes-long")
from fastapi.testclient import TestClient
from app import app
from kr_stock_autotrader import api as api_module

HEADERS = {"X-Internal-API-Key": os.environ["INTERNAL_API_KEY"]}
CONTROL_HEADERS = {"X-Research-Control-Key": os.environ["RESEARCH_CONTROL_KEY"]}


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def commitment(run_key, receipts, source_root=None):
    run_date = datetime.strptime(run_key.removeprefix("research-").removesuffix("-0700-kst"), "%Y-%m-%d")
    dates = [(run_date - timedelta(days=1)).strftime("%Y%m%d"), run_date.strftime("%Y%m%d")]
    source_root = source_root or Path.home() / ".hermes" / "runs" / "giraffe-7923" / "dart-source-packets"
    sources = [{
        "rcp_no": rcp_no,
        "date": rcp_no[:8],
        "packet_path": str(source_root / rcp_no[:8] / f"{rcp_no}.json"),
        "packet_sha256": "a" * 64,
    } for rcp_no in sorted(receipts)]
    contract = {
        "schema_version": "giraffe-research-control-v1",
        "run_key": run_key,
        "dates": dates,
        "source_valid": True,
        "expected_rcp_nos": sorted(receipts),
        "control_count": len(receipts),
        "sources": sources,
    }
    return contract, hashlib.sha256(canonical(contract)).hexdigest()


def register(client, key, receipts=(), headers=CONTROL_HEADERS):
    contract, digest = commitment(key, receipts)
    response = client.post(f"/api/internal/research-runs/{key}/register", json={"control_contract": contract}, headers=headers)
    assert response.status_code == 200
    return contract, digest


def start(client, key, receipts=()):
    contract, digest = register(client, key, receipts)
    response = client.post(f"/api/internal/scheduler-runs/{key}/start", json={"kind": "research"}, headers=HEADERS)
    assert response.status_code == 200 and response.json()["idempotent"] is True
    return contract, digest


def receipt(run_key, digest, receipts, **extra):
    control = len(receipts)
    value = {
        "schema_version": "giraffe-research-completion-v1", "run_key": run_key,
        "control_contract_sha256": digest, "control_count": control,
        "source_valid": control, "reviewed_unique": control,
        "reviewed_rcp_nos": sorted(receipts), "source_error": 0, "store_error": 0,
        "coverage_error": 0, "rejected_after_evidence": control, "saved": 0,
        "existing": 0, "correction_stored": 0,
    }
    value.update(extra)
    return value


def test_exact_observed_forged_done_control_exploit_is_rejected_and_keeps_started():
    client = TestClient(app); key = "research-2026-09-12-0700-kst"
    _, digest = start(client, key, ["20260912000001"])
    forged = {"status": "done", "count": 0, "detail": {"completion_receipt": receipt(key, digest, [])}}
    assert client.post(f"/api/internal/scheduler-runs/{key}/finish", json=forged, headers=HEADERS).status_code == 422
    latest = client.get("/api/internal/scheduler-runs/latest?kind=research&date=2026-09-12", headers=HEADERS)
    assert latest.json()["status"] == "started"
    assert latest.json()["detail"]["control_commitment"]["control_contract"]["expected_rcp_nos"] == ["20260912000001"]


def test_research_done_binds_exact_control_ids_contract_and_terminal_idempotence():
    client = TestClient(app); key = "research-2026-09-13-0700-kst"
    contract, digest = start(client, key, ["20260913000001", "20260913000002"])
    for bad_receipts in (["20260913000001"], ["20260913000001", "20260913000001"], ["20260913000001", "20260913999999"]):
        payload = {"status": "done", "count": 0, "detail": {"completion_receipt": receipt(key, digest, bad_receipts)}}
        assert client.post(f"/api/internal/scheduler-runs/{key}/finish", json=payload, headers=HEADERS).status_code == 422
    bad_run = {"status": "done", "count": 0, "detail": {"completion_receipt": receipt("research-2026-09-12-0700-kst", digest, contract["expected_rcp_nos"])}}
    assert client.post(f"/api/internal/scheduler-runs/{key}/finish", json=bad_run, headers=HEADERS).status_code == 422
    bad_hash = {"status": "done", "count": 0, "detail": {"completion_receipt": receipt(key, "b" * 64, contract["expected_rcp_nos"])}}
    assert client.post(f"/api/internal/scheduler-runs/{key}/finish", json=bad_hash, headers=HEADERS).status_code == 422
    payload = {"status": "done", "count": 0, "detail": {"completion_receipt": receipt(key, digest, contract["expected_rcp_nos"])}}
    first = client.post(f"/api/internal/scheduler-runs/{key}/finish", json=payload, headers=HEADERS)
    assert first.status_code == 200 and first.json()["status"] == "done"
    second = client.post(f"/api/internal/scheduler-runs/{key}/finish", json={"status": "error", "count": 0, "detail": {}}, headers=HEADERS)
    assert second.json()["status"] == "done"


def test_internal_scheduler_cannot_create_or_forge_canonical_research_runs():
    client = TestClient(app); key = "research-2026-09-14-0700-kst"
    assert client.post(f"/api/internal/scheduler-runs/{key}/start", json={"kind": "research"}, headers=HEADERS).status_code == 404
    contract, _ = commitment(key, ["20260914000001"])
    assert client.post(f"/api/internal/scheduler-runs/{key}/start", json={"kind": "research", "control_contract": contract}, headers=HEADERS).status_code == 422
    assert client.post(f"/api/internal/research-runs/{key}/register", json={"control_contract": contract}, headers=HEADERS).status_code == 403
    assert client.post(f"/api/internal/research-runs/{key}/register", json={"control_contract": contract}, headers={"X-Research-Control-Key": "wrong"}).status_code == 403
    _, digest = register(client, key, ["20260914000001"])
    conflict, _ = commitment(key, ["20260914000002"])
    assert client.post(f"/api/internal/research-runs/{key}/register", json={"control_contract": conflict}, headers=CONTROL_HEADERS).status_code == 409
    valid = {"status": "done", "count": 0, "detail": {"completion_receipt": receipt(key, digest, ["20260914000001"])}}
    assert client.post(f"/api/internal/scheduler-runs/{key}/finish", json=valid, headers=HEADERS).status_code == 200


def test_registration_does_not_depend_on_caller_contract_path():
    client = TestClient(app); key = "research-2026-09-15-0700-kst"
    contract, _ = commitment(key, [])
    assert client.post(f"/api/internal/research-runs/{key}/register", json={"control_contract": contract}, headers=CONTROL_HEADERS).status_code == 200


def test_registration_uses_explicit_packet_root_when_container_home_differs(monkeypatch):
    client = TestClient(app); key = "research-2026-09-16-0700-kst"
    host_packet_root = Path("/Users/jaewoo/.hermes/runs/giraffe-7923/dart-source-packets")
    contract, _ = commitment(key, ["20260916000001"], source_root=host_packet_root)
    monkeypatch.setenv("GIRAFFE_RESEARCH_PACKET_ROOT", str(host_packet_root))
    monkeypatch.setattr(api_module.Path, "home", classmethod(lambda cls: (_ for _ in ()).throw(AssertionError("configured root must not use HOME"))))
    response = client.post(
        f"/api/internal/research-runs/{key}/register",
        json={"control_contract": contract},
        headers=CONTROL_HEADERS,
    )
    assert response.status_code == 200
