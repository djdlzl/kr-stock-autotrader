import hashlib
import json
import os
import tempfile
from datetime import datetime
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


def commitment(run_key, receipts, source_root=None, source_control_dates=None):
    from kr_stock_autotrader.krx_calendar import admitted_backlog_dates

    run_date = datetime.strptime(run_key.removeprefix("research-").split("-0700-kst", 1)[0], "%Y-%m-%d")
    dates = admitted_backlog_dates(run_date.date())
    source_root = source_root or Path.home() / ".hermes" / "runs" / "giraffe-7923" / "dart-source-packets"
    source_control_dates = source_control_dates or {}
    sources = [{
        "rcp_no": rcp_no,
        "date": source_control_dates.get(rcp_no, rcp_no[:8]),
        "receipt_source_date": rcp_no[:8],
        "packet_path": str(source_root / source_control_dates.get(rcp_no, rcp_no[:8]) / f"{rcp_no}.json"),
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


def register(client, key, receipts=(), headers=CONTROL_HEADERS, **commitment_kwargs):
    contract, digest = commitment(key, receipts, **commitment_kwargs)
    response = client.post(f"/api/internal/research-runs/{key}/register", json={"control_contract": contract}, headers=headers)
    assert response.status_code == 200
    return contract, digest


def start(client, key, receipts=(), **commitment_kwargs):
    contract, digest = register(client, key, receipts, **commitment_kwargs)
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
        "coverage_lanes": {
            name: {"executed": True, "query_count": 1, "checked_url_count": 1,
                   "source_valid_count": 1, "candidate_count": 0, "coverage_error_count": 0}
            for name in ("kind_krx", "issuer_ir_newsroom", "reputable_media")
        },
    }
    value.update(extra)
    return value


def test_versioned_research_run_registers_starts_and_finishes_but_arbitrary_suffix_rejects():
    client = TestClient(app); key = "research-2026-09-16-0700-kst-r1"
    contract, digest = start(client, key, ["20260916000001"])
    assert contract["run_key"] == key
    done = {"status": "done", "count": 0, "detail": {"completion_receipt": receipt(key, digest, ["20260916000001"])}}
    assert client.post(f"/api/internal/scheduler-runs/{key}/finish", json=done, headers=HEADERS).status_code == 200
    bad = "research-2026-09-16-0700-kst-correction"
    contract, _ = commitment(bad, [])
    assert client.post(f"/api/internal/research-runs/{bad}/register", json={"control_contract": contract}, headers=CONTROL_HEADERS).status_code == 422


def test_research_done_requires_exact_coverage_lanes():
    client = TestClient(app); key = "research-2026-09-16-0700-kst"
    _, digest = start(client, key, [])
    cases = [
        {},
        {"kind_krx": {}},
        {"kind_krx": {"executed": False, "query_count": 1, "checked_url_count": 0, "source_valid_count": 0, "candidate_count": 0, "coverage_error_count": 0},
         "issuer_ir_newsroom": {"executed": True, "query_count": 1, "checked_url_count": 0, "source_valid_count": 0, "candidate_count": 0, "coverage_error_count": 0},
         "reputable_media": {"executed": True, "query_count": 1, "checked_url_count": 0, "source_valid_count": 0, "candidate_count": 0, "coverage_error_count": 0}},
    ]
    valid = receipt(key, digest, [])
    for lanes in cases:
        invalid = receipt(key, digest, [], coverage_lanes=lanes)
        payload = {"status": "done", "count": 0, "detail": {"completion_receipt": invalid}}
        assert client.post(f"/api/internal/scheduler-runs/{key}/finish", json=payload, headers=HEADERS).status_code == 422
    for field, value in (("query_count", 0), ("coverage_error_count", 1)):
        invalid = receipt(key, digest, [])
        invalid["coverage_lanes"]["kind_krx"][field] = value
        payload = {"status": "done", "count": 0, "detail": {"completion_receipt": invalid}}
        assert client.post(f"/api/internal/scheduler-runs/{key}/finish", json=payload, headers=HEADERS).status_code == 422
    # A query alone is not coverage: every independent lane must check and
    # validate an original source before a no-candidate DONE is safe.
    for lane_name in ("kind_krx", "issuer_ir_newsroom", "reputable_media"):
        for field, value in (("checked_url_count", 0), ("source_valid_count", 0)):
            invalid = receipt(key, digest, [])
            invalid["coverage_lanes"][lane_name][field] = value
            payload = {"status": "done", "count": 0, "detail": {"completion_receipt": invalid}}
            assert client.post(f"/api/internal/scheduler-runs/{key}/finish", json=payload, headers=HEADERS).status_code == 422
    invalid = receipt(key, digest, [])
    invalid["coverage_lanes"]["issuer_ir_newsroom"].update({"checked_url_count": 1, "source_valid_count": 2})
    payload = {"status": "done", "count": 0, "detail": {"completion_receipt": invalid}}
    assert client.post(f"/api/internal/scheduler-runs/{key}/finish", json=payload, headers=HEADERS).status_code == 422
    payload = {"status": "done", "count": 0, "detail": {"completion_receipt": valid}}
    assert client.post(f"/api/internal/scheduler-runs/{key}/finish", json=payload, headers=HEADERS).status_code == 200


def test_exact_observed_forged_done_control_exploit_is_rejected_and_keeps_started():
    client = TestClient(app); key = "research-2026-09-11-0700-kst"
    _, digest = start(client, key, ["20260911000001"])
    forged = {"status": "done", "count": 0, "detail": {"completion_receipt": receipt(key, digest, [])}}
    assert client.post(f"/api/internal/scheduler-runs/{key}/finish", json=forged, headers=HEADERS).status_code == 422
    latest = client.get("/api/internal/scheduler-runs/latest?kind=research&date=2026-09-11", headers=HEADERS)
    assert latest.json()["status"] == "started"
    assert latest.json()["detail"]["control_commitment"]["control_contract"]["expected_rcp_nos"] == ["20260911000001"]


def test_research_done_binds_exact_control_ids_contract_and_terminal_idempotence():
    client = TestClient(app); key = "research-2026-09-14-0700-kst"
    contract, digest = start(client, key, ["20260914000001", "20260914000002"])
    for bad_receipts in (["20260914000001"], ["20260914000001", "20260914000001"], ["20260914000001", "20260914999999"]):
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
    client = TestClient(app); key = "research-2026-09-15-0700-kst"
    assert client.post(f"/api/internal/scheduler-runs/{key}/start", json={"kind": "research"}, headers=HEADERS).status_code == 404
    contract, _ = commitment(key, ["20260915000001"])
    assert contract["dates"] == ["20260914", "20260915"]
    assert client.post(f"/api/internal/scheduler-runs/{key}/start", json={"kind": "research", "control_contract": contract}, headers=HEADERS).status_code == 422
    assert client.post(f"/api/internal/research-runs/{key}/register", json={"control_contract": contract}, headers=HEADERS).status_code == 403
    assert client.post(f"/api/internal/research-runs/{key}/register", json={"control_contract": contract}, headers={"X-Research-Control-Key": "wrong"}).status_code == 403
    _, digest = register(client, key, ["20260915000001"])
    conflict, _ = commitment(key, ["20260915000002"])
    assert client.post(f"/api/internal/research-runs/{key}/register", json={"control_contract": conflict}, headers=CONTROL_HEADERS).status_code == 409
    valid = {"status": "done", "count": 0, "detail": {"completion_receipt": receipt(key, digest, ["20260915000001"])}}
    assert client.post(f"/api/internal/scheduler-runs/{key}/finish", json=valid, headers=HEADERS).status_code == 200


def test_registration_does_not_depend_on_caller_contract_path():
    client = TestClient(app); key = "research-2026-09-16-0700-kst"
    contract, _ = commitment(key, [])
    assert client.post(f"/api/internal/research-runs/{key}/register", json={"control_contract": contract}, headers=CONTROL_HEADERS).status_code == 200


def test_registration_uses_explicit_packet_root_when_container_home_differs(monkeypatch):
    client = TestClient(app); key = "research-2026-09-17-0700-kst"
    host_packet_root = Path("/Users/jaewoo/.hermes/runs/giraffe-7923/dart-source-packets")
    contract, _ = commitment(key, ["20260917000001"], source_root=host_packet_root)
    monkeypatch.setenv("GIRAFFE_RESEARCH_PACKET_ROOT", str(host_packet_root))
    monkeypatch.setattr(api_module.Path, "home", classmethod(lambda cls: (_ for _ in ()).throw(AssertionError("configured root must not use HOME"))))
    response = client.post(
        f"/api/internal/research-runs/{key}/register",
        json={"control_contract": contract},
        headers=CONTROL_HEADERS,
    )
    assert response.status_code == 200


def test_correction_contract_registers_and_completes_with_control_date_packet_path():
    client = TestClient(app); key = "research-2026-09-15-0700-kst"
    receipt_id = "20260914000432"
    contract, digest = start(client, key, [receipt_id], source_control_dates={receipt_id: "20260915"})
    assert contract["sources"] == [{
        "rcp_no": receipt_id, "date": "20260915", "receipt_source_date": "20260914",
        "packet_path": str(Path.home() / ".hermes" / "runs" / "giraffe-7923" / "dart-source-packets" / "20260915" / f"{receipt_id}.json"),
        "packet_sha256": "a" * 64,
    }]
    done = {"status": "done", "count": 0, "detail": {"completion_receipt": receipt(key, digest, [receipt_id])}}
    assert client.post(f"/api/internal/scheduler-runs/{key}/finish", json=done, headers=HEADERS).status_code == 200


def test_registration_rejects_legacy_or_forged_correction_source_schema():
    client = TestClient(app); key = "research-2026-09-15-0700-kst"; receipt_id = "20260914000432"
    contract, _ = commitment(key, [receipt_id], source_control_dates={receipt_id: "20260915"})
    variants = [
        dict(contract, sources=[{key: value for key, value in contract["sources"][0].items() if key != "receipt_source_date"}]),
        dict(contract, sources=[dict(contract["sources"][0], receipt_source_date="20260915")]),
        dict(contract, sources=[dict(contract["sources"][0], date="20260913", packet_path=str(Path.home() / ".hermes" / "runs" / "giraffe-7923" / "dart-source-packets" / "20260913" / f"{receipt_id}.json"))]),
        dict(contract, sources=[dict(contract["sources"][0], packet_path="/untrusted/20260915/20260914000432.json")]),
    ]
    for bad in variants:
        assert client.post(f"/api/internal/research-runs/{key}/register", json={"control_contract": bad}, headers=CONTROL_HEADERS).status_code == 422


def test_registration_rejects_contract_date_shrink_expand_reorder_and_cross_date_receipts():
    client = TestClient(app)
    key = "research-2026-09-14-0700-kst"  # Monday: Sat/Sun/Mon backlog.
    contract, _ = commitment(key, ["20260912000001", "20260913000001", "20260914000001"])
    variants = []
    variants.append(dict(contract, dates=contract["dates"][1:]))
    variants.append(dict(contract, dates=["20260911", *contract["dates"]]))
    variants.append(dict(contract, dates=list(reversed(contract["dates"]))))
    variants.append(dict(contract, sources=[dict(contract["sources"][0], date="20260911"), *contract["sources"][1:]]))
    for bad in variants:
        response = client.post(f"/api/internal/research-runs/{key}/register", json={"control_contract": bad}, headers=CONTROL_HEADERS)
        assert response.status_code == 422
