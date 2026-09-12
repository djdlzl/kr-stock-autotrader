import hashlib
import json
import os
import tempfile

os.environ.setdefault("DATABASE_PATH", tempfile.mktemp(suffix=".db"))
os.environ.setdefault("INTERNAL_API_KEY", "test-key")
os.environ.setdefault("SESSION_SECRET", "test-session-secret-that-is-at-least-thirty-two-bytes-long")
from fastapi.testclient import TestClient
from app import app

HEADERS = {"X-Internal-API-Key": os.environ["INTERNAL_API_KEY"]}


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def commitment(run_key, receipts):
    date = run_key.removeprefix("research-").removesuffix("-0700-kst").replace("-", "")
    sources = [{
        "rcp_no": rcp_no,
        "date": date,
        "packet_path": f"/trusted/{rcp_no}.json",
        "packet_sha256": "a" * 64,
    } for rcp_no in sorted(receipts)]
    contract = {
        "schema_version": "giraffe-research-control-v1",
        "run_key": run_key,
        "dates": [date],
        "source_valid": True,
        "expected_rcp_nos": sorted(receipts),
        "control_count": len(receipts),
        "sources": sources,
    }
    return contract, hashlib.sha256(canonical(contract)).hexdigest()


def start(client, key, receipts=()):
    contract, digest = commitment(key, receipts)
    response = client.post(
        f"/api/internal/scheduler-runs/{key}/start",
        json={"kind": "research", "control_contract": contract,
              "control_contract_path": "/trusted/nonempty.json",
              "control_contract_sha256": digest}, headers=HEADERS)
    assert response.status_code == 200
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


def test_research_start_rejects_unbound_or_noncanonical_commitments():
    client = TestClient(app); key = "research-2026-09-14-0700-kst"
    contract, digest = commitment(key, ["20260914000001"])
    for payload in (
        {"kind": "research"},
        {"kind": "research", "control_contract": contract, "control_contract_path": "/trusted/x", "control_contract_sha256": "not-a-hash"},
        {"kind": "research", "control_contract": {**contract, "control_count": 0}, "control_contract_path": "/trusted/x", "control_contract_sha256": digest},
    ):
        assert client.post(f"/api/internal/scheduler-runs/{key}/start", json=payload, headers=HEADERS).status_code == 422
