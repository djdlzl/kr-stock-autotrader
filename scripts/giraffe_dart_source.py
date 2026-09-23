#!/usr/bin/env python3
"""Fail-closed DART viewer source packets for the 07:00 research control set."""
from __future__ import annotations

import hashlib
import html
import json
import os
import shutil
import stat
import tempfile
import uuid
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

try:
    from scripts.giraffe_direct_fetch import DEFAULT_PACER, FetchError, fetch_https_bytes
except ModuleNotFoundError:  # Direct script execution from the scripts directory.
    from giraffe_direct_fetch import DEFAULT_PACER, FetchError, fetch_https_bytes

USER_AGENT = "Mozilla/5.0 (compatible; Giraffe-DART-Source/1.0)"
DART_HOST = "dart.fss.or.kr"
VIEWER_PATH = "/report/viewer.do"
TREE_AUTHORITY_RE = re.compile(r"\btreeData\b|\.jstree\s*\(|(?:\.\s*rcpNo|\[\s*['\"]rcpNo['\"]\s*\])\s*=", re.I)
TREE_EVENT_RE = re.compile(
    r"(?:var|let|const)\s+(?P<declare>[A-Za-z_$][\w$]*)\s*=\s*\{\s*\}\s*;"
    r"|(?P<variable>[A-Za-z_$][\w$]*)(?:\[\s*['\"](?P<bracket>\w+)['\"]\s*\]|\.(?P<dot>\w+))\s*=\s*(?P<value>[^;]*);"
    r"|treeData\s*\.\s*push\s*\(\s*(?P<push>[A-Za-z_$][\w$]*)\s*\)\s*;", re.S)
VIEWDOC_RE = re.compile(r"viewDoc\(\s*['\"](?P<rcp>\d{14})['\"]\s*,\s*['\"](?P<dcm>\d+)['\"]\s*,\s*['\"](?P<ele>[^'\"]*)['\"]\s*,\s*['\"](?P<offset>[^'\"]*)['\"]\s*,\s*['\"](?P<length>[^'\"]*)['\"]\s*,\s*['\"](?P<dtd>[^'\"]+)['\"]", re.I)
META_CHARSET_RE = re.compile(r"<meta[^>]+charset\s*=\s*['\"]?\s*([\w.-]+)", re.I)
META_HTTP_EQUIV_RE = re.compile(r"<meta[^>]+content\s*=\s*['\"][^'\"]*charset\s*=\s*([\w.-]+)", re.I)
TAG_RE = re.compile(r"<[^>]+>")
KST = ZoneInfo("Asia/Seoul")
MAX_CAPTURE_AGE = timedelta(hours=72)
MAX_FUTURE_CAPTURE_SKEW = timedelta(minutes=5)
ALLOWED_RESPONSE_HEADER_NAMES = frozenset({"content-type", "content-length"})

class SourceError(RuntimeError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(f"{code}: {message}")


class _TransientFetchError(SourceError):
    """A retryable transport failure, distinct from invalid source data."""


class _RequestPacer:
    def __init__(self, clock: Callable[[], float], sleep: Callable[[float], None]):
        self.clock = clock
        self.sleep = sleep
        self.next_start = float("-inf")

    def defer(self, seconds: float) -> None:
        self.next_start = max(self.next_start, self.clock() + seconds)

    def mark_start(self) -> float:
        started = self.clock()
        self.next_start = started + 1.0
        return started

    def request(self, fetch: Callable[[str], object], url: str) -> object:
        while (remaining := self.next_start - self.clock()) > 0:
            self.sleep(remaining)
        self.next_start = self.clock() + 1.0
        try:
            return fetch(url)
        except Exception as exc:
            # HTTP status errors, TLS/configuration errors and source validation
            # errors are not transient connection failures.
            reason = exc.reason if isinstance(exc, urllib.error.URLError) else exc
            if not isinstance(exc, urllib.error.HTTPError) and isinstance(reason, (TimeoutError, ConnectionError)):
                raise _TransientFetchError("SOURCE_FETCH_ERROR", str(exc)) from exc
            raise


class _NoopPacer:
    """Test-double transport has no network starts to serialize."""
    def defer(self, _seconds: float) -> None:
        pass

    def request(self, fetch: Callable[[str], object], url: str) -> object:
        return fetch(url)


_DEFAULT_PACER = DEFAULT_PACER


def _request_pacer(clock: Callable[[], float], sleep: Callable[[float], None], *, fetch: object,
                   pacer: Any | None = None) -> Any:
    # Only the real transport creates HTTP starts. Synthetic test transports
    # must opt in with an injected deterministic pacer, never sleep wall time.
    if pacer is not None:
        return pacer
    if fetch is _fetch and clock is time.monotonic and sleep is time.sleep:
        return _DEFAULT_PACER
    if clock is time.monotonic and sleep is time.sleep:
        return _NoopPacer()
    return _RequestPacer(clock, sleep)


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


def declared_viewer_sections(main_html: str, rcp_no: str) -> list[dict[str, str]]:
    """Use the declared jsTree order, not eleId arithmetic or a 404 sentinel."""
    tree_nodes = []
    if TREE_AUTHORITY_RE.search(main_html):
        for initializer in re.finditer(r"\btreeData\s*=\s*([^;]*);", main_html):
            if not re.fullmatch(r"\s*(?:\[\s*\]|new\s+Array\s*\(\s*\))\s*", initializer.group(1)):
                raise SourceError("SOURCE_EXTRACT_ERROR", "unparseable authoritative tree initializer")
        variables = {}
        pending_nodes = set()
        required = ("rcpNo", "dcmNo", "eleId", "offset", "length", "dtd", "tocNo", "atocId")
        pushes = 0
        for event in TREE_EVENT_RE.finditer(main_html):
            if event.group("declare"):
                if event.group("declare") in pending_nodes:
                    raise SourceError("SOURCE_EXTRACT_ERROR", "overwritten unpushed tree section")
                variables[event.group("declare")] = {}
            elif event.group("push"):
                pushes += 1
                fields = variables.get(event.group("push"), {})
                if any(not fields.get(key) for key in required) or fields["rcpNo"] != rcp_no:
                    raise SourceError("SOURCE_EXTRACT_ERROR", "malformed declared tree section")
                tree_nodes.append(dict(fields))
                pending_nodes.discard(event.group("push"))
            elif event.group("variable") in variables:
                key = event.group("bracket") or event.group("dot")
                value = event.group("value").strip()
                if key in required:
                    if len(value) < 2 or value[0] not in "\"'" or value[-1] != value[0] or "\\" in value[1:-1] or value[0] in value[1:-1]:
                        raise SourceError("SOURCE_EXTRACT_ERROR", "unparseable declared tree field")
                    variables[event.group("variable")][key] = html.unescape(value[1:-1])
                    pending_nodes.add(event.group("variable"))
        if pending_nodes or not tree_nodes or pushes != len(re.findall(r"treeData\s*\.\s*push\s*\(", main_html)):
            raise SourceError("SOURCE_EXTRACT_ERROR", "unparseable authoritative tree")
        candidates = tree_nodes
    else:
        candidates = [{"rcpNo": m.group("rcp"), "dcmNo": m.group("dcm"), "eleId": m.group("ele"), "offset": m.group("offset"), "length": m.group("length"), "dtd": m.group("dtd"), "tocNo": "", "atocId": ""} for m in VIEWDOC_RE.finditer(main_html) if m.group("rcp") == rcp_no]
    if not candidates:
        raise SourceError("SOURCE_EXTRACT_ERROR", "missing declared viewer sections for rcpNo")
    sections: list[dict[str, str]] = []; seen: set[tuple[str, str, str, str, str]] = set()
    for fields in candidates:
        values = {"dcm": fields["dcmNo"], "ele": fields["eleId"], "offset": fields["offset"], "length": fields["length"], "dtd": fields["dtd"]}
        if (not values["dcm"].isdigit() or not values["ele"].isdigit() or not values["offset"].isdigit() or not values["length"].isdigit() or not re.fullmatch(r"[A-Za-z0-9._-]+", values["dtd"])):
            raise SourceError("SOURCE_EXTRACT_ERROR", "malformed declared viewer section")
        identity = tuple(values[key] for key in ("dcm", "ele", "offset", "length", "dtd"))
        if identity in seen: raise SourceError("SOURCE_EXTRACT_ERROR", "duplicate declared viewer section")
        seen.add(identity); query = {"rcpNo": rcp_no, "dcmNo": values["dcm"], "eleId": values["ele"], "offset": values["offset"], "length": values["length"], "dtd": values["dtd"]}
        sections.append({"canonical_viewer_url": "https://" + DART_HOST + VIEWER_PATH + "?" + urllib.parse.urlencode(query), "dcm_no": values["dcm"], "ele_id": values["ele"], "offset": values["offset"], "length": values["length"], "dtd": values["dtd"], "toc_no": fields["tocNo"], "atoc_id": fields["atocId"]})
    return sections


def canonical_viewer_url(main_html: str, rcp_no: str) -> str:
    """Legacy first-section selector; v3 captures declared_viewer_sections()."""
    return declared_viewer_sections(main_html, rcp_no)[0]["canonical_viewer_url"]


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


def _provenance_headers(headers: dict[str, str], content_type: str | None, raw_length: int) -> dict[str, str]:
    """Persist only receipt fields that can be checked against the stored body."""
    projection: dict[str, str] = {}
    if content_type is not None:
        projection["content-type"] = content_type
    content_length = next((str(value) for key, value in headers.items() if str(key).lower() == "content-length"), None)
    if content_length is not None:
        if not content_length.isascii() or not content_length.isdecimal() or int(content_length) != raw_length:
            raise SourceError("SOURCE_FETCH_ERROR", "invalid content-length receipt")
        projection["content-length"] = content_length
    return projection


def _valid_provenance_headers(headers: object, content_type: object, raw_length: int) -> bool:
    if not isinstance(headers, dict) or any(not isinstance(key, str) or not isinstance(value, str) for key, value in headers.items()):
        return False
    if set(headers) - ALLOWED_RESPONSE_HEADER_NAMES:
        return False
    if content_type is not None and (not isinstance(content_type, str) or headers.get("content-type") != content_type):
        return False
    if content_type is None and "content-type" in headers:
        return False
    content_length = headers.get("content-length")
    return content_length is None or (content_length.isascii() and content_length.isdecimal() and int(content_length) == raw_length)


def _fetch(url: str, timeout: float = 30.0, *, pacer=None) -> tuple[bytes, str | None, str, dict[str, str], int]:
    try:
        return fetch_https_bytes(url, pacer=pacer or _DEFAULT_PACER, attempts=1, timeout=timeout)
    except FetchError as exc:
        error_type = _TransientFetchError if exc.retryable else SourceError
        raise error_type("SOURCE_FETCH_ERROR", exc.failure_class) from exc


def _paced_fetch(pacer, fetch, url):
    if fetch is _fetch:
        # The transport paces each redirect hop; do not double-pace the wrapper.
        return _fetch(url, pacer=pacer)
    try:
        return pacer.request(fetch, url)
    except Exception as exc:
        reason = exc.reason if isinstance(exc, urllib.error.URLError) else exc
        if not isinstance(exc, urllib.error.HTTPError) and isinstance(reason, (TimeoutError, ConnectionError)):
            raise _TransientFetchError("SOURCE_FETCH_ERROR", "transient transport failure") from exc
        raise


def _fetch_result(result: tuple) -> tuple[bytes, str | None, str, dict[str, str], int]:
    if len(result) < 3:
        raise SourceError("SOURCE_FETCH_ERROR", "malformed fetch result")
    raw, content_type, final_url = result[:3]
    headers = result[3] if len(result) > 3 else {}
    status = result[4] if len(result) > 4 else 200
    if (not isinstance(raw, bytes) or not isinstance(content_type, (str, type(None))) or not isinstance(final_url, str)
            or not isinstance(headers, dict) or not isinstance(status, int) or isinstance(status, bool) or status != 200):
        raise SourceError("SOURCE_FETCH_ERROR", "invalid fetch response")
    return raw, content_type, final_url, headers, status


def source_packet(rcp_no: str, fetch: Callable[[str], tuple] = _fetch, *,
                  clock: Callable[[], float] = time.monotonic,
                  sleep: Callable[[float], None] = time.sleep, pacer: Any | None = None) -> dict:
    request_pacer = _request_pacer(clock, sleep, fetch=fetch, pacer=pacer)
    return _source_packet(rcp_no, lambda url: _paced_fetch(request_pacer, fetch, url))


def _source_packet(rcp_no: str, fetch: Callable[[str], tuple]) -> dict:
    if not re.fullmatch(r"\d{14}", rcp_no): raise SourceError("SOURCE_FETCH_ERROR", "invalid rcpNo")
    main_url = "https://dart.fss.or.kr/dsaf001/main.do?" + urllib.parse.urlencode({"rcpNo": rcp_no})
    try:
        main_raw, main_type, main_final, main_headers, main_status = _fetch_result(fetch(main_url))
        if main_final != main_url:
            raise SourceError("SOURCE_FETCH_ERROR", "main URL canonical identity mismatch")
        main_html, main_charset = strict_decode(main_raw, main_type)
        declared = declared_viewer_sections(main_html, rcp_no)
        captured, documents = [], []
        for position, declaration in enumerate(declared):
            canonical = declaration["canonical_viewer_url"]
            raw, content_type, final_url, viewer_headers, viewer_status = _fetch_result(fetch(canonical))
            document, charset = strict_decode(raw, content_type)
            visible = validate_viewer(document, canonical, final_url, rcp_no)
            captured.append(declaration | {"position": position, "final_url": final_url, "content_type": content_type,
                "response_headers": _provenance_headers(viewer_headers, content_type, len(raw)), "response_status": viewer_status,
                "retrieved_at_kst": datetime.now(KST).isoformat(), "charset": charset,
                "raw_sha256": hashlib.sha256(raw).hexdigest(), "raw_bytes": len(raw),
                "text_sha256": hashlib.sha256(document.encode("utf-8")).hexdigest(), "text_chars": len(document),
                "visible_chars": len(visible), "_raw": raw, "_text": document})
            documents.append(document)
        text = "\n\n".join(documents)
        visible = " ".join(validate_viewer(document, section["canonical_viewer_url"], section["final_url"], rcp_no)
                           for document, section in zip(documents, captured))
    except SourceError: raise
    except Exception as exc: raise SourceError("SOURCE_FETCH_ERROR", str(exc)) from exc
    return {"schema_version":"giraffe-dart-source-packet-v3","rcp_no":rcp_no,"source_date":rcp_no[:8],
        "main_url":main_url,"main_final_url":main_final,"main_content_type":main_type,"main_charset":main_charset,
        "main_response_headers":_provenance_headers(main_headers, main_type, len(main_raw)),"main_response_status":main_status,
        "main_raw_sha256":hashlib.sha256(main_raw).hexdigest(),"main_raw_bytes":len(main_raw),
        "sections":[{k:v for k,v in section.items() if not k.startswith("_")} for section in captured],
        "retrieved_at_kst":datetime.now(KST).isoformat(),"text_sha256":hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "text_chars":len(text),"visible_chars":len(visible),"source_valid":True,"text":text,"_main_raw":main_raw,
        "_sections":captured} | ({key: captured[0][key] for key in ("canonical_viewer_url", "final_url", "content_type", "response_headers", "response_status", "charset", "raw_sha256", "raw_bytes")} if len(captured) == 1 else {})


def write_packet(packet: dict, directory: Path) -> Path:
    """Publish v3 packet only after every section has been captured in staging."""
    missing_directories = []
    ancestor = directory
    while not ancestor.exists():
        missing_directories.append(ancestor)
        ancestor = ancestor.parent
    directory.mkdir(parents=True, exist_ok=True)
    for created in reversed(missing_directories):
        _fsync_directory(created.parent)
    if directory.is_symlink() or packet.get("schema_version") != "giraffe-dart-source-packet-v3":
        raise SourceError("SOURCE_FETCH_ERROR", "invalid v3 packet publication")
    directory = directory.resolve()
    if not re.fullmatch(r"\d{8}", directory.name):
        raise SourceError("SOURCE_FETCH_ERROR", "invalid control date directory")
    control_directory = directory
    directory = control_directory / ("v3-" + uuid.uuid4().hex)
    if directory.exists():
        raise FileExistsError(str(directory))
    rcp_no = packet["rcp_no"]
    if not re.fullmatch(r"\d{14}", rcp_no):
        raise SourceError("SOURCE_FETCH_ERROR", "invalid rcpNo")
    main_raw_path = directory / f"{rcp_no}.main.raw"
    text_path = directory / f"{rcp_no}.viewer.txt"
    meta_path = directory / f"{rcp_no}.json"
    sections = packet.get("_sections")
    if not isinstance(sections, list) or len(sections) != len(packet.get("sections", [])):
        raise SourceError("SOURCE_FETCH_ERROR", "incomplete section capture")
    staged: list[tuple[Path, bytes]] = [(main_raw_path, packet.get("_main_raw")), (text_path, packet["text"].encode("utf-8"))]
    public_sections = []
    for position, section in enumerate(sections):
        raw_path = directory / f"{rcp_no}.viewer.{position:04d}.raw"
        section_text_path = directory / f"{rcp_no}.viewer.{position:04d}.txt"
        if not isinstance(section.get("_raw"), bytes) or not isinstance(section.get("_text"), str):
            raise SourceError("SOURCE_FETCH_ERROR", "incomplete section capture")
        staged.extend(((raw_path, section["_raw"]), (section_text_path, section["_text"].encode("utf-8"))))
        public_sections.append({k:v for k,v in section.items() if not k.startswith("_")} | {"raw_path": str(raw_path), "text_path": str(section_text_path)})
    metadata = {k:v for k,v in packet.items() if k not in {"text", "_main_raw", "_sections", "sections"}} | {"sections": public_sections, "text_path":str(text_path), "main_raw_path":str(main_raw_path)}
    staged.append((meta_path, (json.dumps(metadata, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")))
    staging = Path(tempfile.mkdtemp(prefix=".staging-", dir=control_directory))
    try:
        for path, content in staged:
            if not isinstance(content, bytes):
                raise SourceError("SOURCE_FETCH_ERROR", "missing raw response bytes")
            with (staging / path.name).open("xb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
        _fsync_directory(staging)
        # Published generations are nonempty, so rename cannot replace one even
        # when another publisher wins the same generation id concurrently.
        if directory.exists():
            raise FileExistsError(str(directory))
        os.rename(staging, directory)
        _fsync_directory(control_directory)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    if completed_packet(meta_path, rcp_no, expected_control_date=control_directory.name) is None:
        raise SourceError("SOURCE_FETCH_ERROR", "published generation failed readback")
    return meta_path


def _fsync_directory(directory: Path) -> None:
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def packet_control_directory(path: Path) -> Path:
    parent = path.parent
    return parent.parent if re.fullmatch(r"v3-[0-9a-f]{32}", parent.name) else parent


class RetainedPacketSnapshot:
    """Pin directory descriptors and retain each regular file exactly once."""
    def __init__(self, path: Path, *, trusted_root: Path | None = None):
        self.path = Path(os.path.abspath(path))
        self.root = Path(trusted_root).resolve(strict=True) if trusted_root is not None else Path(self.path.anchor)
        self._fds = []
        self._files = {}
        self._directories = []

    @staticmethod
    def _identity(info):
        return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)

    def __enter__(self):
        try:
            relative = self.path.relative_to(self.root)
            fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            self._fds.append(fd)
            for part in relative.parts[:-1]:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                self._fds.append(child)
                self._directories.append((fd, part, child, self._identity(os.fstat(child))))
                fd = child
            self._directory_fd = fd
            self.packet_bytes = self.read_sibling(self.path)
            return self
        except Exception:
            self.__exit__(None, None, None)
            raise

    def read_sibling(self, path: Path) -> bytes:
        path = Path(path)
        if path.parent != self.path.parent or path.name in {".", ".."}:
            raise ValueError("packet sibling outside retained generation")
        if path.name in self._files:
            return self._files[path.name][2]
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=self._directory_fd)
        self._fds.append(fd)
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            raise ValueError("packet sibling is not regular")
        chunks = []
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk: break
            chunks.append(chunk)
        data = b"".join(chunks)
        identity = self._identity(before)
        if identity != self._identity(os.fstat(fd)):
            raise ValueError("packet sibling drift")
        self._files[path.name] = (fd, identity, data)
        return data

    def verify(self):
        for parent, name, fd, identity in self._directories:
            if identity != self._identity(os.fstat(fd)) or identity != self._identity(os.stat(name, dir_fd=parent, follow_symlinks=False)):
                raise ValueError("packet generation drift")
        for name, (fd, identity, _) in self._files.items():
            if identity != self._identity(os.fstat(fd)) or identity != self._identity(os.stat(name, dir_fd=self._directory_fd, follow_symlinks=False)):
                raise ValueError("packet sibling drift")

    def __exit__(self, *_):
        for fd in reversed(self._fds): os.close(fd)
        self._fds.clear()


def _capture_time_is_fresh(value: object, now: datetime) -> bool:
    if not isinstance(value, str): return False
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return False
    offset = parsed.utcoffset()
    if parsed.tzinfo is None or offset is None or offset.total_seconds() != 9 * 60 * 60:
        return False
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    age = now.astimezone(KST) - parsed.astimezone(KST)
    return -MAX_FUTURE_CAPTURE_SKEW <= age <= MAX_CAPTURE_AGE


def completed_packet_snapshot(path: Path, rcp_no: str, packet_bytes: bytes, read_sibling: Callable[[Path], bytes], *, expected_control_date: str | None = None, now: datetime | None = None) -> dict | None:
    """Resume only a fully revalidated v3 packet; every declared section is bound."""
    try:
        control = packet_control_directory(path)
        if (not re.fullmatch(r"\d{14}", rcp_no) or path.name != f"{rcp_no}.json"
                or not re.fullmatch(r"\d{8}", control.name)
                or (expected_control_date is not None and control.name != expected_control_date)): return None
        directory = path.parent
        metadata = json.loads(packet_bytes)
        if isinstance(metadata, dict) and metadata.get("schema_version") == "giraffe-dart-source-packet-v2":
            return _completed_v2(path, rcp_no, packet_bytes, read_sibling, expected_control_date=expected_control_date, now=now)
        if not re.fullmatch(r"v3-[0-9a-f]{32}", directory.name): return None
        required = {"schema_version", "rcp_no", "source_date", "main_url", "main_final_url", "main_content_type", "main_charset", "main_response_headers", "main_response_status", "main_raw_sha256", "main_raw_bytes", "sections", "retrieved_at_kst", "text_sha256", "text_chars", "visible_chars", "source_valid", "text_path", "main_raw_path"}
        legacy = {"canonical_viewer_url", "final_url", "content_type", "response_headers", "response_status", "charset", "raw_sha256", "raw_bytes"}
        if (not isinstance(metadata, dict) or set(metadata) != required and set(metadata) != required | legacy
                or metadata.get("schema_version") != "giraffe-dart-source-packet-v3" or metadata.get("rcp_no") != rcp_no
                or metadata.get("source_date") != rcp_no[:8] or metadata.get("source_valid") is not True
                or metadata.get("main_url") != "https://dart.fss.or.kr/dsaf001/main.do?" + urllib.parse.urlencode({"rcpNo": rcp_no})
                or metadata.get("main_final_url") != metadata.get("main_url") or not _capture_time_is_fresh(metadata.get("retrieved_at_kst"), now or datetime.now(KST))): return None
        main_raw_path, text_path = directory / f"{rcp_no}.main.raw", directory / f"{rcp_no}.viewer.txt"
        if metadata.get("main_raw_path") != str(main_raw_path) or metadata.get("text_path") != str(text_path): return None
        main_raw, text_bytes = read_sibling(main_raw_path), read_sibling(text_path)
        if hashlib.sha256(main_raw).hexdigest() != metadata["main_raw_sha256"] or len(main_raw) != metadata["main_raw_bytes"] or hashlib.sha256(text_bytes).hexdigest() != metadata["text_sha256"]: return None
        main_text, main_charset = strict_decode(main_raw, metadata.get("main_content_type"))
        declared = declared_viewer_sections(main_text, rcp_no); sections = metadata.get("sections")
        if not isinstance(sections, list) or len(sections) != len(declared) or not sections: return None
        documents, visibles = [], []
        for position, (decl, section) in enumerate(zip(declared, sections)):
            if not isinstance(section, dict): return None
            raw_path, section_text_path = directory / f"{rcp_no}.viewer.{position:04d}.raw", directory / f"{rcp_no}.viewer.{position:04d}.txt"
            required_section = set(decl) | {"position", "final_url", "content_type", "response_headers", "response_status", "retrieved_at_kst", "charset", "raw_sha256", "raw_bytes", "text_sha256", "text_chars", "visible_chars", "raw_path", "text_path"}
            if (set(section) != required_section or any(section.get(key) != value for key, value in decl.items()) or section.get("position") != position
                    or section.get("raw_path") != str(raw_path) or section.get("text_path") != str(section_text_path)
                    ): return None
            raw, section_text_bytes = read_sibling(raw_path), read_sibling(section_text_path); document = section_text_bytes.decode("utf-8")
            if (hashlib.sha256(raw).hexdigest() != section["raw_sha256"] or len(raw) != section["raw_bytes"] or hashlib.sha256(section_text_bytes).hexdigest() != section["text_sha256"]): return None
            decoded, charset = strict_decode(raw, section.get("content_type"))
            if decoded != document: return None
            visible = validate_viewer(document, decl["canonical_viewer_url"], section.get("final_url"), rcp_no)
            if (charset != section["charset"] or len(document) != section["text_chars"] or len(visible) != section["visible_chars"] or section.get("response_status") != 200 or not _valid_provenance_headers(section.get("response_headers"), section.get("content_type"), len(raw))): return None
            documents.append(document); visibles.append(visible)
        text = text_bytes.decode("utf-8"); combined = "\n\n".join(documents)
        if legacy <= set(metadata) and any(metadata[key] != sections[0][key] for key in legacy): return None
        if text != combined or len(text) != metadata["text_chars"] or len(" ".join(visibles)) != metadata["visible_chars"] or main_charset != metadata["main_charset"] or metadata.get("main_response_status") != 200 or not _valid_provenance_headers(metadata.get("main_response_headers"), metadata.get("main_content_type"), len(main_raw)): return None
        return metadata
    except (OSError, KeyError, TypeError, ValueError, UnicodeDecodeError, SourceError, json.JSONDecodeError): return None


def _completed_v2(path: Path, rcp_no: str, packet_bytes: bytes, read_sibling: Callable[[Path], bytes], *, expected_control_date: str | None = None, now: datetime | None = None) -> dict | None:
    """Resume only a raw-revalidated packet in its declared control-date directory."""
    try:
        if not re.fullmatch(r"\d{8}", path.parent.name): return None
        directory = path.parent
        metadata = json.loads(packet_bytes)
        raw_path, text_path, main_raw_path = (directory / f"{rcp_no}.viewer.raw", directory / f"{rcp_no}.viewer.txt", directory / f"{rcp_no}.main.raw")
        required = {"schema_version", "rcp_no", "source_date", "main_url", "main_final_url", "main_content_type", "main_charset", "main_response_headers", "main_response_status", "main_raw_sha256", "main_raw_bytes", "canonical_viewer_url", "final_url", "content_type", "response_headers", "response_status", "retrieved_at_kst", "charset", "raw_sha256", "raw_bytes", "text_sha256", "text_chars", "visible_chars", "source_valid", "raw_path", "text_path", "main_raw_path"}
        if (not isinstance(metadata, dict) or set(metadata) != required or metadata.get("schema_version") != "giraffe-dart-source-packet-v2" or metadata.get("rcp_no") != rcp_no
                or metadata.get("source_date") != rcp_no[:8] or metadata.get("source_valid") is not True
                or metadata.get("raw_path") != str(raw_path) or metadata.get("text_path") != str(text_path) or metadata.get("main_raw_path") != str(main_raw_path)
                or metadata.get("main_url") != "https://dart.fss.or.kr/dsaf001/main.do?" + urllib.parse.urlencode({"rcpNo": rcp_no})
                or metadata.get("main_final_url") != metadata.get("main_url")
                or not _capture_time_is_fresh(metadata.get("retrieved_at_kst"), now or datetime.now(KST))): return None
        # Preserve the exact decoded representation. Path.read_text() enables
        # universal-newline translation, which changes authoritative CRLF
        # source text and makes every valid Windows-newline checkpoint miss.
        raw = read_sibling(raw_path); text = read_sibling(text_path).decode("utf-8"); main_raw = read_sibling(main_raw_path)
        if (hashlib.sha256(raw).hexdigest() != metadata["raw_sha256"] or len(raw) != metadata["raw_bytes"] or hashlib.sha256(text.encode("utf-8")).hexdigest() != metadata["text_sha256"]
                or hashlib.sha256(main_raw).hexdigest() != metadata["main_raw_sha256"] or len(main_raw) != metadata["main_raw_bytes"]): return None
        main_type = metadata.get("main_content_type")
        viewer_type = metadata.get("content_type")
        if (not isinstance(main_type, (str, type(None))) or not isinstance(viewer_type, (str, type(None)))
                or not _valid_provenance_headers(metadata.get("main_response_headers"), main_type, len(main_raw))
                or not _valid_provenance_headers(metadata.get("response_headers"), viewer_type, len(raw))): return None
        main_text, main_charset = strict_decode(main_raw, main_type)
        document, charset = strict_decode(raw, viewer_type)
        match = next((m for m in VIEWDOC_RE.finditer(main_text) if m.group("rcp") == rcp_no), None)
        if match is None: return None
        canonical = "https://" + DART_HOST + VIEWER_PATH + "?" + urllib.parse.urlencode({"rcpNo": rcp_no, "dcmNo": match.group("dcm"), "eleId": match.group("ele"), "offset": match.group("offset"), "length": match.group("length"), "dtd": match.group("dtd")})
        visible = validate_viewer(document, canonical, metadata["final_url"], rcp_no)
        if (metadata.get("main_charset") != main_charset or metadata.get("charset") != charset or metadata.get("canonical_viewer_url") != canonical
                or document != text or len(document) != metadata["text_chars"] or len(visible) != metadata["visible_chars"]
                or metadata.get("main_response_status") != 200 or metadata.get("response_status") != 200): return None
        return metadata
    except (OSError, KeyError, TypeError, ValueError, SourceError, json.JSONDecodeError):
        return None


def completed_packet(path: Path, rcp_no: str, *, expected_control_date: str | None = None, now: datetime | None = None, trusted_root: Path | None = None) -> dict | None:
    try:
        with RetainedPacketSnapshot(path, trusted_root=trusted_root) as snapshot:
            metadata = completed_packet_snapshot(snapshot.path, rcp_no, snapshot.packet_bytes, snapshot.read_sibling, expected_control_date=expected_control_date, now=now)
            snapshot.verify()
            return metadata
    except (OSError, ValueError):
        return None


def fetch_with_retry(rcp_no: str, retries: int = 4, fetch: Callable[[str], tuple] = _fetch, *,
                     clock: Callable[[], float] = time.monotonic,
                     sleep: Callable[[float], None] = time.sleep, pacer: Any | None = None) -> dict:
    if retries < 1:
        raise ValueError("retries must be at least 1")
    attempts = min(retries, 4)
    request_pacer = _request_pacer(clock, sleep, fetch=fetch, pacer=pacer)
    for attempt in range(attempts):
        try:
            return _source_packet(rcp_no, lambda url: _paced_fetch(request_pacer, fetch, url))
        except _TransientFetchError:
            if attempt + 1 == attempts:
                raise
            request_pacer.defer(2 ** (attempt + 1))
    raise AssertionError('retry loop exhausted unexpectedly')
