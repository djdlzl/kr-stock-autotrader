import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from kr_stock_autotrader import giraffe_review_queue as queue
from scripts.giraffe_dart_source import source_packet, write_packet


def valid_packet(rcp_no: str) -> dict:
    main = f"<html><meta charset='utf-8'><script>viewDoc('{rcp_no}','11577485','0','0','0','HTML','')</script></html>".encode()
    viewer = b"<html><meta charset='utf-8'><body>valid DART disclosure source body for control test</body></html>"
    main_url = "https://dart.fss.or.kr/dsaf001/main.do?rcpNo=" + rcp_no
    viewer_url = "https://dart.fss.or.kr/report/viewer.do?rcpNo=" + rcp_no + "&dcmNo=11577485&eleId=0&offset=0&length=0&dtd=HTML"
    return source_packet(rcp_no, lambda url: (main, "text/html; charset=utf-8", main_url) if "main.do" in url else (viewer, "text/html; charset=utf-8", viewer_url))


def contract(tmp_path: Path, *, sources=None):
    packet = write_packet(valid_packet("20260923000001"), tmp_path / "source" / "20260923")
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


def test_packet_hash_mutation_is_rejected_before_snapshot_validation(tmp_path):
    root, value, digest, source_root = contract(tmp_path); source = value["sources"][0]
    Path(source["packet_path"]).write_bytes(b"mutated")
    with pytest.raises(queue.ReviewQueueError, match="hash mismatch"):
        queue.open_review_packet(value["run_key"], digest, source["rcp_no"], control_root=root, source_root=source_root)


def test_packet_and_text_symlink_and_escape_are_rejected(tmp_path):
    root, value, digest, source_root = contract(tmp_path); source = value["sources"][0]
    outside = tmp_path / "outside.json"; outside.write_bytes(Path(source["packet_path"]).read_bytes())
    Path(source["packet_path"]).unlink(); Path(source["packet_path"]).symlink_to(outside)
    with pytest.raises(queue.ReviewQueueError): queue.open_review_packet(value["run_key"], digest, source["rcp_no"], control_root=root, source_root=source_root)
    # Restore a trusted packet, then attack a sibling opened from its snapshot.
    Path(source["packet_path"]).unlink(); Path(source["packet_path"]).write_bytes(outside.read_bytes())
    text = source_root / "20260923" / "20260923000001.viewer.txt"
    text.unlink(); text.symlink_to(outside)
    with pytest.raises(queue.ReviewQueueError): queue.open_review_packet(value["run_key"], digest, source["rcp_no"], control_root=root, source_root=source_root)


def test_compact_visible_text_preserves_visible_order_boundaries_and_single_entity_decode():
    document = "<div>첫째 &amp; one</div><table><tr><th>헤더</th><td>값</td></tr></table><script>omit()</script><style>.x{}</style><p>교정 전</p><p>교정 후</p>"
    assert queue.compact_visible_text(document) == "첫째 & one\n헤더\n값\n교정 전\n교정 후\n"
    entities = "<p>기재정정 &amp; &#xAC00; &#44032; &amp;amp;#xAC00;</p><p>교정 전</p><p>교정 후</p>"
    assert queue.compact_visible_text(entities) == "기재정정 & 가 가 &amp;#xAC00;\n교정 전\n교정 후\n"
    for bad in ("<script>hidden", "<p>\ufffd</p>", "<div>   </div>"):
        with pytest.raises(queue.ReviewQueueError): queue.compact_visible_text(bad)


def test_full_packet_returns_compact_text_with_hashes_and_completion(tmp_path):
    root, value, digest, source_root = contract(tmp_path); source = value["sources"][0]
    result = queue.open_review_packet(value["run_key"], digest, source["rcp_no"], control_root=root, source_root=source_root)
    assert result["text"] == "valid DART disclosure source body for control test\n" and result["compaction_completed"] is True
    assert result["compact_text_sha256"] == hashlib.sha256(result["text"].encode()).hexdigest() and "packet_path" not in json.dumps(result)


def test_packet_replacement_race_cannot_mix_old_packet_with_replacement_artifacts(tmp_path, monkeypatch):
    root, value, digest, source_root = contract(tmp_path); source = value["sources"][0]
    original = queue._read_regular
    swapped = False
    def racing_read(path, trusted_root):
        nonlocal swapped
        data = original(path, trusted_root)
        if path == Path(source["packet_path"]) and not swapped:
            swapped = True
            replacement = valid_packet(source["rcp_no"])
            replacement["text"] = "<html><meta charset='utf-8'><body>replacement attacker text</body></html>"
            write_packet(replacement, path.parent)
        return data
    monkeypatch.setattr(queue, "_read_regular", racing_read)
    with pytest.raises(queue.ReviewQueueError, match="provenance"):
        queue.open_review_packet(value["run_key"], digest, source["rcp_no"], control_root=root, source_root=source_root)


def test_handle_batch_loads_exact_contract_and_does_not_accept_source_reconstruction(tmp_path):
    root, value, digest, _ = contract(tmp_path)
    audits = {"20260923000001": {"source_url": "https://example.com/a", "economic_disposition": "below_threshold", "economic_reason": "not material", "disposition": "rejected"}, "20260923000002": {}}
    result = queue.terminal_batch_handle(value["run_key"], digest, audits, control_root=root)
    assert result["exact_receipt_order"] == value["expected_rcp_nos"]
    assert [item["rcp_no"] for item in result["terminal_items"]] == value["expected_rcp_nos"]
    with pytest.raises(queue.ReviewQueueError): queue.terminal_batch_handle(value["run_key"], digest, {"20260923000001": audits["20260923000001"]}, control_root=root)




@pytest.mark.parametrize("error_position", [0, 1, 2])
def test_handle_batch_merges_source_errors_in_original_contract_order(tmp_path, error_position):
    packet_source = {"rcp_no": "20260923000001", "date": "20260923", "receipt_source_date": "20260923", "report_class": "other", "report_name": "기타", "packet_path": str(tmp_path / "source" / "20260923" / "20260923000001.json"), "packet_sha256": "a" * 64}
    sources = []
    for number in range(1, 4):
        rcp = f"2026092300000{number}"
        if number - 1 == error_position:
            sources.append({"rcp_no": rcp, "date": "20260923", "receipt_source_date": "20260923", "report_class": "other", "report_name": "기타", "source_error_code": "SOURCE_FETCH_ERROR"})
        else:
            sources.append({**packet_source, "rcp_no": rcp})
    root, value, digest, _ = contract(tmp_path, sources=sources)
    audits = {source["rcp_no"]: {} if "source_error_code" in source else {"source_url": "https://example.com/" + source["rcp_no"], "economic_disposition": "below_threshold", "economic_reason": "not material", "disposition": "rejected"} for source in sources}
    result = queue.terminal_batch_handle(value["run_key"], digest, audits, control_root=root)
    assert [item["rcp_no"] for item in result["terminal_items"]] == value["expected_rcp_nos"]
    assert result["terminal_items"][error_position]["disposition"] == "source_error"


def test_handle_batch_rejects_duplicate_missing_or_extra_terminal_results(tmp_path, monkeypatch):
    root, value, digest, _ = contract(tmp_path)
    import kr_stock_autotrader.giraffe_terminal_audit as terminal
    monkeypatch.setattr(terminal, "terminal_audit_batch", lambda *_: {"terminal_items": [{"rcp_no": "20260923000001"}, {"rcp_no": "20260923000001"}], "evidence_requirements": []})
    with pytest.raises(queue.ReviewQueueError, match="exact source set"):
        queue.terminal_batch_handle(value["run_key"], digest, {"20260923000001": {"source_url": "https://example.com/a", "economic_disposition": "below_threshold", "economic_reason": "not material", "disposition": "rejected"}, "20260923000002": {}}, control_root=root)


def test_prehook_compact_payload_is_bounded_and_has_no_contract_or_paths():
    payload = queue.compact_gate_payload("research-2026-09-23-0700-kst", "a" * 64, 222, 219, 3)
    assert payload["compaction_required"] is True and len(json.dumps(payload, ensure_ascii=False).encode()) < 350 and "packet_path" not in json.dumps(payload)




def test_measurement_is_explicit_source_intake_proxy_with_exclusions(tmp_path):
    root, value, _, _ = contract(tmp_path)
    output = subprocess.run([sys.executable, "scripts/giraffe_token_surface_measure.py", str(root / (value["run_key"] + ".json"))], cwd=Path(__file__).parents[1], text=True, capture_output=True, check=True)
    measured = json.loads(output.stdout)
    assert measured["metric_scope"] == "source-intake/operator-protocol proxy"
    assert "not tokenizer measurement" in measured["metric"]
    assert "scheduler-readback detail" in measured["excluded_components"]
    assert "common agent-generated audits" in measured["excluded_components"]


def test_cli_manifest_represents_complete_path_free_control(tmp_path):
    root, value, digest, _ = contract(tmp_path)
    env = {**os.environ, "GIRAFFE_DART_CONTROL_ROOT": str(root)}
    completed = subprocess.run([sys.executable, "-m", "kr_stock_autotrader.cli", "giraffe-review-manifest", value["run_key"], digest, "--page-size", "1"], env=env, text=True, capture_output=True, check=True)
    result = json.loads(completed.stdout)
    assert result["page_count"] == 2 and "packet_path" not in completed.stdout
