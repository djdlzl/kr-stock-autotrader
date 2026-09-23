"""Bounded on-demand views of immutable Giraffe 07:00 source controls."""
from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any

from scripts.giraffe_dart_source import completed_packet

CONTROL_ROOT = Path(os.environ.get("GIRAFFE_DART_CONTROL_ROOT", str(Path.home() / ".hermes" / "runs" / "giraffe-7923" / "dart-control-contracts")))
_RUN_KEY = re.compile(r"^research-\d{4}-\d{2}-\d{2}-0700-kst(?:-r[1-9]\d*)?$")
_RECEIPT = re.compile(r"^\d{14}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SOURCE_ERRORS = frozenset({"SOURCE_FETCH_ERROR", "SOURCE_EXTRACT_ERROR", "SOURCE_DECODE_ERROR"})


class ReviewQueueError(ValueError):
    """An untrusted compact-review handle or immutable contract failed validation."""


def canonical_bytes(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def compact_gate_payload(run_key: str, digest: str, control_count: int,
                         source_valid_count: int, source_error_count: int) -> dict[str, object]:
    """The entire LLM-visible successful prehook payload; full contract stays local."""
    if (not _RUN_KEY.fullmatch(run_key) or not _SHA256.fullmatch(digest)
            or any(not isinstance(value, int) or isinstance(value, bool) or value < 0
                   for value in (control_count, source_valid_count, source_error_count))
            or source_valid_count + source_error_count != control_count):
        raise ReviewQueueError("invalid compact prehook payload")
    return {"gate": "GIRAFFE_DART_GATE_V1", "complete": True, "run_key": run_key,
            "control_contract_sha256": digest, "control_count": control_count,
            "source_valid_count": source_valid_count, "source_error_count": source_error_count}


def _load_contract(run_key: str, digest: str, control_root: Path) -> dict[str, Any]:
    if not _RUN_KEY.fullmatch(run_key) or not _SHA256.fullmatch(digest):
        raise ReviewQueueError("invalid compact review handle")
    path = control_root / f"{run_key}.json"
    try:
        raw = path.read_bytes()
        contract = json.loads(raw)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ReviewQueueError("immutable control contract unavailable") from exc
    canonical = canonical_bytes(contract)
    if raw != canonical + b"\n" or hashlib.sha256(canonical).hexdigest() != digest:
        raise ReviewQueueError("immutable control contract hash mismatch")
    _validate_contract(contract, run_key)
    return contract


def _validate_contract(contract: object, run_key: str) -> None:
    if not isinstance(contract, dict) or contract.get("schema_version") != "giraffe-research-control-v3" or contract.get("run_key") != run_key:
        raise ReviewQueueError("invalid immutable control contract")
    sources, expected = contract.get("sources"), contract.get("expected_rcp_nos")
    count = contract.get("control_count")
    if (not isinstance(sources, list) or not isinstance(expected, list)
            or not isinstance(count, int) or isinstance(count, bool) or count < 0
            or count != len(sources) or len(expected) != len(sources)):
        raise ReviewQueueError("control count mismatch")
    receipts, valid, errors = [], 0, 0
    for source in sources:
        if not isinstance(source, dict):
            raise ReviewQueueError("malformed compact review item")
        rcp_no, date, receipt_date = source.get("rcp_no"), source.get("date"), source.get("receipt_source_date")
        report_class, report_name = source.get("report_class"), source.get("report_name")
        if (not isinstance(rcp_no, str) or not _RECEIPT.fullmatch(rcp_no)
                or not isinstance(date, str) or not re.fullmatch(r"\d{8}", date)
                or not isinstance(receipt_date, str) or receipt_date != rcp_no[:8]
                or report_class is not None and not isinstance(report_class, str)
                or report_name is not None and not isinstance(report_name, str)):
            raise ReviewQueueError("malformed compact review item")
        has_packet = "packet_path" in source or "packet_sha256" in source
        has_error = "source_error_code" in source
        if has_packet == has_error:
            raise ReviewQueueError("ambiguous compact review item")
        if has_packet:
            if (not isinstance(source.get("packet_path"), str) or not isinstance(source.get("packet_sha256"), str)
                    or not _SHA256.fullmatch(source["packet_sha256"])):
                raise ReviewQueueError("malformed packet provenance")
            valid += 1
        elif source.get("source_error_code") not in _SOURCE_ERRORS:
            raise ReviewQueueError("malformed source error provenance")
        else:
            errors += 1
        receipts.append(rcp_no)
    if (receipts != expected or receipts != sorted(receipts) or len(set(receipts)) != len(receipts)
            or valid + errors != count):
        raise ReviewQueueError("exact review set mismatch")


def _queue_item(source: dict[str, Any], position: int) -> dict[str, object]:
    return {"position": position, "rcp_no": source["rcp_no"], "date": source["date"],
            "receipt_source_date": source["receipt_source_date"], "report_class": source.get("report_class"),
            "report_name": source.get("report_name"),
            "source_state": "packet" if "packet_path" in source else "source_error"}


def compact_review_queue(run_key: str, digest: str, *, control_root: Path = CONTROL_ROOT,
                         offset: int = 0, limit: int = 25) -> dict[str, object]:
    """Return a deterministic bounded summary without source text or packet paths."""
    if (not isinstance(offset, int) or isinstance(offset, bool) or offset < 0
            or not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100):
        raise ReviewQueueError("invalid compact review page")
    contract = _load_contract(run_key, digest, control_root)
    sources = contract["sources"]
    page = sources[offset:offset + limit]
    valid = sum("packet_path" in item for item in sources)
    return {"schema_version": "giraffe-compact-review-queue-v1", "run_key": run_key,
            "control_contract_sha256": digest, "control_count": len(sources), "source_valid_count": valid,
            "source_error_count": len(sources) - valid, "offset": offset, "limit": limit,
            "remaining": max(0, len(sources) - offset - len(page)),
            "items": [_queue_item(item, offset + index) for index, item in enumerate(page)]}


def open_review_packet(run_key: str, digest: str, rcp_no: str, *, control_root: Path = CONTROL_ROOT) -> dict[str, object]:
    """Open one full source only after its compact item was selected for review."""
    if not isinstance(rcp_no, str) or not _RECEIPT.fullmatch(rcp_no):
        raise ReviewQueueError("invalid review receipt")
    contract = _load_contract(run_key, digest, control_root)
    matches = [(index, source) for index, source in enumerate(contract["sources"]) if source["rcp_no"] == rcp_no]
    if len(matches) != 1:
        raise ReviewQueueError("review receipt is outside exact control set")
    position, source = matches[0]
    if "packet_path" not in source:
        raise ReviewQueueError("source-error receipt has no readable packet")
    metadata = completed_packet(Path(source["packet_path"]), rcp_no, expected_control_date=source["date"])
    if metadata is None:
        raise ReviewQueueError("immutable source packet failed provenance validation")
    try:
        text = Path(metadata["text_path"]).read_text(encoding="utf-8")
    except (KeyError, OSError, UnicodeDecodeError) as exc:
        raise ReviewQueueError("immutable source text unavailable") from exc
    return {"schema_version": "giraffe-full-review-packet-v1", "run_key": run_key,
            "control_contract_sha256": digest, "position": position, "source": source,
            "packet_metadata": metadata, "text": text}
