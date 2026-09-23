#!/usr/bin/env python3
"""Cron prehook: collect complete previous/current KST DART manifests for Giraffe."""

from __future__ import annotations

import json
import hashlib
import hmac
import os
import re
import sys
import urllib.request
import urllib.error
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from zoneinfo import ZoneInfo

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
sys.path.insert(0, str(SCRIPT_DIR.parent))
from giraffe_dart_manifest import ManifestError, collect_manifest  # noqa: E402
from giraffe_dart_source import SourceError, RetainedPacketSnapshot, completed_packet_snapshot, fetch_with_retry, write_packet  # noqa: E402
from kr_stock_autotrader.dart_report_classification import authoritative_report_class  # noqa: E402
from kr_stock_autotrader.giraffe_review_queue import compact_gate_payload  # noqa: E402
from kr_stock_autotrader.krx_calendar import CalendarError, admitted_backlog_dates  # noqa: E402

OUTPUT_ROOT = Path.home() / ".hermes" / "runs" / "giraffe-7923" / "dart-manifests"
SOURCE_ROOT = Path.home() / ".hermes" / "runs" / "giraffe-7923" / "dart-source-packets"
CONTROL_ROOT = Path.home() / ".hermes" / "runs" / "giraffe-7923" / "dart-control-contracts"
CARD_PROMPT_PATH = SCRIPT_DIR.parent / "prompts" / "giraffe-decision-card-scheduler-v1.md"
CARD_PROMPT_SHA256 = "287972a1b03bc986905cb86e62575451ecde11893bbe786c0a2fc216826dfb38"


def check_card_prompt() -> None:
    """Do not emit 07 authority that an altered 08 consumer could use."""
    try:
        actual = hashlib.sha256(CARD_PROMPT_PATH.read_bytes()).hexdigest()
    except OSError as exc:
        raise ManifestError("08 prompt unavailable") from exc
    if actual != CARD_PROMPT_SHA256:
        raise ManifestError("08 prompt integrity mismatch")


def validated_packet(path: Path, rcp_no: str, control_date: str):
    """Return metadata and digest of one retained, verified generation."""
    try:
        root = path.parent.parent if re.fullmatch(r"v3-[0-9a-f]{32}", path.parent.name) else path.parent
        if root.name != control_date:
            return None
        with RetainedPacketSnapshot(path, trusted_root=root) as snapshot:
            metadata = completed_packet_snapshot(path, rcp_no, snapshot.packet_bytes,
                                                 snapshot.read_sibling, expected_control_date=control_date)
            if metadata is None:
                return None
            snapshot.verify()
            return metadata, hashlib.sha256(snapshot.packet_bytes).hexdigest()
    except (OSError, ValueError, SourceError):
        return None


def generation_checkpoint(directory: Path, rcp_no: str) -> Path | None:
    """Reuse a complete published generation; never rewrite canonical v2 history."""
    for path in sorted(directory.resolve().glob("v3-*/" + rcp_no + ".json")):
        if re.fullmatch(r"v3-[0-9a-f]{32}", path.parent.name) and validated_packet(path, rcp_no, directory.name):
            return path
    return None


def canonical_bytes(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def canonical_discovery_url(value: object) -> str | None:
    """Mirror the control API's canonical coverage URL authority."""
    if (not isinstance(value, str) or not value or len(value) > 2000
            or any(ord(char) <= 32 or ord(char) == 127 for char in value)):
        return None
    try:
        parsed = urlsplit(value); host, port = parsed.hostname, parsed.port
    except ValueError:
        return None
    if (parsed.scheme != "https" or host is None or parsed.username is not None
            or parsed.password is not None or parsed.fragment):
        return None
    try:
        canonical_host = host.rstrip(".").encode("idna").decode("ascii").lower()
    except UnicodeError:
        return None
    if not canonical_host:
        return None
    if ":" in canonical_host:
        canonical_host = "[" + canonical_host + "]"
    def normalized_percent(component: str) -> str | None:
        pieces, index = [], 0
        while index < len(component):
            character = component[index]
            if character != "%":
                pieces.append(character); index += 1; continue
            if index + 2 >= len(component) or re.fullmatch(r"[0-9A-Fa-f]{2}", component[index + 1:index + 3]) is None:
                return None
            code = int(component[index + 1:index + 3], 16); decoded = chr(code)
            pieces.append(decoded if decoded.isascii() and (decoded.isalnum() or decoded in "-._~") else "%" + component[index + 1:index + 3].upper())
            index += 3
        return "".join(pieces)
    path, query = normalized_percent(parsed.path), normalized_percent(parsed.query)
    if path is None or query is None:
        return None
    hostport = canonical_host if port in (None, 443) else canonical_host + ":" + str(port)
    return urlunsplit(("https", hostport, "" if path in ("", "/") else path, query, ""))


def valid_discovery_timestamp(value: object) -> bool:
    if (not isinstance(value, str) or not value or len(value) > 64
            or any(ord(char) < 32 or ord(char) == 127 for char in value)):
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() is not None


def valid_research_run_key(value: object) -> bool:
    match = re.fullmatch(r"research-(\d{4}-\d{2}-\d{2})-0700-kst(?:-r([1-9]\d*))?", value) if isinstance(value, str) else None
    if match is None:
        return False
    try:
        datetime.strptime(match.group(1), "%Y-%m-%d")
    except ValueError:
        return False
    return True


def bounded_discovery_payload(value: object) -> bool:
    forbidden = re.compile(r"(?:secret|password|token|api[_-]?key|authorization)", re.I)
    def valid(item: object, depth: int = 0) -> bool:
        if depth > 4:
            return False
        if item is None or isinstance(item, bool) or isinstance(item, (int, float)):
            return not isinstance(item, float) or item == item and abs(item) != float("inf")
        if isinstance(item, str):
            return len(item) <= 2000 and not any(ord(char) < 32 for char in item)
        if isinstance(item, list):
            return len(item) <= 32 and all(valid(child, depth + 1) for child in item)
        if isinstance(item, dict):
            return (len(item) <= 32 and all(isinstance(key, str) and 0 < len(key) <= 100
                    and forbidden.search(key) is None and valid(child, depth + 1)
                    for key, child in item.items()))
        return False
    return isinstance(value, dict) and valid(value)


def fetch_research_backlog_state() -> tuple[list[dict], list[dict]]:
    """Read the pending cursor before constructing this run's immutable union."""
    base, key = os.environ.get("GIRAFFE_URL", "").strip().rstrip("/"), os.environ.get("RESEARCH_CONTROL_KEY", "")
    if not base or not key:
        raise ManifestError("GIRAFFE_URL and RESEARCH_CONTROL_KEY are required for durable backlog")
    request = urllib.request.Request(base + "/api/internal/research-backlog", headers={"X-Research-Control-Key": key})
    try:
        with urllib.request.urlopen(request, timeout=10) as response: value = json.load(response)
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ValueError) as exc:
        raise ManifestError("durable research backlog read failed") from exc
    items = value.get("items") if isinstance(value, dict) and value.get("schema_version") == "giraffe-research-backlog-v1" else None
    if (not isinstance(items, list) or any(not isinstance(item, dict) or not isinstance(item.get("identity"), str) for item in items)
            or [item["identity"] for item in items] != sorted(item["identity"] for item in items)):
        raise ManifestError("durable research backlog response invalid")
    return items, []


def fetch_terminal_history(receipts: list[str]) -> list[dict]:
    """Request only the exact current OpenDART set; never scan terminal history."""
    if (not isinstance(receipts, list) or not 0 < len(receipts) <= 200 or receipts != sorted(receipts)
            or len(receipts) != len(set(receipts)) or any(re.fullmatch(r'\d{14}', item) is None for item in receipts)):
        raise ManifestError('invalid bounded terminal lookup request')
    base, key = os.environ.get("GIRAFFE_URL", "").strip().rstrip("/"), os.environ.get("RESEARCH_CONTROL_KEY", "")
    if not base or not key:
        raise ManifestError("GIRAFFE_URL and RESEARCH_CONTROL_KEY are required for durable backlog")
    request = urllib.request.Request(base + '/api/internal/research-backlog/terminal-items', data=canonical_bytes({'rcp_nos': receipts}), method='POST', headers={'Content-Type': 'application/json', 'X-Research-Control-Key': key})
    try:
        with urllib.request.urlopen(request, timeout=10) as response: value = json.load(response)
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ValueError) as exc:
        raise ManifestError('bounded terminal lookup failed') from exc
    items = value.get('items') if isinstance(value, dict) and value.get('schema_version') == 'giraffe-research-terminal-items-v1' and value.get('requested_rcp_nos') == receipts else None
    returned = [(item.get('payload', {}).get('rcp_no'), item.get('identity')) if isinstance(item, dict) and isinstance(item.get('payload'), dict) else (None, None) for item in items] if isinstance(items, list) else []
    if (not isinstance(items, list) or len(items) > 2 * len(receipts) or returned != sorted(returned)
            or len({identity for _, identity in returned}) != len(returned)
            or any(rcp_no not in receipts or not isinstance(identity, str) for rcp_no, identity in returned)):
        raise ManifestError('bounded terminal lookup response invalid')
    return items


def fetch_research_backlog() -> tuple[list[dict], list[dict]]:
    """Read pending work plus terminal audit history for v3 contract assembly."""
    return fetch_research_backlog_state()


def _report_class(record: dict) -> tuple[str, str]:
    """Carry OpenDART's immutable classification into the control contract."""
    name = record.get('report_nm')
    if not isinstance(name, str):
        # Old persisted fixtures have no authoritative listing metadata.  They
        # remain readable here, but v3 registration rejects their omission.
        return ('legacy_unclassified', 'legacy OpenDART report metadata unavailable')
    if not 0 < len(name) <= 500:
        raise ManifestError('OpenDART candidate report name invalid')
    report_class = authoritative_report_class(name)
    if report_class is None:
        raise ManifestError('OpenDART candidate report name invalid')
    return (report_class, name)


_DART_CORE_PROVENANCE_FIELDS = frozenset({"rcp_no", "date", "receipt_source_date", "packet_path", "packet_sha256"})
_SOURCE_ERROR_CODES = frozenset({"SOURCE_FETCH_ERROR", "SOURCE_DECODE_ERROR", "SOURCE_EXTRACT_ERROR"})
_DART_FAILURE_FIELDS = frozenset({"rcp_no", "date", "receipt_source_date", "report_class", "report_name", "source_error_code"})


def failed_dart_source(value: object) -> bool:
    return (isinstance(value, dict) and set(value) == _DART_FAILURE_FIELDS
            and isinstance(value['rcp_no'], str) and re.fullmatch(r'\d{14}', value['rcp_no']) is not None
            and isinstance(value['date'], str) and re.fullmatch(r'\d{8}', value['date']) is not None
            and value['receipt_source_date'] == value['rcp_no'][:8]
            and isinstance(value['source_error_code'], str) and value['source_error_code'] in _SOURCE_ERROR_CODES
            and isinstance(value['report_name'], str) and 0 < len(value['report_name']) <= 500
            and value['report_class'] == authoritative_report_class(value['report_name']))


def same_failed_dart_identity(failed: dict, current: dict) -> bool:
    return all(failed.get(field) == current.get(field) for field in _DART_FAILURE_FIELDS - {'source_error_code'})


def same_dart_core_provenance(left: object, right: object) -> bool:
    """Compare immutable packet provenance without treating v3 classification as carry state."""
    return (isinstance(left, dict) and isinstance(right, dict)
            and all(left.get(field) == right.get(field) for field in _DART_CORE_PROVENANCE_FIELDS))


def terminal_dart_matches_current(terminal: object, current: object) -> bool:
    """Accept immutable v2 provenance and one authoritative v3 class promotion."""
    if not isinstance(terminal, dict) or not isinstance(current, dict):
        return False
    terminal_fields = set(terminal)
    if terminal_fields == _DART_CORE_PROVENANCE_FIELDS:
        return same_dart_core_provenance(terminal, current)
    classified_fields = _DART_CORE_PROVENANCE_FIELDS | {'report_class', 'report_name'}
    if terminal_fields != classified_fields or set(current) != classified_fields:
        return False
    if terminal == current:
        return True
    current_class = current['report_class']
    return (terminal['report_class'] == 'other'
            and isinstance(current_class, str) and current_class != 'other'
            and terminal['report_name'] == current['report_name']
            and current_class == authoritative_report_class(current['report_name'])
            and same_dart_core_provenance(terminal, current))


def correction_dart_matches_current(prior: object, current: object) -> bool:
    """Allow an explicit correction to bind a new generation of the same receipt."""
    if terminal_dart_matches_current(prior, current):
        return True
    if not isinstance(prior, dict) or not isinstance(current, dict):
        return False
    classified = _DART_CORE_PROVENANCE_FIELDS | {'report_class', 'report_name'}
    if set(prior) not in (_DART_CORE_PROVENANCE_FIELDS, classified) or set(current) != classified:
        return False
    if any(prior.get(field) != current.get(field) for field in ('rcp_no', 'date', 'receipt_source_date')):
        return False
    if set(prior) == classified and any(prior[field] != current[field] for field in ('report_class', 'report_name')):
        return False
    if not isinstance(current.get('packet_path'), str) or not isinstance(prior.get('packet_path'), str):
        return False
    path, prior_path = Path(current['packet_path']), Path(prior['packet_path'])
    if (not re.fullmatch(r'v3-[0-9a-f]{32}', path.parent.name)
            or path.parent.parent.name != current['date'] or path.name != current['rcp_no'] + '.json'
            or path == prior_path or '..' in path.parts):
        return False
    validated = validated_packet(prior_path, prior['rcp_no'], prior['date'])
    return validated is not None and validated[1] == prior['packet_sha256']


def normalize_durable_dart_backlog_payload(payload: object, current: object) -> dict | None:
    """Read legacy classified rows only when their immutable metadata matches current authority."""
    if not isinstance(payload, dict):
        return None
    fields = set(payload)
    if fields == _DART_CORE_PROVENANCE_FIELDS:
        return {field: payload[field] for field in _DART_CORE_PROVENANCE_FIELDS}
    classified_fields = _DART_CORE_PROVENANCE_FIELDS | {'report_class', 'report_name'}
    if fields != classified_fields or (current is not None and (not isinstance(current, dict) or set(current) != classified_fields)):
        return None
    report_name, report_class = payload['report_name'], payload['report_class']
    if (not isinstance(report_name, str) or not report_name.strip() or len(report_name) > 500
            or report_class not in {'dart_single_sale_supply_contract', 'other'}
            or report_class != authoritative_report_class(report_name)
            or (current is not None and (payload['report_name'] != current['report_name'] or payload['report_class'] != current['report_class']))):
        return None
    return {field: payload[field] for field in _DART_CORE_PROVENANCE_FIELDS}


def control_contract(run_key: str, summaries: list[dict], carry_forward: list[dict] | None = None,
                     terminal_history: list[dict] | None = None, correction_receipts: list[str] | None = None,
                     retry_failures: dict[str, str] | None = None) -> dict:
    sources = []
    retry_failures = retry_failures or {}
    for summary in summaries:
        control_date = summary.get("date")
        candidates = summary.get("material_candidate_records")
        if (not isinstance(control_date, str) or not re.fullmatch(r"\d{8}", control_date)
                or not isinstance(candidates, list) or not isinstance(summary.get("source_packet_paths"), list)):
            raise ManifestError("DART source summary is invalid")
        candidate_receipts, candidate_metadata = set(), {}
        for candidate in candidates:
            rcp_no = candidate.get("rcp_no") if isinstance(candidate, dict) else None
            rcept_dt = candidate.get("rcept_dt") if isinstance(candidate, dict) else None
            if (not isinstance(rcp_no, str) or not re.fullmatch(r"\d{14}", rcp_no)
                    or not isinstance(rcept_dt, str) or rcept_dt != control_date
                    or rcp_no in candidate_receipts):
                raise ManifestError("DART manifest candidate does not bind to the control date")
            candidate_receipts.add(rcp_no)
            candidate_metadata[rcp_no] = _report_class(candidate)
        packet_receipts, packet_paths = set(), set()
        for packet_path in summary["source_packet_paths"]:
            if not isinstance(packet_path, str):
                raise ManifestError("DART source packet path is invalid")
            path = Path(packet_path)
            path_identity = str(path.resolve())
            if path_identity in packet_paths:
                raise ManifestError("duplicate DART source packet path")
            packet_paths.add(path_identity)
            if path.parent.name != control_date and not (re.fullmatch(r"v3-[0-9a-f]{32}", path.parent.name) and path.parent.parent.name == control_date):
                raise ManifestError("DART source packet path does not bind to the control date")
            rcp_no = path.stem
            if rcp_no in packet_receipts:
                raise ManifestError("duplicate DART source packet receipt")
            packet_receipts.add(rcp_no)
            validated = validated_packet(path, rcp_no, control_date)
            if validated is None:
                raise ManifestError("DART source packet is incomplete or invalid")
            metadata, packet_hash = validated
            source_date = metadata.get("source_date")
            if source_date != rcp_no[:8]:
                raise ManifestError("DART source packet date does not match the control window")
            if rcp_no not in candidate_metadata:
                raise ManifestError("DART candidate/packet receipt sets do not match")
            report_class, report_name = candidate_metadata[rcp_no]
            source = {"rcp_no": rcp_no, "date": control_date, "packet_path": packet_path,
                      "packet_sha256": packet_hash, "receipt_source_date": source_date}
            if report_class != 'legacy_unclassified':
                source.update({"report_class": report_class, "report_name": report_name})
            sources.append(source)
        failures = summary.get('source_errors', [])
        if not isinstance(failures, list):
            raise ManifestError('invalid DART source failure audit')
        failure_receipts = set()
        for failure in failures:
            if (not isinstance(failure, dict) or set(failure) != {'rcp_no', 'code'}
                    or failure.get('rcp_no') not in candidate_receipts
                    or failure['rcp_no'] in failure_receipts or failure['rcp_no'] in packet_receipts
                    or failure.get('code') not in _SOURCE_ERROR_CODES):
                raise ManifestError('invalid DART source failure audit')
            rcp_no = failure['rcp_no']; report_class, report_name = candidate_metadata[rcp_no]
            source = {'rcp_no': rcp_no, 'date': control_date, 'receipt_source_date': rcp_no[:8],
                      'report_class': report_class, 'report_name': report_name, 'source_error_code': failure['code']}
            if not failed_dart_source(source):
                raise ManifestError('invalid DART source failure provenance')
            sources.append(source); failure_receipts.add(rcp_no)
        if packet_receipts | failure_receipts != candidate_receipts:
            raise ManifestError("DART candidate/packet receipt sets do not match")
    carry_forward = carry_forward or []
    terminal_history = terminal_history or []
    carry_items = []
    carried_corrections = {}
    sources_by_receipt = {item["rcp_no"]: item for item in sources}
    if len(sources_by_receipt) != len(sources):
        raise ManifestError("duplicate DART receipt across control dates")
    for item in carry_forward:
        identity, kind, payload = item.get("identity"), item.get("kind"), item.get("payload")
        if not isinstance(identity, str) or any(identity == prior["identity"] for prior in carry_items) or kind not in {"dart", "discovery"}:
            raise ManifestError("durable research backlog item invalid")
        if kind == "dart":
            current = sources_by_receipt.get(payload.get("rcp_no")) if isinstance(payload, dict) else None
            if failed_dart_source(payload):
                if identity != 'dart:' + payload['rcp_no'] or (current is not None and not same_failed_dart_identity(payload, current)):
                    raise ManifestError('conflicting current and carried DART provenance')
                if current is None:
                    checkpoint = generation_checkpoint(SOURCE_ROOT / payload['date'], payload['rcp_no'])
                    validated = validated_packet(checkpoint, payload['rcp_no'], payload['date']) if checkpoint else None
                    if validated is not None:
                        current = {key: value for key, value in payload.items() if key != 'source_error_code'}
                        current.update(packet_path=str(checkpoint), packet_sha256=validated[1])
                    else:
                        current = dict(payload)
                        if payload['rcp_no'] in retry_failures:
                            code = retry_failures[payload['rcp_no']]
                            if code not in _SOURCE_ERROR_CODES:
                                raise ManifestError('invalid DART retry failure code')
                            current['source_error_code'] = code
                    sources_by_receipt[payload['rcp_no']] = current
                carry_items.append({'identity': identity, 'kind': 'dart', 'payload': payload})
                continue
            normalized_payload = normalize_durable_dart_backlog_payload(payload, current)
            if normalized_payload is None:
                raise ManifestError("durable DART backlog source invalid")
            if current is not None and not same_dart_core_provenance(current, normalized_payload):
                raise ManifestError("conflicting current and carried DART provenance")
            validated = validated_packet(Path(normalized_payload["packet_path"]), normalized_payload["rcp_no"], normalized_payload["date"])
            is_correction = re.fullmatch(r'dart:correction:[0-9a-f]{64}', identity) is not None
            if validated is None or validated[1] != normalized_payload["packet_sha256"] or (identity != "dart:" + normalized_payload["rcp_no"] and not is_correction):
                raise ManifestError("durable DART backlog packet unavailable")
            if is_correction:
                carried_corrections[normalized_payload['rcp_no']] = identity
            if current is None:
                sources_by_receipt[normalized_payload["rcp_no"]] = dict(payload)
            carry_items.append({"identity": identity, "kind": "dart", "payload": normalized_payload})
        else:
            if set(item) != {"identity", "kind", "payload", "original_announcement_at", "first_run_key"}:
                raise ManifestError("durable discovery backlog provenance invalid")
            envelope = payload
            if not isinstance(envelope, dict) or set(envelope) != {"source_url", "announcement_at", "payload"}:
                raise ManifestError("durable discovery backlog provenance invalid")
            source_url, announced, nested_payload = envelope["source_url"], envelope["announcement_at"], envelope["payload"]
            canonical_url = canonical_discovery_url(source_url)
            if (canonical_url is None or not valid_discovery_timestamp(announced)
                    or item["original_announcement_at"] != announced or not valid_research_run_key(item["first_run_key"])
                    or not bounded_discovery_payload(nested_payload)):
                raise ManifestError("durable discovery backlog provenance invalid")
            expected = hashlib.sha256(canonical_bytes({"url": canonical_url, "announcement_at": announced})).hexdigest()
            if identity != "discovery:" + expected:
                raise ManifestError("durable discovery backlog identity invalid")
            carry_items.append({"identity": identity, "kind": "discovery", "source_url": canonical_url,
                                "announcement_at": announced, "payload": nested_payload})
    correction_receipts = correction_receipts or []
    if (not isinstance(correction_receipts, list) or correction_receipts != sorted(correction_receipts)
            or len(correction_receipts) != len(set(correction_receipts))
            or any(not isinstance(rcp, str) or re.fullmatch(r'\d{14}', rcp) is None for rcp in correction_receipts)):
        raise ManifestError('invalid correction receipt selection')
    correction_receipts = sorted(set(correction_receipts) | set(carried_corrections))
    terminal_by_receipt, correction_history = {}, {}
    for item in terminal_history:
        required = {"identity", "kind", "payload", "terminal_disposition", "terminal_run_key", "terminal_evidence_id", "terminal_at"}
        payload = item.get("payload") if isinstance(item, dict) else None
        rcp_no = payload.get("rcp_no") if isinstance(payload, dict) else None
        if (not isinstance(item, dict) or set(item) != required or item.get("kind") != "dart"
                or not isinstance(rcp_no, str) or not isinstance(item.get("identity"), str)
                or not (item["identity"] == "dart:" + rcp_no or item["identity"].startswith("dart:correction:"))
                or item.get("terminal_disposition") not in {"saved", "existing", "correction_stored", "rejected", "hold", "source_error", "store_error"}
                or not valid_research_run_key(item.get("terminal_run_key")) or not valid_discovery_timestamp(item.get("terminal_at"))
                or (item.get("terminal_disposition") in {"saved", "existing", "correction_stored"} and (not isinstance(item.get("terminal_evidence_id"), int) or isinstance(item.get("terminal_evidence_id"), bool) or item["terminal_evidence_id"] <= 0))
                or (item.get("terminal_disposition") in {"rejected", "hold", "source_error", "store_error"} and item.get("terminal_evidence_id") is not None)):
            raise ManifestError("durable terminal research history invalid")
        if item['identity'] == 'dart:' + rcp_no:
            if rcp_no in terminal_by_receipt:
                raise ManifestError("duplicate durable terminal research receipt")
            terminal_by_receipt[rcp_no] = item
        elif rcp_no in correction_history:
            raise ManifestError("duplicate durable terminal research receipt")
        else:
            correction_history[rcp_no] = item
    missing_corrections = [rcp for rcp in correction_receipts if rcp not in terminal_by_receipt]
    if (missing_corrections or any(terminal_by_receipt[rcp]['terminal_disposition'] not in {'rejected', 'hold'} for rcp in correction_receipts)
            or any(rcp in correction_history for rcp in correction_receipts)):
        raise ManifestError('selected correction receipt is not a rejected/hold terminal audit')
    exclusions = [terminal_by_receipt[rcp] for rcp in sorted((set(sources_by_receipt) & set(terminal_by_receipt)) - set(correction_receipts))
                  if terminal_by_receipt[rcp]['terminal_disposition'] not in {'source_error', 'store_error'}]
    corrections = [terminal_by_receipt[rcp] for rcp in correction_receipts]
    for rcp, identity in carried_corrections.items():
        expected_identity = 'dart:correction:' + hashlib.sha256(canonical_bytes({'source': sources_by_receipt[rcp], 'prior': terminal_by_receipt[rcp]})).hexdigest()
        if identity != expected_identity:
            raise ManifestError('carried correction identity conflicts with original lineage')
    for items, matches in ((exclusions, terminal_dart_matches_current), (corrections, correction_dart_matches_current)):
        for item in items:
            if not matches(item["payload"], sources_by_receipt[item["payload"]["rcp_no"]]):
                raise ManifestError("terminal research history conflicts with current DART provenance")
    for item in exclusions:
        del sources_by_receipt[item["payload"]["rcp_no"]]
    sources = list(sources_by_receipt.values())
    sources.sort(key=lambda item: item["rcp_no"])
    receipts = [item["rcp_no"] for item in sources]
    if len(receipts) != len(set(receipts)):
        raise ManifestError("duplicate DART receipt across control dates")
    return {"schema_version": "giraffe-research-control-v3", "run_key": run_key,
            "dates": [item["date"] for item in summaries], "source_valid": True,
            "expected_rcp_nos": receipts, "control_count": len(receipts), "sources": sources,
            "carry_forward": sorted(carry_items, key=lambda item: item["identity"]),
            "terminal_exclusions": exclusions, "correction_of": corrections}


def rerun_suffix(version: str) -> str:
    """Validate the raw rerun environment value before any prehook side effect."""
    if version == "":
        return ""
    if not isinstance(version, str) or re.fullmatch(r"[1-9]\d*", version) is None:
        raise ManifestError("GIRAFFE_RESEARCH_RERUN_VERSION must be a strict positive integer")
    return f"-r{version}"


def research_run_key(today: str, rerun: str = "") -> str:
    """Construct canonical identity from an already validated rerun suffix."""
    if not isinstance(today, str) or re.fullmatch(r"\d{8}", today) is None:
        raise ManifestError("invalid research run date")
    return f"research-{today[:4]}-{today[4:6]}-{today[6:]}-0700-kst{rerun}"



def register_research_run(run_key: str, contract: dict) -> None:
    """Register the immutable canonical run before any agent/LLM work begins."""
    base, key = os.environ.get("GIRAFFE_URL", "").strip().rstrip("/"), os.environ.get("RESEARCH_CONTROL_KEY", "")
    if not base or not key:
        raise ManifestError("GIRAFFE_URL and RESEARCH_CONTROL_KEY are required for deterministic registration")
    body = canonical_bytes({"control_contract": contract})
    request = urllib.request.Request(base + "/api/internal/research-runs/" + run_key + "/register", data=body, method="POST", headers={"Content-Type": "application/json", "X-Research-Control-Key": key})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            if response.status < 200 or response.status >= 300: raise ManifestError("deterministic research registration failed")
            value = json.load(response)
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ValueError) as exc:
        raise ManifestError("deterministic research registration failed") from exc
    if not isinstance(value, dict) or value.get("run_key") != run_key or value.get("kind") != "research" or value.get("status") not in {"started", "done", "error"}:
        raise ManifestError("deterministic research registration response invalid")


def target_dates(now: datetime | None = None) -> list[str]:
    """Automatic runs use calendar admission; override is explicit recovery only."""
    override = os.environ.get("GIRAFFE_DART_GATE_DATES", "").strip()
    if override:
        # The automation date selector is not a capability.  A recovery caller
        # must present an HMAC bound to the exact canonical date set, using the
        # separately held recovery secret (never the cron toggle itself).
        dates = [item.strip().replace("-", "") for item in override.split(",") if item.strip()]
        canonical_dates = ",".join(dates)
        key = os.environ.get("GIRAFFE_DART_RECOVERY_KEY", "")
        supplied = os.environ.get("GIRAFFE_DART_RECOVERY_AUTHORIZATION", "")
        expected = hmac.new(key.encode("utf-8"), canonical_dates.encode("ascii"), hashlib.sha256).hexdigest() if key else ""
        if (not dates or any(len(item) != 8 or not item.isdigit() for item in dates)
                or len(dates) != len(set(dates)) or not expected
                or not hmac.compare_digest(supplied, expected)):
            raise ManifestError("GIRAFFE_DART_GATE_DATES requires authorized recovery capability")
        return dates
    current = (now or datetime.now(ZoneInfo("Asia/Seoul"))).astimezone(ZoneInfo("Asia/Seoul"))
    try:
        return admitted_backlog_dates(current.date())
    except CalendarError as exc:
        raise ManifestError(str(exc)) from exc


def record_recovery_invocation(dates: list[str]) -> None:
    """Leave an immutable local readback record before recovery side effects."""
    if not os.environ.get("GIRAFFE_DART_GATE_DATES", "").strip():
        return
    audit_root = CONTROL_ROOT / "recovery-audit"
    audit_root.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(canonical_bytes({"dates": dates})).hexdigest()
    path = audit_root / f"{digest}.json"
    payload = {"schema_version": "giraffe-recovery-audit-v1", "dates": dates, "authorization_sha256": hashlib.sha256(os.environ["GIRAFFE_DART_RECOVERY_AUTHORIZATION"].encode()).hexdigest()}
    if path.exists() and path.read_bytes() != canonical_bytes(payload) + b"\n":
        raise ManifestError("recovery audit conflict")
    if not path.exists():
        path.write_bytes(canonical_bytes(payload) + b"\n")


def invocation_args(argv: list[str]) -> tuple[str, list[str]]:
    """One-shot correction capability: explicit rerun plus exact receipts only."""
    rerun, selected = None, []
    index = 0
    while index < len(argv):
        if argv[index] == '--rerun-version' and index + 1 < len(argv) and rerun is None:
            rerun = argv[index + 1]; index += 2; continue
        if argv[index] == '--correction-rcp-no' and index + 1 < len(argv):
            selected.extend(part for part in argv[index + 1].split(',') if part); index += 2; continue
        raise ManifestError('invalid prehook invocation arguments')
    if selected and rerun is None:
        raise ManifestError('correction selection requires explicit --rerun-version')
    if selected and (len(selected) != len(set(selected)) or any(re.fullmatch(r'\d{14}', item) is None for item in selected)):
        raise ManifestError('invalid correction receipt selection')
    if rerun is None:
        return rerun_suffix(os.environ.get('GIRAFFE_RESEARCH_RERUN_VERSION', '')), []
    return rerun_suffix(rerun), sorted(selected)


def main(argv: list[str] | None = None) -> int:
    try:
        rerun, selected_corrections = invocation_args([] if argv is None else argv)
    except ManifestError as exc:
        print(json.dumps({"gate": "GIRAFFE_DART_GATE_V1", "complete": False, "error": str(exc)}, ensure_ascii=False))
        return 2
    try:
        dates = target_dates()
        record_recovery_invocation(dates)
        check_card_prompt()
        backlog = fetch_research_backlog()
        carry_forward = backlog[0] if isinstance(backlog, tuple) else backlog
        carried_sources = {item['payload']['rcp_no']: item['payload'] for item in carry_forward
                           if item.get('kind') == 'dart' and isinstance(item.get('payload'), dict)
                           and 'packet_path' in item['payload']}
        terminal_history = []
        terminal_lookups = set()
        summaries = []
        for date in dates:
            manifest = collect_manifest(date)
            receipts = sorted(record['rcp_no'] for record in manifest['material_candidate_records'])
            if isinstance(backlog, tuple) and receipts:
                history = fetch_terminal_history(receipts)
                terminal_history.extend(history)
                terminal_lookups.update(receipts)
                for item in history:
                    payload = item.get('payload', {})
                    if (item.get('identity') == 'dart:' + payload.get('rcp_no', '') and 'packet_path' in payload
                            and payload['rcp_no'] not in selected_corrections):
                        carried_sources.setdefault(payload['rcp_no'], payload)
            output = OUTPUT_ROOT / f"{date}.json"
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            packet_dir = SOURCE_ROOT / date
            source_packets = []
            source_errors = []
            for candidate in manifest["material_candidate_records"]:
                rcp_no = candidate["rcp_no"]
                try:
                    carried = carried_sources.get(rcp_no)
                    if carried:
                        checkpoint = Path(carried['packet_path'])
                        validated = validated_packet(checkpoint, rcp_no, date)
                        if validated is None or validated[1] != carried['packet_sha256']:
                            raise ManifestError("durable DART backlog packet unavailable")
                    else:
                        checkpoint = generation_checkpoint(packet_dir, rcp_no)
                        if checkpoint is None:
                            checkpoint = write_packet(fetch_with_retry(rcp_no), packet_dir)
                    source_packets.append(str(checkpoint))
                except SourceError as exc:
                    source_errors.append({"rcp_no": rcp_no, "code": exc.code if exc.code in _SOURCE_ERROR_CODES else 'SOURCE_FETCH_ERROR'})
            summaries.append({
                "date": date,
                "declared_total": manifest["declared_total"],
                "declared_pages": manifest["declared_pages"],
                "pages_collected": manifest["pages_collected"],
                "page_counts": manifest["page_counts"],
                "unique_receipts": manifest["unique_receipts"],
                "material_candidate_count": manifest["material_candidate_count"],
                "material_candidate_records": manifest["material_candidate_records"],
                "source_packet_paths": source_packets,
                "source_valid_count": len(source_packets),
                "source_error_count": len(source_errors),
                "source_errors": source_errors,
                "complete": manifest["complete"],
                "manifest_path": str(output),
            })
    except ManifestError as exc:
        if str(exc) == "KRX market closed":
            # Hermes only suppresses the LLM when this is the final non-empty
            # stdout line. Emit it before any DART or registration side effect.
            print(json.dumps({"gate": "GIRAFFE_DART_GATE_V1", "wakeAgent": False, "reason": "KRX market closed"}, ensure_ascii=False, sort_keys=True))
            return 0
        print(json.dumps({"gate": "GIRAFFE_DART_GATE_V1", "complete": False, "error": str(exc)}, ensure_ascii=False))
        return 2
    today = summaries[-1]["date"]
    run_key = research_run_key(today, rerun)
    try:
        retry_failures = {}
        current_receipts = sorted(record['rcp_no'] for summary in summaries for record in summary['material_candidate_records'])
        for item in carry_forward:
            payload = item.get('payload') if isinstance(item, dict) else None
            if item.get('kind') != 'dart' or not failed_dart_source(payload) or payload['rcp_no'] in current_receipts:
                continue
            packet_dir = SOURCE_ROOT / payload['date']
            if generation_checkpoint(packet_dir, payload['rcp_no']) is None:
                try:
                    write_packet(fetch_with_retry(payload['rcp_no']), packet_dir)
                except SourceError as exc:
                    # The carried immutable failure remains pending; this run
                    # must still report source_error for the exact receipt.
                    retry_failures[payload['rcp_no']] = exc.code if exc.code in _SOURCE_ERROR_CODES else 'SOURCE_FETCH_ERROR'
        history_receipts = sorted(set(current_receipts) | {item['payload']['rcp_no'] for item in carry_forward
                                  if isinstance(item, dict) and str(item.get('identity', '')).startswith('dart:correction:')
                                  and isinstance(item.get('payload'), dict) and isinstance(item['payload'].get('rcp_no'), str)})
        remaining_history = sorted(set(history_receipts) - terminal_lookups)
        if isinstance(backlog, tuple) and remaining_history:
            terminal_history.extend(fetch_terminal_history(remaining_history))
    except ManifestError as exc:
        print(json.dumps({"gate": "GIRAFFE_DART_GATE_V1", "complete": False, "error": str(exc)}, ensure_ascii=False))
        return 2
    try:
        contract = control_contract(run_key, summaries, carry_forward, terminal_history, selected_corrections, retry_failures)
    except ManifestError as exc:
        print(json.dumps({"gate": "GIRAFFE_DART_GATE_V1", "complete": False, "error": str(exc)}, ensure_ascii=False))
        return 2
    digest = hashlib.sha256(canonical_bytes(contract)).hexdigest()
    contract_path = CONTROL_ROOT / f"{run_key}.json"
    try:
        CONTROL_ROOT.mkdir(parents=True, exist_ok=True)
        committed_bytes = canonical_bytes(contract) + b'\n'
        try:
            with contract_path.open('xb') as stream:
                stream.write(committed_bytes)
        except FileExistsError:
            if contract_path.read_bytes() != committed_bytes:
                raise ManifestError('immutable local research control commitment conflict')
        register_research_run(run_key, contract)
    except (ManifestError, OSError) as exc:
        error = str(exc) if isinstance(exc, ManifestError) else 'research control persistence failed'
        print(json.dumps({"gate": "GIRAFFE_DART_GATE_V1", "complete": False, "error": error}, ensure_ascii=False))
        return 2
    # Keep the immutable full contract local/prehook-owned. The agent receives
    # only this compact handle and pages/open packets through the bounded CLI.
    print(json.dumps(compact_gate_payload(run_key, digest, contract["control_count"],
                                          sum("packet_path" in source for source in contract["sources"]),
                                          sum("source_error_code" in source for source in contract["sources"])),
                     ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
