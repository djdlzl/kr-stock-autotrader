"""Regression state table for v3 append-only DART correction terminal routing."""

import hashlib
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import app
from kr_stock_autotrader import api as api_module
from kr_stock_autotrader.db import connect
# Pytest places this test directory on sys.path for direct-file collection;
# importing its sibling by module name avoids treating ``tests`` as a package.
from test_giraffe_research_completion import CONTROL_HEADERS, HEADERS, canonical, commitment, receipt


def _correction_case(monkeypatch, tmp_path, *, suffix: int, report_class: str, report_name: str):
    """Create one immutable rejected parent and its pending v3 correction cursor."""
    import kr_stock_autotrader.db as db_module

    monkeypatch.setattr(db_module, "DATABASE_PATH", str(tmp_path / f"correction-{suffix}.db"))
    client = TestClient(app)
    rcp = f"2026092300{suffix:04d}"
    prior_key = "research-2026-09-22-0700-kst-r2"
    key = f"research-2026-09-23-0700-kst-r{suffix}"
    base, _ = commitment(key, [rcp])
    source = base["sources"][0]
    source.update({
        "report_class": report_class,
        "report_name": report_name,
        "packet_path": str(Path(source["packet_path"]).parent / ("v3-" + "a" * 32) / f"{rcp}.json"),
        "packet_sha256": "b" * 64,
    })
    legacy = {field: source[field] for field in ("rcp_no", "date", "receipt_source_date", "packet_path", "packet_sha256")}
    # The historical parent is deliberately old-schema and never rewritten.
    db = connect()
    try:
        db.execute("INSERT INTO scheduler_runs(run_key,kind,status,started_at,finished_at,detail) VALUES(?,?,?,?,?,?)", (prior_key, "research", "done", "2026-09-22T07:00:00+09:00", "2026-09-22T07:01:00+09:00", "{}"))
        db.execute("INSERT INTO giraffe_research_backlog(identity,kind,payload,first_run_key,status,terminal_disposition,terminal_run_key,terminal_evidence_id,created_at,terminal_at) VALUES(?,?,?,?,?,?,?,?,?,?)", ("dart:" + rcp, "dart", json.dumps(legacy, sort_keys=True), prior_key, "terminal", "rejected", prior_key, None, "2026-09-22T07:00:00+09:00", "2026-09-22T07:01:00+09:00"))
        db.commit()
        parent = dict(db.execute("SELECT identity,kind,payload,terminal_disposition,terminal_run_key,terminal_evidence_id,terminal_at FROM giraffe_research_backlog WHERE identity=?", ("dart:" + rcp,)).fetchone())
        parent["payload"] = json.loads(parent["payload"])
    finally:
        db.close()
    base.update({"schema_version": "giraffe-research-control-v3", "carry_forward": [], "terminal_exclusions": [], "correction_of": [parent]})
    registered = client.post(f"/api/internal/research-runs/{key}/register", headers=CONTROL_HEADERS, json={"control_contract": base})
    assert registered.status_code == 200, registered.text
    return client, key, rcp, base, parent


def _non_supply_item(rcp, disposition, economic):
    return {"rcp_no": rcp, "disposition": disposition, "evidence_id": None,
            "economic_disposition": economic, "economic_reason": "source-grounded non-material merger completion",
            "economic_facts": None, "audit_source_url": "https://dart.fss.or.kr/dsaf001/main.do?rcpNo=" + rcp}


def test_source_grounded_other_correction_can_terminalize_rejected_without_evidence(monkeypatch, tmp_path):
    """r5 regression: an append-only non-supply correction has a legal terminal state."""
    client, key, rcp, contract, parent = _correction_case(monkeypatch, tmp_path, suffix=258, report_class="other", report_name="합병등종료보고서")
    digest = hashlib.sha256(canonical(contract)).hexdigest()
    completed = receipt(key, digest, [rcp])
    item = _non_supply_item(rcp, "rejected", "below_threshold")
    response = client.post(f"/api/internal/scheduler-runs/{key}/finish", headers=HEADERS, json={"status": "done", "count": 0, "detail": {"completion_receipt": completed, "control_terminal_dispositions": [item]}})
    assert response.status_code == 200, response.text

    db = connect()
    try:
        rows = db.execute("SELECT identity,payload,status,terminal_disposition,terminal_run_key,terminal_evidence_id FROM giraffe_research_backlog ORDER BY identity").fetchall()
        original = next(row for row in rows if row["identity"] == "dart:" + rcp)
        correction = next(row for row in rows if row["identity"].startswith("dart:correction:"))
        assert json.loads(original["payload"]) == parent["payload"]
        assert dict(correction)["status"] == "terminal"
        assert dict(correction)["terminal_disposition"] == "rejected"
        assert dict(correction)["terminal_run_key"] == key
        assert dict(correction)["terminal_evidence_id"] is None
    finally:
        db.close()


def test_nonqualifying_supply_correction_can_terminalize_rejected_without_evidence(monkeypatch, tmp_path):
    """A correction is not evidence-only when validated supply facts reject it."""
    client, key, rcp, contract, _ = _correction_case(monkeypatch, tmp_path, suffix=264, report_class="dart_single_sale_supply_contract", report_name="단일판매ㆍ공급계약체결")
    digest = hashlib.sha256(canonical(contract)).hexdigest()
    item = {"rcp_no": rcp, "disposition": "rejected", "evidence_id": None,
            "economic_disposition": "below_threshold", "economic_reason": "binding contract is 1 percent of prior revenue",
            "economic_facts": {"binding_contract": True, "contract_amount": 100, "prior_revenue": 10000,
                               "ratio_percent": 1.0, "term": "2026-09-23 to 2026-12-31"}}
    response = client.post(f"/api/internal/scheduler-runs/{key}/finish", headers=HEADERS, json={"status": "done", "count": 0, "detail": {"completion_receipt": receipt(key, digest, [rcp]), "control_terminal_dispositions": [item]}})
    assert response.status_code == 200, response.text
    assert client.get("/api/internal/research-backlog", headers=CONTROL_HEADERS).json()["items"] == []


@pytest.mark.parametrize("disposition,economic,expected_status,expected_pending", [
    ("hold", "timing_unresolved", 200, False),
    ("rejected", "timing_unresolved", 422, True),
    ("correction_stored", "below_threshold", 422, True),
    ("saved", "below_threshold", 422, True),
    ("source_error", "error", 200, True),
    ("store_error", "error", 200, True),
])
def test_other_correction_terminal_state_table_preserves_forbidden_or_retry_effects(monkeypatch, tmp_path, disposition, economic, expected_status, expected_pending):
    """Correction × other × result/disposition/evidence/readback state table."""
    client, key, rcp, contract, _ = _correction_case(monkeypatch, tmp_path, suffix={"hold": 259, "rejected": 266, "correction_stored": 260, "saved": 261, "source_error": 262, "store_error": 263}[disposition], report_class="other", report_name="합병등종료보고서")
    digest = hashlib.sha256(canonical(contract)).hexdigest()
    if disposition in {"source_error", "store_error"}:
        completed = receipt(key, digest, [rcp], source_valid=int(disposition != "source_error"), reviewed_unique=0, reviewed_rcp_nos=[], source_error=int(disposition == "source_error"), store_error=int(disposition == "store_error"), rejected_after_evidence=0)
        item = {"rcp_no": rcp, "disposition": disposition, "evidence_id": None, "economic_disposition": "error", "economic_reason": "truthful transient failure", "economic_facts": None}
    else:
        completed = receipt(key, digest, [rcp], rejected_after_evidence=1)
        item = _non_supply_item(rcp, disposition, economic)
    response = client.post(f"/api/internal/scheduler-runs/{key}/finish", headers=HEADERS, json={"status": "done", "count": 0, "detail": {"completion_receipt": completed, "control_terminal_dispositions": [item]}})
    assert response.status_code == expected_status, response.text
    pending = client.get("/api/internal/research-backlog", headers=CONTROL_HEADERS).json()["items"]
    assert bool(pending) is expected_pending


def test_other_correction_rejects_unrelated_audit_url_and_keeps_cursor_pending(monkeypatch, tmp_path):
    """A terminal non-supply audit must cite the exact authoritative DART receipt."""
    client, key, rcp, contract, _ = _correction_case(monkeypatch, tmp_path, suffix=265, report_class="other", report_name="합병등종료보고서")
    item = _non_supply_item(rcp, "rejected", "below_threshold")
    item["audit_source_url"] = "https://example.invalid/not-the-dart-source"
    digest = hashlib.sha256(canonical(contract)).hexdigest()
    response = client.post(f"/api/internal/scheduler-runs/{key}/finish", headers=HEADERS, json={"status": "done", "count": 0, "detail": {"completion_receipt": receipt(key, digest, [rcp]), "control_terminal_dispositions": [item]}})
    assert response.status_code == 422, response.text
    pending = client.get("/api/internal/research-backlog", headers=CONTROL_HEADERS).json()["items"]
    assert len(pending) == 1 and pending[0]["identity"].startswith("dart:correction:")
