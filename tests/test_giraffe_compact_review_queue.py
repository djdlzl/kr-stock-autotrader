import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from kr_stock_autotrader import giraffe_review_queue as queue


def contract(tmp_path: Path, *, sources=None):
    sources = sources if sources is not None else [
        {"rcp_no": "20260923000001", "date": "20260923", "receipt_source_date": "20260923", "report_class": "supply_contract", "report_name": "단일판매ㆍ공급계약체결", "packet_path": str(tmp_path / "20260923" / "20260923000001.json"), "packet_sha256": "a" * 64},
        {"rcp_no": "20260923000002", "date": "20260923", "receipt_source_date": "20260923", "report_class": "other", "report_name": "기타", "source_error_code": "SOURCE_FETCH_ERROR"},
    ]
    value = {"schema_version": "giraffe-research-control-v3", "run_key": "research-2026-09-23-0700-kst", "dates": ["20260923"], "source_valid": True, "expected_rcp_nos": [item["rcp_no"] for item in sources], "control_count": len(sources), "sources": sources, "carry_forward": [], "terminal_exclusions": [], "correction_of": []}
    root = tmp_path / "controls"; root.mkdir()
    path = root / (value["run_key"] + ".json")
    path.write_bytes(queue.canonical_bytes(value) + b"\n")
    return root, value, hashlib.sha256(queue.canonical_bytes(value)).hexdigest()


def test_compact_queue_preserves_exact_identity_order_and_hash(tmp_path):
    root, value, digest = contract(tmp_path)
    result = queue.compact_review_queue(value["run_key"], digest, control_root=root, offset=0, limit=1)
    assert result == {"schema_version": "giraffe-compact-review-queue-v1", "run_key": value["run_key"], "control_contract_sha256": digest, "control_count": 2, "source_valid_count": 1, "source_error_count": 1, "offset": 0, "limit": 1, "remaining": 1, "items": [{"position": 0, "rcp_no": "20260923000001", "date": "20260923", "receipt_source_date": "20260923", "report_class": "supply_contract", "report_name": "단일판매ㆍ공급계약체결", "source_state": "packet"}]}


@pytest.mark.parametrize("mutate", [
    lambda value: value.update(control_count=3),
    lambda value: value.update(expected_rcp_nos=list(reversed(value["expected_rcp_nos"]))),
    lambda value: value.update(sources=[{**value["sources"][0], "report_class": None}]),
])
def test_compact_queue_fails_closed_for_malformed_or_nonexact_control(tmp_path, mutate):
    root, value, digest = contract(tmp_path)
    mutate(value)
    (root / (value["run_key"] + ".json")).write_bytes(queue.canonical_bytes(value) + b"\n")
    with pytest.raises(queue.ReviewQueueError):
        queue.compact_review_queue(value["run_key"], digest, control_root=root)


def test_full_packet_is_only_opened_on_demand_and_revalidates_provenance(tmp_path, monkeypatch):
    root, value, digest = contract(tmp_path)
    source = value["sources"][0]
    calls = []
    monkeypatch.setattr(queue, "completed_packet", lambda path, rcp_no, expected_control_date: calls.append((path, rcp_no, expected_control_date)) or {"text_path": str(tmp_path / "packet.txt"), "raw_sha256": "b" * 64})
    (tmp_path / "packet.txt").write_text("full immutable source", encoding="utf-8")
    result = queue.open_review_packet(value["run_key"], digest, source["rcp_no"], control_root=root)
    assert calls == [(Path(source["packet_path"]), source["rcp_no"], source["date"])]
    assert result["position"] == 0
    assert result["source"] == source
    assert result["text"] == "full immutable source"


def test_prehook_compact_payload_is_bounded_and_has_no_contract_or_paths():
    payload = queue.compact_gate_payload("research-2026-09-23-0700-kst", "a" * 64, 222, 219, 3)
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    assert set(payload) == {"gate", "complete", "run_key", "control_contract_sha256", "control_count", "source_valid_count", "source_error_count"}
    assert "control_contract" not in set(payload) and "packet_path" not in encoded
    assert len(encoded.encode("utf-8")) < 300


def test_cli_pages_representative_exact_control_without_emitting_full_packet(tmp_path):
    root, value, digest = contract(tmp_path)
    env = {**os.environ, "GIRAFFE_DART_CONTROL_ROOT": str(root)}
    command = [sys.executable, "-m", "kr_stock_autotrader.cli", "giraffe-review-queue", value["run_key"], digest, "--limit", "1"]
    completed = subprocess.run(command, env=env, text=True, capture_output=True, check=True)
    result = json.loads(completed.stdout)
    assert result["items"][0]["rcp_no"] == value["expected_rcp_nos"][0]
    assert result["remaining"] == 1
    assert "packet_path" not in completed.stdout
    assert "full immutable source" not in completed.stdout
