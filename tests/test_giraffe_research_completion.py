import os
import tempfile

os.environ.setdefault("DATABASE_PATH", tempfile.mktemp(suffix=".db"))
os.environ.setdefault("INTERNAL_API_KEY", "test-key")
os.environ.setdefault("SESSION_SECRET", "test-session-secret-that-is-at-least-thirty-two-bytes-long")
from fastapi.testclient import TestClient
from app import app

HEADERS = {"X-Internal-API-Key": os.environ["INTERNAL_API_KEY"]}


def receipt(control=0, **extra):
    value = {"schema_version":"giraffe-research-completion-v1", "control_count":control, "source_valid":control, "reviewed_unique":control, "source_error":0, "store_error":0, "coverage_error":0, "rejected_after_evidence":control, "saved":0, "existing":0, "correction_stored":0}
    value.update(extra)
    return value


def start(client, key):
    assert client.post(f"/api/internal/scheduler-runs/{key}/start", json={"kind":"research"}, headers=HEADERS).status_code == 200


def test_research_done_rejects_forged_or_partial_receipts_and_keeps_started():
    client=TestClient(app); key="research-2026-09-12-0700-kst"; start(client,key)
    forged={"status":"done","count":0,"detail":{"completion_receipt":receipt(96, source_error=1, rejected_after_evidence=95)}}
    assert client.post(f"/api/internal/scheduler-runs/{key}/finish",json=forged,headers=HEADERS).status_code == 422
    malformed={"status":"done","count":0,"detail":{"completion_receipt":{"schema_version":"giraffe-research-completion-v1"}}}
    assert client.post(f"/api/internal/scheduler-runs/{key}/finish",json=malformed,headers=HEADERS).status_code == 422
    latest=client.get("/api/internal/scheduler-runs/latest?kind=research&date=2026-09-12",headers=HEADERS)
    assert latest.json()["status"] == "started"


def test_research_zero_manifest_done_and_terminal_idempotence():
    client=TestClient(app); key="research-2026-09-13-0700-kst"; start(client,key)
    payload={"status":"done","count":0,"detail":{"completion_receipt":receipt(0)}}
    first=client.post(f"/api/internal/scheduler-runs/{key}/finish",json=payload,headers=HEADERS)
    assert first.status_code == 200 and first.json()["status"] == "done"
    second=client.post(f"/api/internal/scheduler-runs/{key}/finish",json={"status":"error","count":0,"detail":{}},headers=HEADERS)
    assert second.json()["status"] == "done"
