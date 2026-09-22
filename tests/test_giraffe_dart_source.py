import importlib.util
import json
import pathlib
import sys
import urllib.error
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

ROOT = pathlib.Path(__file__).parents[1]
spec = importlib.util.spec_from_file_location("dart_source", ROOT / "scripts" / "giraffe_dart_source.py")
source = importlib.util.module_from_spec(spec)
assert spec and spec.loader
sys.modules["dart_source"] = source
spec.loader.exec_module(source)


class FakeClock:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds

    def advance(self, seconds):
        self.now += seconds


def main_page(rcp="20260911800823", dcm="11577485"):
    return (f"<html><meta charset='utf-8'><script>viewDoc('{rcp}','{dcm}','0','0','0','HTML','')</script></html>").encode()


def fetch_from(mapping):
    def fetch(url):
        for needle, value in mapping.items():
            if needle in url: return value
        raise AssertionError(url)
    return fetch


@pytest.mark.parametrize("charset", ["cp949", "ms949"])
def test_cp949_and_ms949_packets_are_strictly_decoded(charset):
    rcp = "20260911800823"
    viewer = "<html><meta charset='%s'><body>삼성전자 공급계약 체결 공시 본문입니다.</body></html>" % charset
    packet = source.source_packet(rcp, fetch_from({"main.do": (main_page(rcp), "text/html; charset=utf-8", "https://dart.fss.or.kr/dsaf001/main.do?rcpNo=" + rcp), "viewer.do": (viewer.encode(charset), "text/html; charset=" + charset, "https://dart.fss.or.kr/report/viewer.do?rcpNo=" + rcp + "&dcmNo=11577485&eleId=0&offset=0&length=0&dtd=HTML")}))
    assert packet["source_valid"] is True and packet["charset"].lower() == charset


def test_utf8_and_meta_fallback_are_valid():
    rcp = "20260911800823"
    viewer = "<html><meta charset='utf-8'><body>UTF8 DART disclosure body is long enough.</body></html>"
    packet = source.source_packet(rcp, fetch_from({"main.do": (main_page(rcp), None, "https://dart.fss.or.kr/dsaf001/main.do?rcpNo=" + rcp), "viewer.do": (viewer.encode(), None, "https://dart.fss.or.kr/report/viewer.do?rcpNo=" + rcp + "&dcmNo=11577485&eleId=0&offset=0&length=0&dtd=HTML")}))
    assert packet["charset"] == "utf-8"

@pytest.mark.parametrize("viewer,content_type,code", [
    (b"<html><body>\xff\xfe\xff</body></html>", "text/html; charset=utf-8", "SOURCE_DECODE_ERROR"),
    ("<html><meta charset='utf-8'><body>占쏙옙 document body longer enough</body></html>".encode(), None, "SOURCE_DECODE_ERROR"),
    (b"<html><meta charset='utf-8'><body>login please now with enough body</body></html>", None, "SOURCE_EXTRACT_ERROR"),
])
def test_wrong_charset_mojibake_and_login_are_typed_failures(viewer, content_type, code):
    rcp = "20260911800823"
    with pytest.raises(source.SourceError) as exc:
        source.source_packet(rcp, fetch_from({"main.do": (main_page(rcp), "text/html; charset=utf-8", "https://dart.fss.or.kr/dsaf001/main.do?rcpNo=" + rcp), "viewer.do": (viewer, content_type, "https://dart.fss.or.kr/report/viewer.do?rcpNo=" + rcp + "&dcmNo=11577485&eleId=0&offset=0&length=0&dtd=HTML")}))
    assert exc.value.code == code


def test_missing_dcm_and_wrong_final_url_fail_closed():
    with pytest.raises(source.SourceError, match="SOURCE_EXTRACT_ERROR"):
        source.canonical_viewer_url("<html></html>", "20260911800823")
    with pytest.raises(source.SourceError, match="SOURCE_FETCH_ERROR"):
        source.validate_viewer("valid document body with enough content", "https://dart.fss.or.kr/report/viewer.do?rcpNo=20260911800823", "https://evil.example/viewer", "20260911800823")


def test_packet_keeps_main_raw_and_only_verifiable_header_projection():
    rcp = "20260911800823"
    viewer = b"<html><meta charset='utf-8'><body>safe document body with enough content</body></html>"
    packet = source.source_packet(rcp, fetch_from({
        "main.do": (main_page(rcp), "text/html; charset=utf-8", "https://dart.fss.or.kr/dsaf001/main.do?rcpNo=" + rcp, {"Set-Cookie": "secret", "X-Trace": "trace", "Content-Length": str(len(main_page(rcp)))}, 200),
        "viewer.do": (viewer, "text/html; charset=utf-8", "https://dart.fss.or.kr/report/viewer.do?rcpNo=" + rcp + "&dcmNo=11577485&eleId=0&offset=0&length=0&dtd=HTML", {"Authorization": "secret", "X-Api-Key": "secret", "Content-Type": "text/html", "Content-Length": str(len(viewer))}, 200),
    }))
    assert packet["main_raw_sha256"] and packet["main_raw_bytes"] == len(main_page(rcp))
    assert packet["main_response_status"] == packet["response_status"] == 200
    assert packet["main_response_headers"] == {"content-type": "text/html; charset=utf-8", "content-length": str(len(main_page(rcp)))}
    assert packet["response_headers"] == {"content-type": "text/html; charset=utf-8", "content-length": str(len(viewer))}


def test_main_redirect_and_bad_content_length_fail_closed():
    rcp = "20260911800823"
    viewer = b"<html><meta charset='utf-8'><body>safe document body with enough content</body></html>"
    with pytest.raises(source.SourceError, match="main URL canonical identity mismatch"):
        source.source_packet(rcp, fetch_from({"main.do": (main_page(rcp), "text/html; charset=utf-8", "https://dart.fss.or.kr/dsaf001/main.do?rcpNo=" + rcp + "&redirect=1")}))
    with pytest.raises(source.SourceError, match="invalid content-length receipt"):
        source.source_packet(rcp, fetch_from({
            "main.do": (main_page(rcp), "text/html; charset=utf-8", "https://dart.fss.or.kr/dsaf001/main.do?rcpNo=" + rcp),
            "viewer.do": (viewer, "text/html; charset=utf-8", "https://dart.fss.or.kr/report/viewer.do?rcpNo=" + rcp + "&dcmNo=11577485&eleId=0&offset=0&length=0&dtd=HTML", {"content-length": "1"}),
        }))


def test_checkpoint_rejects_cross_directory_symlink_and_corrupt_siblings(tmp_path):
    rcp = "20260911800823"
    packet = source.source_packet(rcp, fetch_from({"main.do": (main_page(rcp), "text/html; charset=utf-8", "https://dart.fss.or.kr/dsaf001/main.do?rcpNo=" + rcp), "viewer.do": (b"<html><meta charset='utf-8'><body>valid document body with enough content</body></html>", "text/html; charset=utf-8", "https://dart.fss.or.kr/report/viewer.do?rcpNo=" + rcp + "&dcmNo=11577485&eleId=0&offset=0&length=0&dtd=HTML")}))
    packet_dir = tmp_path / rcp[:8]
    checkpoint = source.write_packet(packet, packet_dir)
    assert source.completed_packet(checkpoint, rcp) is not None
    copied = tmp_path / "other" / f"{rcp}.json"; copied.parent.mkdir(); copied.write_bytes(checkpoint.read_bytes())
    assert source.completed_packet(copied, rcp) is None
    metadata = json.loads(checkpoint.read_text())
    raw = packet_dir / f"{rcp}.viewer.raw"; raw.write_bytes(b"corrupt")
    assert source.completed_packet(checkpoint, rcp) is None
    raw.unlink(); raw.symlink_to(packet_dir / f"{rcp}.viewer.txt")
    assert source.completed_packet(checkpoint, rcp) is None


def test_checkpoint_reuses_receipt_in_explicit_later_control_date_without_weakening_containment(tmp_path):
    rcp = "20260914000432"
    control_date = "20260915"
    packet = source.source_packet(rcp, fetch_from({
        "main.do": (main_page(rcp), "text/html; charset=utf-8", "https://dart.fss.or.kr/dsaf001/main.do?rcpNo=" + rcp),
        "viewer.do": (b"<html><meta charset='utf-8'><body>valid correction disclosure source body</body></html>", "text/html; charset=utf-8", "https://dart.fss.or.kr/report/viewer.do?rcpNo=" + rcp + "&dcmNo=11577485&eleId=0&offset=0&length=0&dtd=HTML"),
    }))
    checkpoint = source.write_packet(packet, tmp_path / control_date)

    assert source.completed_packet(checkpoint, rcp, expected_control_date=control_date) is not None
    assert source.completed_packet(checkpoint, rcp, expected_control_date=rcp[:8]) is None
    assert source.completed_packet(checkpoint, rcp, expected_control_date="bad-date") is None


def test_checkpoint_rederives_semantics_and_rejects_resealed_tampering(tmp_path):
    rcp = "20260911800823"
    packet = source.source_packet(rcp, fetch_from({
        "main.do": (main_page(rcp), "text/html; charset=utf-8", "https://dart.fss.or.kr/dsaf001/main.do?rcpNo=" + rcp, {"content-type": "text/html; charset=utf-8"}, 200),
        "viewer.do": (b"<html><meta charset='utf-8'><body>valid document body with enough content</body></html>", "text/html; charset=utf-8", "https://dart.fss.or.kr/report/viewer.do?rcpNo=" + rcp + "&dcmNo=11577485&eleId=0&offset=0&length=0&dtd=HTML", {"content-type": "text/html; charset=utf-8"}, 200),
    }))
    packet_dir = tmp_path / rcp[:8]; checkpoint = source.write_packet(packet, packet_dir)
    def reseal(change):
        metadata = json.loads(checkpoint.read_text()); change(metadata)
        checkpoint.write_text(json.dumps(metadata), encoding="utf-8")
        assert source.completed_packet(checkpoint, rcp) is None
        checkpoint.write_bytes(original)
    original = checkpoint.read_bytes()
    viewer_text = packet_dir / f"{rcp}.viewer.txt"
    viewer_text.write_text("<html><body>resealed but unrelated source body long enough</body></html>", encoding="utf-8")
    metadata = json.loads(checkpoint.read_text()); metadata["text_sha256"] = __import__("hashlib").sha256(viewer_text.read_bytes()).hexdigest(); metadata["text_chars"] = len(viewer_text.read_text()); checkpoint.write_text(json.dumps(metadata), encoding="utf-8")
    assert source.completed_packet(checkpoint, rcp) is None
    viewer_text.write_text(packet["text"], encoding="utf-8"); checkpoint.write_bytes(original)
    reseal(lambda m: m.update(main_final_url="https://dart.fss.or.kr/dsaf001/main.do?rcpNo=" + rcp + "&extra=1"))
    reseal(lambda m: m.update(final_url="https://dart.fss.or.kr/report/viewer.do?rcpNo=" + rcp + "&dcmNo=x"))
    reseal(lambda m: m["response_headers"].update({"x-unverifiable": "forged"}))
    reseal(lambda m: m["main_response_headers"].update({"x-unverifiable": "forged"}))
    reseal(lambda m: m["response_headers"].update({"content-type": "text/html; charset=cp949"}))
    reseal(lambda m: m["response_headers"].update({"content-length": "999"}))
    reseal(lambda m: m.update(response_status=201))
    reseal(lambda m: m.update(retrieved_at_kst="2999-01-01T00:00:00+09:00"))
    reseal(lambda m: m.update(retrieved_at_kst="2000-01-01T00:00:00+09:00"))
    reseal(lambda m: m.update(retrieved_at_kst="2026-09-11T07:00:00"))
    reseal(lambda m: m.update(source_date="20200101"))
    reseal(lambda m: m.update(main_raw_sha256="0" * 64))
    escaped = tmp_path / "escaped.raw"; escaped.write_bytes((packet_dir / f"{rcp}.viewer.raw").read_bytes())
    (packet_dir / f"{rcp}.viewer.raw").unlink(); (packet_dir / f"{rcp}.viewer.raw").symlink_to(escaped)
    assert source.completed_packet(checkpoint, rcp) is None


def test_checkpoint_resume_preserves_authoritative_crlf_text(tmp_path):
    rcp = "20260911800823"
    viewer = b"<html>\r\n<meta charset='utf-8'>\r\n<body>valid document body with enough content</body>\r\n</html>"
    packet = source.source_packet(rcp, fetch_from({
        "main.do": (main_page(rcp), "text/html; charset=utf-8", "https://dart.fss.or.kr/dsaf001/main.do?rcpNo=" + rcp),
        "viewer.do": (viewer, "text/html; charset=utf-8", "https://dart.fss.or.kr/report/viewer.do?rcpNo=" + rcp + "&dcmNo=11577485&eleId=0&offset=0&length=0&dtd=HTML"),
    }))
    checkpoint = source.write_packet(packet, tmp_path / rcp[:8])

    resumed = source.completed_packet(checkpoint, rcp)

    assert resumed is not None
    assert (tmp_path / rcp[:8] / f"{rcp}.viewer.txt").read_bytes().decode("utf-8") == packet["text"]


def test_checkpoint_freshness_has_deterministic_kst_bounds(tmp_path):
    rcp = "20260911800823"
    viewer = b"<html><meta charset='utf-8'><body>valid document body with enough content</body></html>"
    packet = source.source_packet(rcp, fetch_from({
        "main.do": (main_page(rcp), "text/html; charset=utf-8", "https://dart.fss.or.kr/dsaf001/main.do?rcpNo=" + rcp),
        "viewer.do": (viewer, "text/html; charset=utf-8", "https://dart.fss.or.kr/report/viewer.do?rcpNo=" + rcp + "&dcmNo=11577485&eleId=0&offset=0&length=0&dtd=HTML"),
    }))
    checkpoint = source.write_packet(packet, tmp_path / rcp[:8])
    now = datetime(2026, 9, 12, 7, 0, tzinfo=ZoneInfo("Asia/Seoul"))
    metadata = json.loads(checkpoint.read_text())
    for captured, accepted in ((now - timedelta(hours=72), True), (now - timedelta(hours=72, seconds=1), False), (now + timedelta(minutes=5), True), (now + timedelta(minutes=5, seconds=1), False)):
        metadata["retrieved_at_kst"] = captured.isoformat()
        checkpoint.write_text(json.dumps(metadata), encoding="utf-8")
        assert (source.completed_packet(checkpoint, rcp, now=now) is not None) is accepted


def test_source_packet_paces_main_and_viewer_request_starts_one_second_apart():
    rcp = "20260911800823"
    clock = FakeClock()
    requests = []
    viewer = b"<html><meta charset='utf-8'><body>valid paced disclosure source body</body></html>"

    def fetch(url):
        requests.append((url, clock()))
        if "main.do" in url:
            clock.advance(0.25)
            return main_page(rcp), "text/html; charset=utf-8", url
        return viewer, "text/html; charset=utf-8", url

    packet = source.source_packet(rcp, fetch, clock=clock, sleep=clock.sleep)

    assert packet["source_valid"] is True
    assert [url.split("?", 1)[0].rsplit("/", 1)[-1] for url, _ in requests] == ["main.do", "viewer.do"]
    assert requests[1][1] - requests[0][1] == pytest.approx(1.0)
    assert clock.sleeps == pytest.approx([0.75])


@pytest.mark.parametrize("configured_attempts", [None, 99])
def test_transient_retry_backoff_defaults_and_caps_at_four_attempts(configured_attempts):
    rcp = "20260911800823"
    clock = FakeClock()
    request_times = []

    def fetch(_url):
        request_times.append(clock())
        raise TimeoutError("temporary DART timeout")

    kwargs = {} if configured_attempts is None else {"retries": configured_attempts}
    with pytest.raises(source.SourceError) as exc:
        source.fetch_with_retry(rcp, fetch=fetch, clock=clock, sleep=clock.sleep, **kwargs)

    assert exc.value.code == "SOURCE_FETCH_ERROR"
    assert request_times == pytest.approx([0.0, 2.0, 6.0, 14.0])
    assert clock.sleeps == pytest.approx([2.0, 4.0, 8.0])


def test_non_transient_source_validation_failure_is_not_retried():
    rcp = "20260911800823"
    clock = FakeClock()
    requests = []
    login_shell = b"<html><meta charset='utf-8'><body>login page with enough content to parse</body></html>"

    def fetch(url):
        requests.append(url)
        if "main.do" in url:
            return main_page(rcp), "text/html; charset=utf-8", url
        return login_shell, "text/html; charset=utf-8", url

    with pytest.raises(source.SourceError) as exc:
        source.fetch_with_retry(rcp, fetch=fetch, clock=clock, sleep=clock.sleep)

    assert exc.value.code == "SOURCE_EXTRACT_ERROR"
    assert [url.split("?", 1)[0].rsplit("/", 1)[-1] for url in requests] == ["main.do", "viewer.do"]
    assert clock.sleeps == pytest.approx([1.0])


def test_viewer_timeout_retries_without_adding_pacing_to_backoff():
    rcp = "20260911800823"
    clock = FakeClock()
    requests = []

    def fetch(url):
        requests.append(("main.do" if "main.do" in url else "viewer.do", clock()))
        clock.advance(0.25)
        if "main.do" in url:
            return main_page(rcp), "text/html; charset=utf-8", url
        if len(requests) == 2:
            raise urllib.error.URLError(TimeoutError("temporary timeout"))
        return b"<body>valid disclosure body with enough content</body>", "text/html; charset=utf-8", url

    assert source.fetch_with_retry(rcp, fetch=fetch, clock=clock, sleep=clock.sleep)["source_valid"] is True
    assert requests == [("main.do", 0.0), ("viewer.do", 1.0), ("main.do", 3.25), ("viewer.do", 4.25)]
    assert clock.sleeps == pytest.approx([0.75, 2.0, 0.75])


@pytest.mark.parametrize("error", [
    ValueError("invalid response"),
    urllib.error.HTTPError("https://dart.fss.or.kr", 403, "Forbidden", {}, None),
    source.SourceError("SOURCE_FETCH_ERROR", "invalid receipt"),
])
def test_non_transport_fetch_errors_are_not_retried(error):
    clock = FakeClock()
    requests = []

    def fetch(url):
        requests.append(url)
        raise error

    with pytest.raises(source.SourceError):
        source.fetch_with_retry("20260911800823", fetch=fetch, clock=clock, sleep=clock.sleep)
    assert len(requests) == 1
    assert clock.sleeps == []


def test_slow_main_request_needs_no_additional_pacing_sleep():
    clock = FakeClock()
    requests = []

    def fetch(url):
        requests.append(clock())
        if "main.do" in url:
            clock.advance(1.5)
            return main_page(), "text/html; charset=utf-8", url
        return b"<body>valid disclosure body with enough content</body>", "text/html; charset=utf-8", url

    source.source_packet("20260911800823", fetch, clock=clock, sleep=clock.sleep)
    assert requests == [0.0, 1.5]
    assert clock.sleeps == []


def test_default_pacing_is_shared_between_packet_calls(monkeypatch):
    clock = FakeClock()
    pacing = source._RequestPacer(clock, clock.sleep)
    requests = []

    def fetch(url):
        requests.append(clock())
        raw = main_page() if "main.do" in url else b"<body>valid disclosure body with enough content</body>"
        return raw, "text/html; charset=utf-8", url

    source.source_packet("20260911800823", fetch, pacer=pacing)
    source.fetch_with_retry("20260911800823", fetch=fetch, pacer=pacing)
    assert requests == [0.0, 1.0, 2.0, 3.0]
    assert clock.sleeps == [1.0, 1.0, 1.0]
