#!/usr/bin/env python3
"""Bounded public HTTPS discovery with one process-shared DART request pacer."""
from __future__ import annotations

import argparse
import fcntl
import http.client
import ipaddress
import json
import math
import os
import re
import socket
import ssl
import threading
import time
import urllib.error
import urllib.parse
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path

INTERVAL = 0.75
MAX_BODY_BYTES = 2 * 1024 * 1024
MAX_REDIRECTS = 5
MAX_ATTEMPTS = 3
TIMEOUT = 15.0


class FetchError(RuntimeError):
    def __init__(self, failure_class, *, retryable=False):
        self.failure_class = failure_class
        self.retryable = retryable
        super().__init__(failure_class)


class LocalPacer:
    """Injectable timeline; legacy DART tests retain their one-second cadence."""
    def __init__(self, clock=time.monotonic, sleep=time.sleep):
        self.clock, self.sleep = clock, sleep
        self.next_start = float("-inf")

    def defer(self, seconds):
        self.next_start = max(self.next_start, self.clock() + seconds)

    def request(self, fetch, url):
        while (remaining := self.next_start - self.clock()) > 0:
            self.sleep(remaining)
        self.next_start = self.clock() + 1.0
        return fetch(url)

    def mark_start(self):
        started = self.clock()
        self.next_start = started + 1.0
        return started


class SharedPacer:
    """Serialize starts across interpreters, holding the lock through transport.

    The state contains no URLs or credentials. All production entry points use
    the same per-user default path, including different worktrees. An explicit
    override must be identical in every cooperating process.
    """
    clock = staticmethod(time.monotonic)

    def __init__(self):
        self.next_start = float("-inf")
        self._active = threading.local()

    def defer(self, seconds):
        self.next_start = max(self.next_start, self.clock() + seconds)

    def request(self, fetch, url):
        path = Path(os.environ.get("GIRAFFE_HTTP_PACER_STATE", str(Path.home() / ".hermes" / "runs" / "giraffe-http-pacer.json")))
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "r+", encoding="ascii") as state:
            fcntl.flock(state, fcntl.LOCK_EX)
            raw = state.read(256)
            now = self.clock()
            try:
                last = float(raw) if raw else float("-inf")
                if raw and not math.isfinite(last):
                    raise ValueError("invalid pacing state")
            except ValueError as exc:
                raise FetchError("extractor_failure") from exc
            # A monotonic timestamp from before reboot must not stall forever.
            due = max(self.next_start, min(last, now) + INTERVAL)
            while (remaining := due - self.clock()) > 0:
                time.sleep(remaining)
            self._active.state = state
            self.mark_start()
            # Keep the lock until the call has actually started and completed:
            # another process cannot reserve a slot and overtake a delayed start.
            try:
                return fetch(url)
            finally:
                del self._active.state

    def mark_start(self):
        """Refresh the reservation immediately before the actual HTTP write."""
        started = self.clock()
        state = self._active.state
        state.seek(0)
        state.write(str(started))
        state.truncate()
        state.flush()
        return started


DEFAULT_PACER = SharedPacer()


def _public_ip(value):
    address = ipaddress.ip_address(value)
    return (address.is_global and not address.is_multicast and not address.is_reserved
            and not address.is_loopback and not address.is_link_local and not address.is_unspecified)


def validate_target(url):
    if (not isinstance(url, str) or len(url) > 8192
            or re.search(r"[\x00-\x20\x7f\\]", url)
            or re.search(r"%(?:0[0-9a-f]|1[0-9a-f]|7f)", url, re.I)):
        raise FetchError("unsupported_or_js")
    try:
        parsed = urllib.parse.urlsplit(url)
        host = parsed.hostname
        if (parsed.scheme != "https" or not host or parsed.username is not None
                or parsed.password is not None or "#" in url or parsed.port not in (None, 443)):
            raise ValueError("unsafe target")
        host = host.encode("idna").decode("ascii").rstrip(".").lower()
        if host == "localhost" or host.endswith((".localhost", ".local")):
            raise ValueError("local target")
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            if not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", host):
                raise ValueError("invalid hostname")
        else:
            if not _public_ip(address):
                raise ValueError("non-public target")
        return parsed, host
    except (ValueError, UnicodeError) as exc:
        raise FetchError("unsupported_or_js") from exc


def public_addresses(host, resolver=socket.getaddrinfo):
    addresses = resolver(host, 443, type=socket.SOCK_STREAM)
    if not addresses:
        raise FetchError("unsupported_or_js")
    ips = [item[4][0] for item in addresses]
    if any(not _public_ip(ip) for ip in ips):
        raise FetchError("unsupported_or_js")
    return ips


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host, address, timeout):
        super().__init__(host, port=443, timeout=timeout, context=ssl.create_default_context())
        self.address = address

    def connect(self):
        raw = socket.create_connection((self.address, 443), self.timeout)
        try:
            self.sock = self._context.wrap_socket(raw, server_hostname=self.host)
        except BaseException:
            raw.close()
            raise


def _open_once(url, *, timeout=TIMEOUT, on_http_start=None):
    parsed, host = validate_target(url)
    # Pin the approved DNS answer while retaining hostname verification/SNI.
    # No proxy environment, cookies, auth headers, or automatic redirects.
    address = public_addresses(host)[0]
    connection = _PinnedHTTPSConnection(host, address, timeout)
    try:
        target = urllib.parse.urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
        connection.connect()
        if on_http_start is not None:
            on_http_start()
        connection.request("GET", target, headers={"User-Agent": "Giraffe-Source/1.0", "Accept-Encoding": "identity"})
        response = connection.getresponse()
        headers = {key.lower(): value for key, value in response.getheaders()}
        body = response.read(MAX_BODY_BYTES + 1)
        if len(body) > MAX_BODY_BYTES:
            raise FetchError("extractor_failure")
        return body, headers, response.status
    finally:
        connection.close()


def fetch_https_bytes(url, *, pacer=DEFAULT_PACER, attempts=MAX_ATTEMPTS, audit=None, timeout=TIMEOUT, same_origin=False):
    _parsed, original_host = validate_target(url)
    audit = audit if audit is not None else []
    current, visited = url, {url}
    redirects = 0
    for attempt in range(max(1, min(attempts, MAX_ATTEMPTS))):
        while True:
            def start(target):
                event = {"monotonic": pacer.clock(), "at_utc": datetime.now(timezone.utc).isoformat(), "phase": "transport_attempt_start"}
                audit.append(event)
                def http_start():
                    stamp = pacer.mark_start() if hasattr(pacer, "mark_start") else pacer.clock()
                    event.update(monotonic=stamp, at_utc=datetime.now(timezone.utc).isoformat(), phase="http_request_start")
                return _open_once(target, timeout=timeout, on_http_start=http_start)
            try:
                body, headers, status = pacer.request(start, current)
            except FetchError:
                raise
            except Exception as exc:
                reason = exc.reason if isinstance(exc, urllib.error.URLError) else exc
                transient = isinstance(reason, (TimeoutError, ConnectionError))
                if transient and attempt + 1 < min(attempts, MAX_ATTEMPTS):
                    break
                raise FetchError("timeout" if transient else "extractor_failure", retryable=transient) from exc
            if len(body) > MAX_BODY_BYTES:
                raise FetchError("extractor_failure")
            if status in (301, 302, 303, 307, 308):
                location = headers.get("location")
                if not location:
                    raise FetchError("extractor_failure")
                if re.search(r"[\x00-\x20\x7f]", location):
                    raise FetchError("unsupported_or_js")
                target = urllib.parse.urljoin(current, location)
                _parsed, target_host = validate_target(target)
                if same_origin and target_host != original_host:
                    raise FetchError("unsupported_or_js")
                if target in visited or redirects >= MAX_REDIRECTS:
                    raise FetchError("redirect_loop")
                current = target
                visited.add(target)
                redirects += 1
                continue
            if status in (404, 410):
                raise FetchError("not_found")
            if status != 200:
                raise FetchError("unsupported_or_js")
            return body, headers.get("content-type"), current, headers, status
    raise FetchError("timeout", retryable=True)


class _VisibleText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.hidden = 0
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "noscript"):
            self.hidden += 1

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript") and self.hidden:
            self.hidden -= 1

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def fetch_document(url, *, pacer=DEFAULT_PACER):
    audit = []
    result = {"schema_version": "giraffe-direct-source-v1", "ok": False, "failure_class": None, "request_starts": audit}
    try:
        body, content_type, final_url, _headers, _status = fetch_https_bytes(url, pacer=pacer, audit=audit)
        if not content_type or content_type.split(";", 1)[0].strip().lower() not in ("text/html", "text/plain", "application/xhtml+xml", "application/json"):
            raise FetchError("unsupported_or_js")
        match = re.search(r"charset\s*=\s*['\"]?([\w.-]+)", content_type, re.I)
        if not match:
            match = re.search(br"<meta[^>]+charset\s*=\s*['\"]?([\w.-]+)", body, re.I)
        charset = match.group(1) if match else "utf-8"
        if isinstance(charset, bytes):
            charset = charset.decode("ascii")
        document = body.decode(charset, "strict")
        if "\ufffd" in document:
            raise FetchError("extractor_failure")
        if "html" in content_type.split(";", 1)[0].lower():
            parser = _VisibleText()
            parser.feed(document)
            text = " ".join(" ".join(parser.parts).split())
        else:
            text = document.strip()
        if (len(text) < 20 or (len(text) < 500 and any(marker in text.lower() for marker in
                ("enable javascript", "javascript is required", "verify you are human", "access denied")))):
            raise FetchError("unsupported_or_js")
        # URL queries and all response headers are excluded from CLI receipts.
        parsed = urllib.parse.urlsplit(final_url)
        result.update(ok=True, final_url=urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", "")), text=text, raw_bytes=len(body))
    except FetchError as exc:
        result["failure_class"] = exc.failure_class
    except Exception:
        result["failure_class"] = "extractor_failure"
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url")
    args = parser.parse_args(argv)
    result = fetch_document(args.url)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
