"""Offline transport and separate-interpreter pacing regressions."""
import importlib
import json
import os
import pathlib
import ssl
import subprocess
import sys
import urllib.error

import pytest

ROOT = pathlib.Path(__file__).parents[1]


@pytest.fixture
def direct(monkeypatch, tmp_path):
    monkeypatch.setenv("GIRAFFE_HTTP_PACER_STATE", str(tmp_path / "pacing.json"))
    return importlib.import_module("scripts.giraffe_direct_fetch")


class Clock:
    now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, delay):
        self.now += delay


def pacer(direct):
    clock = Clock()
    return direct.LocalPacer(clock, clock.sleep), clock


def response(url, status=200, body=b"<html><body>A verified issuer announcement with sufficient text.</body></html>", **headers):
    return body, {"content-type": "text/html; charset=utf-8", **headers}, status


@pytest.mark.parametrize("url", [
    "http://issuer.example/news", "https://user:secret@issuer.example/news",
    "https://issuer.example/news#fragment", "https://issuer.example/\nnews",
    "https://issuer.example/a b", "https://127.0.0.1/news", "https://[::1]/news",
    "https://issuer.example/%0d%0asecret", "https://issuer.example:bad/news",
    "https://issuer.example:444/news", "https://224.0.0.1/news",
    "https://169.254.169.254/latest", "https://100.64.0.1/news",
    "https://0.0.0.0/news", "https://192.168.1.1/news",
])
def test_rejects_unsafe_targets_before_http(direct, monkeypatch, url):
    monkeypatch.setattr(direct, "_open_once", lambda *_args, **_kwargs: pytest.fail("unsafe URL reached transport"))
    result = direct.fetch_document(url)
    assert result["failure_class"] == "unsupported_or_js"
    assert result["request_starts"] == []
    assert "secret" not in json.dumps(result)


def test_redirect_retry_hops_are_individually_paced_and_audited(direct, monkeypatch):
    pacing, clock = pacer(direct)
    seen = []

    def open_once(url, **kwargs):
        seen.append((url, clock()))
        if len(seen) == 1:
            raise TimeoutError("secret transport diagnostics")
        if url.endswith("/start"):
            return response(url, 302, location="/news")
        return response(url)

    monkeypatch.setattr(direct, "_open_once", open_once)
    result = direct.fetch_document("https://issuer.example/start", pacer=pacing)
    assert result["ok"] is True
    assert result["final_url"] == "https://issuer.example/news"
    assert len(result["request_starts"]) == 3
    assert [item["monotonic"] for item in result["request_starts"]] == [stamp for _, stamp in seen]
    assert all(b[1] - a[1] >= 0.5 for a, b in zip(seen, seen[1:]))
    assert all(item["at_utc"].endswith("+00:00") for item in result["request_starts"])
    assert "secret" not in json.dumps(result)


@pytest.mark.parametrize("mode,expected,max_calls", [
    ("timeout", "timeout", 3), ("redirect", "redirect_loop", 2),
    ("not_found", "not_found", 1), ("bad_charset", "extractor_failure", 1),
    ("js", "unsupported_or_js", 1), ("unsafe_redirect", "unsupported_or_js", 1),
    ("binary", "unsupported_or_js", 1), ("tls", "extractor_failure", 1),
    ("oversize", "extractor_failure", 1),
])
def test_bounded_closed_failure_classes(direct, monkeypatch, mode, expected, max_calls):
    calls = []
    pacing, _ = pacer(direct)

    def open_once(url, **kwargs):
        calls.append(url)
        if mode == "timeout":
            raise urllib.error.URLError(TimeoutError("secret"))
        if mode == "tls":
            raise urllib.error.URLError(ssl.SSLCertVerificationError("secret"))
        if mode == "redirect":
            return response(url, 302, location="/b" if url.endswith("/a") else "/a")
        if mode == "unsafe_redirect":
            return response(url, 302, location="http://issuer.example/news?token=secret")
        if mode == "not_found":
            return response(url, 404)
        if mode == "bad_charset":
            return response(url, body=b"\xff", **{"content-type": "text/html; charset=utf-8"})
        if mode == "js":
            return response(url, body=b"<html><script>secret content with sufficient length</script></html>")
        if mode == "binary":
            return response(url, **{"content-type": "application/pdf"})
        return response(url, body=b"x" * (direct.MAX_BODY_BYTES + 1))

    monkeypatch.setattr(direct, "_open_once", open_once)
    result = direct.fetch_document("https://issuer.example/a", pacer=pacing)
    assert result["ok"] is False
    assert result["failure_class"] == expected
    assert len(calls) == max_calls
    assert "secret" not in json.dumps(result)


def test_cli_invalid_target_is_json_nonzero_without_secret():
    result = subprocess.run([sys.executable, str(ROOT / "scripts/giraffe_direct_fetch.py"), "https://user:secret@issuer.example/"], capture_output=True, text=True)
    assert result.returncode == 1
    assert json.loads(result.stdout)["failure_class"] == "unsupported_or_js"
    assert "secret" not in result.stdout + result.stderr


def test_real_processes_share_dart_manifest_direct_retry_and_redirect_pacing(tmp_path):
    code = r'''
import json, sys, time
from scripts import giraffe_direct_fetch as direct
from scripts import giraffe_dart_source as dart
from scripts import giraffe_dart_manifest as manifest
events = []
def open_once(url, **kwargs):
    events.append(time.monotonic())
    if sys.argv[1] == "manifest":
        if "/api/list.json" in url:
            return b"", {"location": "/api/final.json"}, 302
        return b'{"status":"013"}', {"content-type": "application/json"}, 200
    if sys.argv[1] == "dart":
        if len(events) == 1:
            raise TimeoutError("offline transient")
        body = ("<script>viewDoc('20260911800823','11577485','0','0','0','HTML','')</script>"
                if "main.do" in url else "<body>Valid DART disclosure body with enough text.</body>")
        return body.encode(), {"content-type": "text/html; charset=utf-8"}, 200
    if url.endswith("/start"):
        return b"", {"location": "/news"}, 302
    return b"<body>Valid issuer announcement with enough text.</body>", {"content-type": "text/html; charset=utf-8"}, 200
direct._open_once = open_once
began = time.monotonic()
if sys.argv[1] == "dart":
    assert dart.fetch_with_retry("20260911800823")["source_valid"]
elif sys.argv[1] == "manifest":
    assert manifest.fetch_page("20260921", 1, api_key="dummy-offline-key")["status"] == "013"
else:
    assert direct.fetch_document("https://issuer.example/start")["ok"]
print(json.dumps({"began": began, "events": events}))
'''
    env = dict(os.environ, GIRAFFE_HTTP_PACER_STATE=str(tmp_path / "shared.json"))
    processes = [subprocess.Popen([sys.executable, "-c", code, kind], cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for kind in ("dart", "manifest", "direct")]
    records = []
    for process in processes:
        stdout, stderr = process.communicate(timeout=20)
        assert process.returncode == 0, stderr
        records.append(json.loads(stdout))
    starts = sorted(stamp for record in records for stamp in record["events"])
    assert len(starts) == 7  # DART timeout/main/viewer; manifest and direct redirect/document.
    assert starts[0] - min(record["began"] for record in records) < 0.5
    assert all(b - a >= 0.5 for a, b in zip(starts, starts[1:])), starts


def test_prompt_routes_direct_access_through_helper():
    prompt = (ROOT / "prompts/giraffe-material-discovery-v1.md").read_text()
    assert "python scripts/giraffe_direct_fetch.py" in prompt
    assert "web_search`와 `web_extract`는 로컬 pacer의 적용 대상이 아니다" in prompt


@pytest.mark.parametrize("addresses", [["127.0.0.1"], ["8.8.8.8", "10.0.0.1"], ["224.0.0.1"], ["::1"], ["169.254.169.254"], []])
def test_dns_rejects_any_nonpublic_answer(direct, addresses):
    def resolve(*args, **kwargs):
        return [(2, 1, 6, "", (address, 443)) for address in addresses]

    with pytest.raises(direct.FetchError, match="unsupported_or_js"):
        direct.public_addresses("issuer.example", resolver=resolve)


def test_pinned_connection_preserves_tls_hostname_and_uses_approved_ip(direct, monkeypatch):
    calls = []
    raw = object()
    monkeypatch.setattr(direct.socket, "create_connection", lambda address, timeout: calls.append((address, timeout)) or raw)
    connection = direct._PinnedHTTPSConnection("issuer.example", "8.8.8.8", 15)
    assert connection._context.verify_mode == ssl.CERT_REQUIRED
    assert connection._context.check_hostname is True
    class Context:
        def wrap_socket(self, sock, *, server_hostname):
            assert sock is raw
            calls.append(server_hostname)
            return "tls-socket"
    connection._context = Context()
    connection.connect()
    assert calls == [(("8.8.8.8", 443), 15), "issuer.example"]
    assert connection.sock == "tls-socket"


def test_transport_reads_only_bounded_body_without_auth_or_proxy(direct, monkeypatch):
    calls = []
    class Response:
        status = 200
        def getheaders(self):
            return [("Content-Type", "text/html")]
        def read(self, bound):
            calls.append(bound)
            return b"x" * bound
    class Connection:
        def __init__(self, *args):
            calls.append(args)
        def connect(self):
            pass
        def request(self, method, target, *, headers):
            assert method == "GET" and target == "/news?public=value"
            assert set(headers) == {"User-Agent", "Accept-Encoding"}
        def getresponse(self):
            return Response()
        def close(self):
            calls.append("closed")
    monkeypatch.setattr(direct, "public_addresses", lambda host: ["8.8.8.8"])
    monkeypatch.setattr(direct, "_PinnedHTTPSConnection", Connection)
    with pytest.raises(direct.FetchError, match="extractor_failure"):
        direct._open_once("https://issuer.example/news?public=value")
    assert calls == [("issuer.example", "8.8.8.8", 15.0), direct.MAX_BODY_BYTES + 1, "closed"]


def test_success_omits_query_secrets_and_response_headers(direct, monkeypatch):
    pacing, _ = pacer(direct)
    monkeypatch.setattr(direct, "_open_once", lambda url, **kwargs: response(url, **{"set-cookie": "secret", "x-token": "secret"}))
    result = direct.fetch_document("https://issuer.example/news?token=secret", pacer=pacing)
    assert result["ok"] is True
    assert result["final_url"] == "https://issuer.example/news"
    assert "secret" not in json.dumps(result)


def test_redirect_limit_applies_to_distinct_urls(direct, monkeypatch):
    pacing, _ = pacer(direct)
    calls = []
    def open_once(url, **kwargs):
        calls.append(url)
        return response(url, 302, location=f"/hop/{len(calls)}")
    monkeypatch.setattr(direct, "_open_once", open_once)
    result = direct.fetch_document("https://issuer.example/start", pacer=pacing)
    assert result["failure_class"] == "redirect_loop"
    assert len(calls) == direct.MAX_REDIRECTS + 1


def test_success_cli_emits_json_and_zero_exit(direct, monkeypatch, capsys):
    monkeypatch.setattr(direct, "fetch_document", lambda url: {"ok": True, "failure_class": None, "text": "announcement"})
    assert direct.main(["https://issuer.example/news"]) == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True


def test_javascript_challenge_is_a_fallback(direct, monkeypatch):
    pacing, _ = pacer(direct)
    monkeypatch.setattr(direct, "_open_once", lambda url, **kwargs: response(url, body=b"<body>Please enable JavaScript to continue reading this site.</body>"))
    assert direct.fetch_document("https://issuer.example/news", pacer=pacing)["failure_class"] == "unsupported_or_js"


def test_manifest_redirect_hops_use_common_pacer_without_double_wait(direct, monkeypatch):
    from scripts import giraffe_dart_manifest as manifest
    monkeypatch.setattr(manifest.urllib.request, "urlopen", lambda *args, **kwargs: pytest.fail("legacy unpaced redirect transport"))
    pacing, clock = pacer(direct)
    seen = []
    def open_once(url, **kwargs):
        seen.append(clock())
        if len(seen) == 1:
            return b"", {"location": "/api/redirected.json"}, 302
        return b'{"status":"013"}', {"content-type": "application/json"}, 200
    monkeypatch.setattr(direct, "_open_once", open_once)
    assert manifest.fetch_page("20260921", 1, api_key="secret", pacer=pacing, retries=1) == {"status": "013"}
    assert seen == [0.0, 1.0]


def test_manifest_transport_failure_never_echoes_api_key(direct, monkeypatch):
    from scripts import giraffe_dart_manifest as manifest
    monkeypatch.setattr(manifest.urllib.request, "urlopen", lambda *args, **kwargs: pytest.fail("legacy unpaced redirect transport"))
    pacing, _ = pacer(direct)
    def open_once(url, **kwargs):
        raise TimeoutError(url)
    monkeypatch.setattr(direct, "_open_once", open_once)
    with pytest.raises(manifest.ManifestError) as exc:
        manifest.fetch_page("20260921", 1, api_key="secret", pacer=pacing, retries=1)
    assert "secret" not in str(exc.value)


def test_audit_http_boundary_follows_dns_and_tls_setup(direct, monkeypatch):
    pacing, clock = pacer(direct)
    def resolve(host):
        clock.now += 3
        return ["8.8.8.8"]
    class Response:
        status = 200
        def getheaders(self):
            return [("Content-Type", "text/html; charset=utf-8")]
        def read(self, bound):
            return b"<body>Verified announcement with sufficient text.</body>"
    class Connection:
        def __init__(self, *args):
            pass
        def connect(self):
            clock.now += 2
        def request(self, *args, **kwargs):
            assert clock.now == 5
        def getresponse(self):
            return Response()
        def close(self):
            pass
    monkeypatch.setattr(direct, "public_addresses", resolve)
    monkeypatch.setattr(direct, "_PinnedHTTPSConnection", Connection)
    result = direct.fetch_document("https://issuer.example/news", pacer=pacing)
    assert result["ok"] is True
    assert result["request_starts"][0]["monotonic"] == 5
    assert result["request_starts"][0]["phase"] == "http_request_start"
    assert pacing.next_start == 6


def test_manifest_key_cannot_be_redirected_to_another_host(direct, monkeypatch):
    from scripts import giraffe_dart_manifest as manifest
    pacing, _ = pacer(direct)
    calls = []
    def open_once(url, **kwargs):
        calls.append(url)
        return b"", {"location": "https://other.example/steal?crtfc_key=secret"}, 302
    monkeypatch.setattr(direct, "_open_once", open_once)
    with pytest.raises(manifest.ManifestError):
        manifest.fetch_page("20260921", 1, api_key="secret", pacer=pacing, retries=1)
    assert len(calls) == 1
