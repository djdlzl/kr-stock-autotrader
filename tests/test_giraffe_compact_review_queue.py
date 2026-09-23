import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from kr_stock_autotrader import giraffe_review_queue as queue


def contract(tmp_path: Path, *, sources=None):
    packet = tmp_path / "source" / "20260923" / "20260923000001.json"
    packet.parent.mkdir(parents=True, exist_ok=True); packet.write_bytes(b"packet-metadata")
    sources = sources if sources is not None else [
        {"rcp_no": "20260923000001", "date": "20260923", "receipt_source_date": "20260923", "report_class": "other", "report_name": "기타", "packet_path": str(packet), "packet_sha256": hashlib.sha256(packet.read_bytes()).hexdigest()},
        {"rcp_no": "20260923000002", "date": "20260923", "receipt_source_date": "20260923", "report_class": "other", "report_name": "기타", "source_error_code": "SOURCE_FETCH_ERROR"},
    ]
    value = {"schema_version": "giraffe-research-control-v3", "run_key": "research-2026-09-23-0700-kst", "dates": ["20260923"], "source_valid": True, "expected_rcp_nos": [item["rcp_no"] for item in sources], "control_count": len(sources), "sources": sources, "carry_forward": [], "terminal_exclusions": [], "correction_of": []}
    root = tmp_path / "controls"; root.mkdir(); (root / (value["run_key"] + ".json")).write_bytes(queue.canonical_bytes(value) + b"\n")
    return root, value, hashlib.sha256(queue.canonical_bytes(value)).hexdigest(), tmp_path / "source"


def test_compact_queue_preserves_exact_identity_order_hash_and_page_digest(tmp_path):
    root, value, digest, _ = contract(tmp_path)
    result = queue.compact_review_queue(value["run_key"], digest, control_root=root, offset=0, limit=1)
    assert result["items"] == [{"position": 0, "rcp_no": "20260923000001", "date": "20260923", "receipt_source_date": "20260923", "report_class": "other", "report_name": "기타", "source_state": "packet"}]
    assert result["remaining"] == 1 and result["page_sha256"] == hashlib.sha256(queue.canonical_bytes(result["items"])).hexdigest()
    manifest = queue.compact_review_manifest(value["run_key"], digest, control_root=root, page_size=1)
    assert manifest["page_count"] == 2 and [item["rcp_no"] for item in manifest["items"]] == value["expected_rcp_nos"]
    assert "packet_path" not in json.dumps(manifest)


@pytest.mark.parametrize("mutate", [lambda value: value.update(control_count=3), lambda value: value.update(expected_rcp_nos=list(reversed(value["expected_rcp_nos"]))), lambda value: value.update(sources=[{**value["sources"][0], "report_class": None}])])
def test_compact_queue_fails_closed_for_malformed_or_nonexact_control(tmp_path, mutate):
    root, value, digest, _ = contract(tmp_path); mutate(value)
    (root / (value["run_key"] + ".json")).write_bytes(queue.canonical_bytes(value) + b"\n")
    with pytest.raises(queue.ReviewQueueError): queue.compact_review_queue(value["run_key"], digest, control_root=root)


def test_packet_hash_mutation_is_rejected_before_packet_open(tmp_path, monkeypatch):
    root, value, digest, source_root = contract(tmp_path); source = value["sources"][0]
    Path(source["packet_path"]).write_bytes(b"mutated")
    called = []
    monkeypatch.setattr(queue, "completed_packet", lambda *args, **kwargs: called.append(args) or None)
    with pytest.raises(queue.ReviewQueueError, match="hash mismatch"):
        queue.open_review_packet(value["run_key"], digest, source["rcp_no"], control_root=root, source_root=source_root)
    assert called == []


def test_packet_and_text_symlink_and_escape_are_rejected(tmp_path, monkeypatch):
    root, value, digest, source_root = contract(tmp_path); source = value["sources"][0]
    outside = tmp_path / "outside.json"; outside.write_bytes(Path(source["packet_path"]).read_bytes())
    Path(source["packet_path"]).unlink(); Path(source["packet_path"]).symlink_to(outside)
    with pytest.raises(queue.ReviewQueueError): queue.open_review_packet(value["run_key"], digest, source["rcp_no"], control_root=root, source_root=source_root)
    # Restore a packet, then attack the separately opened text artifact.
    Path(source["packet_path"]).unlink(); Path(source["packet_path"]).write_bytes(b"packet-metadata")
    text = source_root / "20260923" / "20260923000001.viewer.txt"; text.write_text("<p>visible</p>", encoding="utf-8")
    monkeypatch.setattr(queue, "completed_packet", lambda *args, **kwargs: {"text_path": str(text), "text_sha256": hashlib.sha256(text.read_bytes()).hexdigest()})
    text.unlink(); text.symlink_to(outside)
    with pytest.raises(queue.ReviewQueueError): queue.open_review_packet(value["run_key"], digest, source["rcp_no"], control_root=root, source_root=source_root)


def test_compact_visible_text_preserves_visible_order_and_boundaries():
    document = "<div>첫째 &amp; one</div><table><tr><th>헤더</th><td>값</td></tr></table><script>omit()</script><style>.x{}</style><p>교정 전</p><p>교정 후</p>"
    assert queue.compact_visible_text(document) == "첫째 & one\n헤더\n값\n교정 전\n교정 후\n"
    for bad in ("<script>hidden", "<p>\ufffd</p>", "<div>   </div>"):
        with pytest.raises(queue.ReviewQueueError): queue.compact_visible_text(bad)


def test_full_packet_returns_compact_text_with_hashes_and_completion(tmp_path, monkeypatch):
    root, value, digest, source_root = contract(tmp_path); source = value["sources"][0]
    text = source_root / "20260923" / "20260923000001.viewer.txt"; text.write_text("<p>full <b>immutable</b> source</p>", encoding="utf-8")
    monkeypatch.setattr(queue, "completed_packet", lambda *args, **kwargs: {"text_path": str(text), "text_sha256": hashlib.sha256(text.read_bytes()).hexdigest()})
    result = queue.open_review_packet(value["run_key"], digest, source["rcp_no"], control_root=root, source_root=source_root)
    assert result["text"] == "full immutable source\n" and result["compaction_completed"] is True
    assert result["compact_text_sha256"] == hashlib.sha256(result["text"].encode()).hexdigest() and "packet_path" not in json.dumps(result)


def test_handle_batch_loads_exact_contract_and_does_not_accept_source_reconstruction(tmp_path):
    root, value, digest, _ = contract(tmp_path)
    audits = {"20260923000001": {"source_url": "https://example.com/a", "economic_disposition": "below_threshold", "economic_reason": "not material", "disposition": "rejected"}, "20260923000002": {}}
    result = queue.terminal_batch_handle(value["run_key"], digest, audits, control_root=root)
    assert result["exact_receipt_order"] == value["expected_rcp_nos"]
    assert [item["rcp_no"] for item in result["terminal_items"]] == value["expected_rcp_nos"]
    with pytest.raises(queue.ReviewQueueError): queue.terminal_batch_handle(value["run_key"], digest, {"20260923000001": audits["20260923000001"]}, control_root=root)


def test_prehook_compact_payload_is_bounded_and_has_no_contract_or_paths():
    payload = queue.compact_gate_payload("research-2026-09-23-0700-kst", "a" * 64, 222, 219, 3)
    assert payload["compaction_required"] is True and len(json.dumps(payload, ensure_ascii=False).encode()) < 350 and "packet_path" not in json.dumps(payload)


def test_cli_manifest_represents_complete_path_free_control(tmp_path):
    root, value, digest, _ = contract(tmp_path)
    env = {**os.environ, "GIRAFFE_DART_CONTROL_ROOT": str(root)}
    completed = subprocess.run([sys.executable, "-m", "kr_stock_autotrader.cli", "giraffe-review-manifest", value["run_key"], digest, "--page-size", "1"], env=env, text=True, capture_output=True, check=True)
    result = json.loads(completed.stdout)
    assert result["page_count"] == 2 and "packet_path" not in completed.stdout
