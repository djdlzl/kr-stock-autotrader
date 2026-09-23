"""Fail-closed, compact views of immutable Giraffe 07:00 source controls."""
from __future__ import annotations

import hashlib
import html
import json
import os
import re
import stat
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from scripts.giraffe_dart_source import completed_packet

CONTROL_ROOT = Path(os.environ.get("GIRAFFE_DART_CONTROL_ROOT", str(Path.home() / ".hermes" / "runs" / "giraffe-7923" / "dart-control-contracts")))
SOURCE_ROOT = Path(os.environ.get("GIRAFFE_DART_SOURCE_ROOT", str(Path.home() / ".hermes" / "runs" / "giraffe-7923" / "dart-source-packets")))
_RUN_KEY = re.compile(r"^research-\d{4}-\d{2}-\d{2}-0700-kst(?:-r[1-9]\d*)?$")
_RECEIPT = re.compile(r"^\d{14}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SOURCE_ERRORS = frozenset({"SOURCE_FETCH_ERROR", "SOURCE_EXTRACT_ERROR", "SOURCE_DECODE_ERROR"})


class ReviewQueueError(ValueError):
    """An untrusted compact-review handle or immutable artifact failed validation."""


def canonical_bytes(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _root(root: Path) -> Path:
    try:
        if root.is_symlink() or not root.is_dir():
            raise ReviewQueueError("approved root unavailable")
        return root.resolve(strict=True)
    except OSError as exc:
        raise ReviewQueueError("approved root unavailable") from exc


def _under(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _read_regular(path: Path, root: Path) -> bytes:
    """Read one non-symlink regular artifact once; digest/parse use these bytes."""
    try:
        parent = path.parent.resolve(strict=True)
        if path.is_symlink() or not _under(parent, root) or path.parent != parent:
            raise ReviewQueueError("artifact path escape or symlink")
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            before = os.fstat(fd)
            if not stat.S_ISREG(before.st_mode):
                raise ReviewQueueError("artifact is not a regular file")
            chunks = []
            while True:
                chunk = os.read(fd, 1024 * 1024)
                if not chunk:
                    break
                chunks.append(chunk)
            after = os.fstat(fd)
        finally:
            os.close(fd)
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
            raise ReviewQueueError("artifact changed while reading")
        return b"".join(chunks)
    except ReviewQueueError:
        raise
    except OSError as exc:
        raise ReviewQueueError("immutable artifact unavailable") from exc


def compact_gate_payload(run_key: str, digest: str, control_count: int, source_valid_count: int, source_error_count: int) -> dict[str, object]:
    if (not _RUN_KEY.fullmatch(run_key) or not _SHA256.fullmatch(digest)
            or any(not isinstance(value, int) or isinstance(value, bool) or value < 0 for value in (control_count, source_valid_count, source_error_count))
            or source_valid_count + source_error_count != control_count):
        raise ReviewQueueError("invalid compact prehook payload")
    return {"gate": "GIRAFFE_DART_GATE_V1", "complete": True, "run_key": run_key, "control_contract_sha256": digest,
            "control_count": control_count, "source_valid_count": source_valid_count, "source_error_count": source_error_count,
            "compaction_required": True}


def _load_contract(run_key: str, digest: str, control_root: Path) -> tuple[dict[str, Any], Path]:
    if not _RUN_KEY.fullmatch(run_key) or not _SHA256.fullmatch(digest):
        raise ReviewQueueError("invalid compact review handle")
    root = _root(control_root)
    path = root / f"{run_key}.json"
    raw = _read_regular(path, root)
    try:
        contract = json.loads(raw.decode("utf-8", "strict"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ReviewQueueError("immutable control contract unavailable") from exc
    canonical = canonical_bytes(contract)
    if raw != canonical + b"\n" or hashlib.sha256(canonical).hexdigest() != digest:
        raise ReviewQueueError("immutable control contract hash mismatch")
    _validate_contract(contract, run_key)
    return contract, root


def _validate_contract(contract: object, run_key: str) -> None:
    if not isinstance(contract, dict) or contract.get("schema_version") != "giraffe-research-control-v3" or contract.get("run_key") != run_key:
        raise ReviewQueueError("invalid immutable control contract")
    sources, expected, count = contract.get("sources"), contract.get("expected_rcp_nos"), contract.get("control_count")
    if not isinstance(sources, list) or not isinstance(expected, list) or not isinstance(count, int) or isinstance(count, bool) or count < 0 or count != len(sources) or len(expected) != len(sources):
        raise ReviewQueueError("control count mismatch")
    receipts, valid, errors = [], 0, 0
    for source in sources:
        if not isinstance(source, dict):
            raise ReviewQueueError("malformed compact review item")
        rcp_no, date, receipt_date = source.get("rcp_no"), source.get("date"), source.get("receipt_source_date")
        if (not isinstance(rcp_no, str) or not _RECEIPT.fullmatch(rcp_no) or not isinstance(date, str) or not re.fullmatch(r"\d{8}", date)
                or receipt_date != rcp_no[:8] or source.get("report_class") is not None and not isinstance(source.get("report_class"), str)
                or source.get("report_name") is not None and not isinstance(source.get("report_name"), str)):
            raise ReviewQueueError("malformed compact review item")
        has_packet, has_error = "packet_path" in source or "packet_sha256" in source, "source_error_code" in source
        if has_packet == has_error:
            raise ReviewQueueError("ambiguous compact review item")
        if has_packet:
            if not isinstance(source.get("packet_path"), str) or not isinstance(source.get("packet_sha256"), str) or not _SHA256.fullmatch(source["packet_sha256"]):
                raise ReviewQueueError("malformed packet provenance")
            valid += 1
        elif source.get("source_error_code") not in _SOURCE_ERRORS:
            raise ReviewQueueError("malformed source error provenance")
        else:
            errors += 1
        receipts.append(rcp_no)
    if receipts != expected or receipts != sorted(receipts) or len(set(receipts)) != len(receipts) or valid + errors != count:
        raise ReviewQueueError("exact review set mismatch")


def _queue_item(source: dict[str, Any], position: int) -> dict[str, object]:
    return {"position": position, "rcp_no": source["rcp_no"], "date": source["date"], "receipt_source_date": source["receipt_source_date"],
            "report_class": source.get("report_class"), "report_name": source.get("report_name"), "source_state": "packet" if "packet_path" in source else "source_error"}


def _queue_items(contract: dict[str, Any]) -> list[dict[str, object]]:
    return [_queue_item(item, index) for index, item in enumerate(contract["sources"])]


def compact_review_queue(run_key: str, digest: str, *, control_root: Path = CONTROL_ROOT, offset: int = 0, limit: int = 25) -> dict[str, object]:
    if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0 or not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100:
        raise ReviewQueueError("invalid compact review page")
    contract, _ = _load_contract(run_key, digest, control_root)
    items = _queue_items(contract); page = items[offset:offset + limit]
    valid = sum(item["source_state"] == "packet" for item in items)
    return {"schema_version": "giraffe-compact-review-queue-v2", "run_key": run_key, "control_contract_sha256": digest, "control_count": len(items),
            "source_valid_count": valid, "source_error_count": len(items) - valid, "offset": offset, "limit": limit,
            "remaining": max(0, len(items) - offset - len(page)), "items": page,
            "page_sha256": hashlib.sha256(canonical_bytes(page)).hexdigest()}


def compact_review_manifest(run_key: str, digest: str, *, control_root: Path = CONTROL_ROOT, page_size: int = 25) -> dict[str, object]:
    if not isinstance(page_size, int) or isinstance(page_size, bool) or not 1 <= page_size <= 100:
        raise ReviewQueueError("invalid compact review page")
    contract, _ = _load_contract(run_key, digest, control_root)
    items = _queue_items(contract)
    pages = [items[index:index + page_size] for index in range(0, len(items), page_size)]
    page_hashes = [hashlib.sha256(canonical_bytes(page)).hexdigest() for page in pages]
    return {"schema_version": "giraffe-compact-review-manifest-v1", "run_key": run_key, "control_contract_sha256": digest,
            "control_count": len(items), "page_size": page_size, "page_count": len(pages), "page_sha256": page_hashes,
            "page_chain_sha256": hashlib.sha256(canonical_bytes(page_hashes)).hexdigest(), "items": items}


class _VisibleText(HTMLParser):
    _BREAKS = frozenset({"p", "div", "br", "li", "tr", "table", "section", "article", "header", "footer", "h1", "h2", "h3", "h4", "h5", "h6", "td", "th"})
    _VOID = frozenset({"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"})
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True); self.parts: list[str] = []; self.hidden = 0; self.stack: list[str] = []
    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag not in self._VOID: self.stack.append(tag)
        if tag in {"script", "style"}: self.hidden += 1
        if tag in self._BREAKS: self.parts.append("\n")
    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() in self._BREAKS: self.parts.append("\n")
    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag not in self._VOID:
            # DART viewer HTML relies on browser optional-end-tag recovery.
            # Drop through the nearest matching opener instead of discarding
            # visible data; hidden script/style still fails closed below.
            if tag in self.stack:
                del self.stack[len(self.stack) - 1 - self.stack[::-1].index(tag):]
        if tag in {"script", "style"} and self.hidden: self.hidden -= 1
        if tag in self._BREAKS: self.parts.append("\n")
    def handle_data(self, data: str) -> None:
        if not self.hidden: self.parts.append(data)


def compact_visible_text(document: str) -> str:
    """Loss-aware HTML compaction: every visible text node, in document order."""
    if not isinstance(document, str) or "\ufffd" in document or "ï¿½" in document or "占쏙옙" in document:
        raise ReviewQueueError("source encoding is unsafe for compaction")
    parser = _VisibleText()
    try:
        parser.feed(document); parser.close()
    except Exception as exc:
        raise ReviewQueueError("malformed HTML compaction failed") from exc
    # HTML from DART uses browser-valid optional table/tag closures.  The
    # parser remains loss-aware; only a still-hidden script/style suffix is
    # unsafe because it would conceal following visible text.
    if parser.hidden:
        raise ReviewQueueError("malformed HTML compaction failed")
    text = "\n".join(" ".join(line.split()) for line in "".join(parser.parts).splitlines() if " ".join(line.split()))
    if not text or not re.search(r"[가-힣A-Za-z0-9]", text):
        raise ReviewQueueError("empty visible source text")
    # One terminal separator makes individual packet boundaries explicit when
    # compact texts are concatenated into a review context.
    return html.unescape(text) + "\n"


def _packet_artifacts(source: dict[str, Any], source_root: Path) -> tuple[Path, Path]:
    packet = Path(source["packet_path"])
    try:
        lexical = packet.absolute()
        resolved_parent = packet.parent.resolve(strict=True)
    except OSError as exc:
        raise ReviewQueueError("immutable source packet unavailable") from exc
    if packet.is_symlink() or not _under(lexical, source_root) or not _under(resolved_parent, source_root) or packet.parent != resolved_parent:
        raise ReviewQueueError("source packet path escape or symlink")
    return packet, source_root


def open_review_packet(run_key: str, digest: str, rcp_no: str, *, control_root: Path = CONTROL_ROOT, source_root: Path = SOURCE_ROOT) -> dict[str, object]:
    if not isinstance(rcp_no, str) or not _RECEIPT.fullmatch(rcp_no):
        raise ReviewQueueError("invalid review receipt")
    contract, _ = _load_contract(run_key, digest, control_root)
    matches = [(i, source) for i, source in enumerate(contract["sources"]) if source["rcp_no"] == rcp_no]
    if len(matches) != 1: raise ReviewQueueError("review receipt is outside exact control set")
    position, source = matches[0]
    if "packet_path" not in source: raise ReviewQueueError("source-error receipt has no readable packet")
    root = _root(source_root); packet, _ = _packet_artifacts(source, root)
    packet_bytes = _read_regular(packet, root)
    if hashlib.sha256(packet_bytes).hexdigest() != source["packet_sha256"]:
        raise ReviewQueueError("source packet artifact hash mismatch")
    # completed_packet independently revalidates metadata and raw/text hashes.
    metadata = completed_packet(packet, rcp_no, expected_control_date=source["date"])
    if metadata is None: raise ReviewQueueError("immutable source packet failed provenance validation")
    text_path = Path(metadata.get("text_path", ""))
    text_bytes = _read_regular(text_path, root)
    try: document = text_bytes.decode("utf-8", "strict")
    except UnicodeDecodeError as exc: raise ReviewQueueError("immutable source text unavailable") from exc
    if hashlib.sha256(document.encode("utf-8")).hexdigest() != metadata.get("text_sha256"):
        raise ReviewQueueError("immutable source text hash mismatch")
    compact = compact_visible_text(document)
    return {"schema_version": "giraffe-compact-review-packet-v2", "run_key": run_key, "control_contract_sha256": digest,
            "position": position, "source": _queue_item(source, position), "packet_sha256": source["packet_sha256"],
            "raw_text_sha256": metadata["text_sha256"], "compact_text_sha256": hashlib.sha256(compact.encode()).hexdigest(),
            "compaction_completed": True, "text": compact}


def terminal_batch_handle(run_key: str, digest: str, audits: object, *, control_root: Path = CONTROL_ROOT) -> dict[str, object]:
    """Exact-set adapter: caller supplies only audit map, never reconstructed sources."""
    from kr_stock_autotrader.giraffe_terminal_audit import TerminalAuditError, terminal_audit_batch
    contract, _ = _load_contract(run_key, digest, control_root)
    sources = contract["sources"]
    if not isinstance(audits, dict) or set(audits) != {source["rcp_no"] for source in sources}:
        raise ReviewQueueError("audit map receipt IDs are not the exact source set")
    semantic_sources, semantic_audits, automatic = [], {}, []
    for source in sources:
        rcp_no = source["rcp_no"]
        if "source_error_code" in source:
            if audits[rcp_no] != {}: raise ReviewQueueError("source-error audit must be empty object")
            automatic.append({"rcp_no": rcp_no, "disposition": "source_error", "evidence_id": None, "source_error_code": source["source_error_code"]})
        else:
            semantic_sources.append(source); semantic_audits[rcp_no] = audits[rcp_no]
    try:
        result = terminal_audit_batch(semantic_sources, semantic_audits) if semantic_sources else {"terminal_items": [], "evidence_requirements": []}
    except TerminalAuditError as exc:
        raise ReviewQueueError(str(exc)) from exc
    return {"schema_version": "giraffe-terminal-batch-handle-v1", "run_key": run_key, "control_contract_sha256": digest,
            "terminal_items": [*result["terminal_items"], *automatic], "evidence_requirements": result["evidence_requirements"],
            "exact_receipt_order": [source["rcp_no"] for source in sources]}
