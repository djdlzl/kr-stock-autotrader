#!/usr/bin/env python3
"""Fail-closed DART viewer source packets for the 07:00 research control set."""
from __future__ import annotations

import hashlib
import html
import json
import re
import time
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

USER_AGENT = "Mozilla/5.0 (compatible; Giraffe-DART-Source/1.0)"
DART_HOST = "dart.fss.or.kr"
VIEWER_PATH = "/report/viewer.do"
VIEWDOC_RE = re.compile(r"viewDoc\(\s*['\"](?P<rcp>\d{14})['\"]\s*,\s*['\"](?P<dcm>\d+)['\"]\s*,\s*['\"](?P<ele>[^'\"]*)['\"]\s*,\s*['\"](?P<offset>[^'\"]*)['\"]\s*,\s*['\"](?P<length>[^'\"]*)['\"]\s*,\s*['\"](?P<dtd>[^'\"]+)['\"]", re.I)
META_CHARSET_RE = re.compile(r"<meta[^>]+charset\s*=\s*['\"]?\s*([\w.-]+)", re.I)
META_HTTP_EQUIV_RE = re.compile(r"<meta[^>]+content\s*=\s*['\"][^'\"]*charset\s*=\s*([\w.-]+)", re.I)
TAG_RE = re.compile(r"<[^>]+>")
SENSITIVE_HEADER_PARTS = ("authorization", "cookie", "token", "secret", "api-key")

class SourceError(RuntimeError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(f"{code}: {message}")


def _charset_from_content_type(value: str | None) -> str | None:
    if not value: return None
    match = re.search(r"charset\s*=\s*['\"]?([\w.-]+)", value, re.I)
    return match.group(1) if match else None


def _meta_charset(raw: bytes) -> str | None:
    probe = raw.decode("latin-1")
    match = META_CHARSET_RE.search(probe) or META_HTTP_EQUIV_RE.search(probe)
    return match.group(1) if match else None


def strict_decode(raw: bytes, content_type: str | None) -> tuple[str, str]:
    charset = _charset_from_content_type(content_type) or _meta_charset(raw)
    if not charset: raise SourceError("SOURCE_DECODE_ERROR", "no HTTP or HTML charset")
    try:
        text = raw.decode(charset, "strict")
    except (LookupError, UnicodeDecodeError) as exc:
        raise SourceError("SOURCE_DECODE_ERROR", f"{charset}: {exc}") from exc
    if "\ufffd" in text or "ï¿½" in text or "占쏙옙" in text:
        raise SourceError("SOURCE_DECODE_ERROR", "replacement or mojibake marker")
    return text, charset


def canonical_viewer_url(main_html: str, rcp_no: str) -> str:
    match = next((m for m in VIEWDOC_RE.finditer(main_html) if m.group("rcp") == rcp_no), None)
    if not match: raise SourceError("SOURCE_EXTRACT_ERROR", "missing dcmNo/viewDoc for rcpNo")
    query = {"rcpNo": rcp_no, "dcmNo": match.group("dcm"), "eleId": match.group("ele"), "offset": match.group("offset"), "length": match.group("length"), "dtd": match.group("dtd")}
    return "https://" + DART_HOST + VIEWER_PATH + "?" + urllib.parse.urlencode(query)


def validate_viewer(text: str, canonical_url: str, final_url: str, rcp_no: str) -> str:
    for value in (canonical_url, final_url):
        parsed = urllib.parse.urlparse(value)
        if parsed.scheme != "https" or parsed.hostname != DART_HOST or parsed.path != VIEWER_PATH:
            raise SourceError("SOURCE_FETCH_ERROR", "non-canonical viewer URL")
    canonical_query = urllib.parse.parse_qs(urllib.parse.urlparse(canonical_url).query, keep_blank_values=True)
    final_query = urllib.parse.parse_qs(urllib.parse.urlparse(final_url).query, keep_blank_values=True)
    if final_query != canonical_query or canonical_query.get("rcpNo") != [rcp_no]:
        raise SourceError("SOURCE_FETCH_ERROR", "final URL canonical identity mismatch")
    visible = " ".join(html.unescape(TAG_RE.sub(" ", text)).split())
    lowered = visible.lower()
    if any(marker in lowered for marker in ("로그인", "login", "error page", "접근이 제한", "service unavailable")):
        raise SourceError("SOURCE_EXTRACT_ERROR", "error/login shell")
    if len(visible) < 20 or not re.search(r"[가-힣A-Za-z0-9]", visible):
        raise SourceError("SOURCE_EXTRACT_ERROR", "viewer body missing")
    return visible


def _sanitize_headers(headers: dict[str, str]) -> dict[str, str]:
    return {str(key).lower(): ("[REDACTED]" if any(part in str(key).lower() for part in SENSITIVE_HEADER_PARTS) else str(value))
            for key, value in headers.items()}


def _fetch(url: str, timeout: float = 30.0) -> tuple[bytes, str | None, str, dict[str, str], int]:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        headers = {key.lower(): value for key, value in response.headers.items()}
        return response.read(), response.headers.get("Content-Type"), response.geturl(), headers, response.status


def _fetch_result(result: tuple) -> tuple[bytes, str | None, str, dict[str, str], int]:
    if len(result) < 3:
        raise SourceError("SOURCE_FETCH_ERROR", "malformed fetch result")
    raw, content_type, final_url = result[:3]
    headers = result[3] if len(result) > 3 else {}
    status = result[4] if len(result) > 4 else 200
    if (not isinstance(raw, bytes) or not isinstance(content_type, (str, type(None))) or not isinstance(final_url, str)
            or not isinstance(headers, dict) or not isinstance(status, int) or isinstance(status, bool) or not 200 <= status < 300):
        raise SourceError("SOURCE_FETCH_ERROR", "invalid fetch response")
    return raw, content_type, final_url, _sanitize_headers(headers), status


def source_packet(rcp_no: str, fetch: Callable[[str], tuple] = _fetch) -> dict:
    if not re.fullmatch(r"\d{14}", rcp_no): raise SourceError("SOURCE_FETCH_ERROR", "invalid rcpNo")
    main_url = "https://dart.fss.or.kr/dsaf001/main.do?" + urllib.parse.urlencode({"rcpNo": rcp_no})
    try:
        main_raw, main_type, main_final, main_headers, main_status = _fetch_result(fetch(main_url))
        main_html, main_charset = strict_decode(main_raw, main_type)
        canonical = canonical_viewer_url(main_html, rcp_no)
        raw, content_type, final_url, viewer_headers, viewer_status = _fetch_result(fetch(canonical))
        document, charset = strict_decode(raw, content_type)
        visible = validate_viewer(document, canonical, final_url, rcp_no)
    except SourceError: raise
    except Exception as exc: raise SourceError("SOURCE_FETCH_ERROR", str(exc)) from exc
    return {"schema_version":"giraffe-dart-source-packet-v2","rcp_no":rcp_no,"source_date":rcp_no[:8],"main_url":main_url,"main_final_url":main_final,"main_content_type":main_type,"main_charset":main_charset,"main_response_headers":main_headers,"main_response_status":main_status,"main_raw_sha256":hashlib.sha256(main_raw).hexdigest(),"main_raw_bytes":len(main_raw),"canonical_viewer_url":canonical,"final_url":final_url,"content_type":content_type,"response_headers":viewer_headers,"response_status":viewer_status,"retrieved_at_kst":datetime.now(ZoneInfo("Asia/Seoul")).isoformat(),"charset":charset,"raw_sha256":hashlib.sha256(raw).hexdigest(),"raw_bytes":len(raw),"text_sha256":hashlib.sha256(document.encode("utf-8")).hexdigest(),"text_chars":len(document),"visible_chars":len(visible),"source_valid":True,"text":document,"_raw":raw,"_main_raw":main_raw}


def write_packet(packet: dict, directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    directory = directory.resolve()
    rcp_no = packet["rcp_no"]
    raw_bytes_path = directory / f"{rcp_no}.viewer.raw"
    raw_path = directory / f"{rcp_no}.viewer.txt"
    main_raw_path = directory / f"{rcp_no}.main.raw"
    meta_path = directory / f"{rcp_no}.json"
    # Atomic sibling writes prevent a receipt from looking complete after a crash.
    for path, content in ((raw_bytes_path, packet.get("_raw", packet["text"].encode("utf-8"))), (main_raw_path, packet.get("_main_raw"))):
        if not isinstance(content, bytes): raise SourceError("SOURCE_FETCH_ERROR", "missing raw response bytes")
        temp = path.with_suffix(path.suffix + ".tmp")
        temp.write_bytes(content)
        temp.replace(path)
    metadata = {k:v for k,v in packet.items() if k not in {"text", "_raw", "_main_raw"}} | {"raw_path":str(raw_bytes_path), "text_path":str(raw_path), "main_raw_path":str(main_raw_path)}
    for path, content in ((raw_path, packet["text"]), (meta_path, json.dumps(metadata, ensure_ascii=False, sort_keys=True, indent=2) + "\n")):
        temp = path.with_suffix(path.suffix + ".tmp")
        temp.write_text(content, encoding="utf-8")
        temp.replace(path)
    return meta_path


def _stored_content_type(metadata: dict, header_name: str, fallback_name: str) -> str | None:
    headers = metadata.get(header_name)
    if not isinstance(headers, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in headers.items()):
        raise ValueError("invalid stored headers")
    header_value = headers.get("content-type")
    fallback = metadata.get(fallback_name)
    if header_value is not None and not isinstance(header_value, str): raise ValueError("invalid content type")
    if fallback is not None and not isinstance(fallback, str): raise ValueError("invalid content type")
    return header_value if header_value is not None else fallback


def _is_aware_iso_kst(value: object) -> bool:
    if not isinstance(value, str): return False
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() is not None and parsed.utcoffset().total_seconds() == 9 * 60 * 60


def completed_packet(path: Path, rcp_no: str) -> dict | None:
    """Resume only a packet re-derived from its raw DART responses, never metadata alone."""
    try:
        if not re.fullmatch(r"\d{14}", rcp_no) or path.is_symlink() or path.name != f"{rcp_no}.json" or path.parent.is_symlink() or path.parent.name != rcp_no[:8]: return None
        directory = path.parent.resolve(strict=True)
        metadata = json.loads(path.read_text(encoding="utf-8"))
        raw_path, text_path, main_raw_path = (directory / f"{rcp_no}.viewer.raw", directory / f"{rcp_no}.viewer.txt", directory / f"{rcp_no}.main.raw")
        required = {"schema_version", "rcp_no", "source_date", "main_url", "main_final_url", "main_content_type", "main_charset", "main_response_headers", "main_response_status", "main_raw_sha256", "main_raw_bytes", "canonical_viewer_url", "final_url", "content_type", "response_headers", "response_status", "retrieved_at_kst", "charset", "raw_sha256", "raw_bytes", "text_sha256", "text_chars", "visible_chars", "source_valid", "raw_path", "text_path", "main_raw_path"}
        if (not isinstance(metadata, dict) or set(metadata) != required or metadata.get("schema_version") != "giraffe-dart-source-packet-v2" or metadata.get("rcp_no") != rcp_no
                or metadata.get("source_date") != rcp_no[:8] or metadata.get("source_valid") is not True
                or metadata.get("raw_path") != str(raw_path) or metadata.get("text_path") != str(text_path) or metadata.get("main_raw_path") != str(main_raw_path)
                or metadata.get("main_url") != "https://dart.fss.or.kr/dsaf001/main.do?" + urllib.parse.urlencode({"rcpNo": rcp_no})
                or not _is_aware_iso_kst(metadata.get("retrieved_at_kst"))): return None
        paths = (raw_path, text_path, main_raw_path)
        if any(item.is_symlink() or item.resolve(strict=True).parent != directory for item in paths): return None
        raw = raw_path.read_bytes(); text = text_path.read_text(encoding="utf-8"); main_raw = main_raw_path.read_bytes()
        if (hashlib.sha256(raw).hexdigest() != metadata["raw_sha256"] or len(raw) != metadata["raw_bytes"] or hashlib.sha256(text.encode("utf-8")).hexdigest() != metadata["text_sha256"]
                or hashlib.sha256(main_raw).hexdigest() != metadata["main_raw_sha256"] or len(main_raw) != metadata["main_raw_bytes"]): return None
        main_type = _stored_content_type(metadata, "main_response_headers", "main_content_type")
        viewer_type = _stored_content_type(metadata, "response_headers", "content_type")
        main_text, main_charset = strict_decode(main_raw, main_type)
        document, charset = strict_decode(raw, viewer_type)
        canonical = canonical_viewer_url(main_text, rcp_no)
        visible = validate_viewer(document, canonical, metadata["final_url"], rcp_no)
        if (metadata.get("main_charset") != main_charset or metadata.get("charset") != charset or metadata.get("canonical_viewer_url") != canonical
                or document != text or len(document) != metadata["text_chars"] or len(visible) != metadata["visible_chars"]
                or not isinstance(metadata.get("main_response_status"), int) or isinstance(metadata["main_response_status"], bool) or not 200 <= metadata["main_response_status"] < 300
                or not isinstance(metadata.get("response_status"), int) or isinstance(metadata["response_status"], bool) or not 200 <= metadata["response_status"] < 300): return None
        return metadata
    except (OSError, KeyError, TypeError, ValueError, SourceError, json.JSONDecodeError):
        return None


def fetch_with_retry(rcp_no: str, retries: int = 2, fetch: Callable[[str], tuple] = _fetch) -> dict:
    last: SourceError | None = None
    for attempt in range(retries):
        try: return source_packet(rcp_no, fetch)
        except SourceError as exc:
            last = exc
            if attempt + 1 < retries: time.sleep(attempt + 1)
    assert last
    raise last
