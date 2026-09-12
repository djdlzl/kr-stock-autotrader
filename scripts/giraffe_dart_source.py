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


def _fetch(url: str, timeout: float = 30.0) -> tuple[bytes, str | None, str, dict[str, str]]:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        headers = {key.lower(): value for key, value in response.headers.items()}
        return response.read(), response.headers.get("Content-Type"), response.geturl(), headers


def source_packet(rcp_no: str, fetch: Callable[[str], tuple[bytes, str | None, str]] = _fetch) -> dict:
    main_url = "https://dart.fss.or.kr/dsaf001/main.do?" + urllib.parse.urlencode({"rcpNo": rcp_no})
    try:
        main_result = fetch(main_url); main_raw, main_type, main_final = main_result[:3]; main_headers = main_result[3] if len(main_result) > 3 else {}
        main_html, main_charset = strict_decode(main_raw, main_type)
        canonical = canonical_viewer_url(main_html, rcp_no)
        viewer_result = fetch(canonical); raw, content_type, final_url = viewer_result[:3]; viewer_headers = viewer_result[3] if len(viewer_result) > 3 else {}
        document, charset = strict_decode(raw, content_type)
        visible = validate_viewer(document, canonical, final_url, rcp_no)
    except SourceError: raise
    except Exception as exc: raise SourceError("SOURCE_FETCH_ERROR", str(exc)) from exc
    return {"schema_version":"giraffe-dart-source-packet-v1","rcp_no":rcp_no,"main_url":main_url,"main_final_url":main_final,"main_charset":main_charset,"main_response_headers":main_headers,"canonical_viewer_url":canonical,"final_url":final_url,"content_type":content_type,"response_headers":viewer_headers,"retrieved_at_kst":datetime.now(ZoneInfo("Asia/Seoul")).isoformat(),"charset":charset,"raw_sha256":hashlib.sha256(raw).hexdigest(),"raw_bytes":len(raw),"text_sha256":hashlib.sha256(document.encode("utf-8")).hexdigest(),"text_chars":len(document),"visible_chars":len(visible),"source_valid":True,"text":document,"_raw":raw}


def write_packet(packet: dict, directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    rcp_no = packet["rcp_no"]
    raw_bytes_path = directory / f"{rcp_no}.viewer.raw"
    raw_path = directory / f"{rcp_no}.viewer.txt"
    meta_path = directory / f"{rcp_no}.json"
    # Atomic sibling writes prevent a receipt from looking complete after a crash.
    raw_temp = raw_bytes_path.with_suffix(raw_bytes_path.suffix + ".tmp")
    raw_temp.write_bytes(packet.get("_raw", packet["text"].encode("utf-8")))
    raw_temp.replace(raw_bytes_path)
    metadata = {k:v for k,v in packet.items() if k not in {"text", "_raw"}} | {"raw_path":str(raw_bytes_path), "text_path":str(raw_path)}
    for path, content in ((raw_path, packet["text"]), (meta_path, json.dumps(metadata, ensure_ascii=False, sort_keys=True, indent=2) + "\n")):
        temp = path.with_suffix(path.suffix + ".tmp")
        temp.write_text(content, encoding="utf-8")
        temp.replace(path)
    return meta_path


def completed_packet(path: Path, rcp_no: str) -> dict | None:
    """A durable per-receipt resume checkpoint; corrupt/incomplete data is refetched."""
    try:
        if path.is_symlink(): return None
        metadata = json.loads(path.read_text(encoding="utf-8"))
        raw_path = Path(metadata["raw_path"])
        text_path = Path(metadata["text_path"])
        if raw_path.is_symlink() or text_path.is_symlink() or metadata["rcp_no"] != rcp_no or not metadata.get("source_valid"):
            return None
        raw = raw_path.read_bytes(); text = text_path.read_text(encoding="utf-8")
        if hashlib.sha256(raw).hexdigest() != metadata["raw_sha256"] or hashlib.sha256(text.encode("utf-8")).hexdigest() != metadata["text_sha256"]:
            return None
        return metadata
    except (OSError, KeyError, TypeError, ValueError):
        return None


def fetch_with_retry(rcp_no: str, retries: int = 2, fetch: Callable[[str], tuple[bytes, str | None, str]] = _fetch) -> dict:
    last: SourceError | None = None
    for attempt in range(retries):
        try: return source_packet(rcp_no, fetch)
        except SourceError as exc:
            last = exc
            if attempt + 1 < retries: time.sleep(attempt + 1)
    assert last
    raise last
