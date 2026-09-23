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


def test_packet_captures_every_main_viewer_section_in_document_order(tmp_path):
    rcp = "20260911800823"
    main = b"""<html><meta charset='utf-8'><script>
viewDoc('20260911800823','11577485','1','0','10','HTML','');
viewDoc('20260911800823','11577485','7','10','20','HTML','');
</script></html>"""
    urls = []
    def fetch(url):
        urls.append(url)
        if "main.do" in url:
            return main, "text/html; charset=utf-8", url
        ele = __import__("urllib.parse").parse.parse_qs(__import__("urllib.parse").parse.urlparse(url).query)["eleId"][0]
        body = ("cover header only sufficient body" if ele == "1" else "merger consideration ratio and effective date substantive body")
        return ("<html><meta charset='utf-8'><body>" + body + "</body></html>").encode(), "text/html; charset=utf-8", url

    packet = source.source_packet(rcp, fetch)
    checkpoint = source.write_packet(packet, tmp_path / rcp[:8])

    assert packet["schema_version"] == "giraffe-dart-source-packet-v3"
    assert [section["canonical_viewer_url"] for section in packet["sections"]] == urls[1:]
    assert [__import__("urllib.parse").parse.parse_qs(__import__("urllib.parse").parse.urlparse(url).query)["eleId"][0] for url in urls[1:]] == ["1", "7"]
    assert "merger consideration ratio" in packet["text"]
    assert source.completed_packet(checkpoint, rcp) is not None


def test_packet_rejects_duplicate_or_partial_section_fetch():
    rcp = "20260911800823"
    duplicate = b"<html><meta charset='utf-8'><script>viewDoc('20260911800823','11577485','1','0','10','HTML','');viewDoc('20260911800823','11577485','1','0','10','HTML','');</script></html>"
    with pytest.raises(source.SourceError, match="duplicate"):
        source.source_packet(rcp, fetch_from({"main.do": (duplicate, "text/html; charset=utf-8", "https://dart.fss.or.kr/dsaf001/main.do?rcpNo=" + rcp)}))
    main = b"<html><meta charset='utf-8'><script>viewDoc('20260911800823','11577485','1','0','10','HTML','');viewDoc('20260911800823','11577485','2','10','10','HTML','');</script></html>"
    with pytest.raises(source.SourceError, match="invalid fetch response"):
        source.source_packet(rcp, fetch_from({
            "main.do": (main, "text/html; charset=utf-8", "https://dart.fss.or.kr/dsaf001/main.do?rcpNo=" + rcp),
            "eleId=1": (b"<html><meta charset='utf-8'><body>first complete source body sufficient</body></html>", "text/html; charset=utf-8", "https://dart.fss.or.kr/report/viewer.do?rcpNo=" + rcp + "&dcmNo=11577485&eleId=1&offset=0&length=10&dtd=HTML"),
            "eleId=2": (b"partial", "text/html; charset=utf-8", "https://dart.fss.or.kr/report/viewer.do?rcpNo=" + rcp + "&dcmNo=11577485&eleId=2&offset=10&length=10&dtd=HTML", {}, 206),
        }))

def test_tree_declaration_controls_noncontiguous_document_order():
    rcp = "20260911800823"
    def node(ele, toc, offset):
        return f'''var node1 = {{}};
node1['rcpNo'] = "{rcp}"; node1['dcmNo'] = "11577485"; node1['eleId'] = "{ele}";
node1['offset'] = "{offset}"; node1['length'] = "10"; node1['dtd'] = "HTML";
node1['tocNo'] = "{toc}"; node1['atocId'] = "{toc}"; treeData.push(node1);'''
    main = ("<meta charset='utf-8'><script>" + node("9", "1", "0") + node("2", "2", "10") + "</script>").encode()
    packet = source.source_packet(rcp, fetch_from({
        "main.do": (main, "text/html; charset=utf-8", "https://dart.fss.or.kr/dsaf001/main.do?rcpNo=" + rcp),
        "eleId=9": (b"<meta charset='utf-8'><body>first tree declared section sufficient</body>", "text/html; charset=utf-8", "https://dart.fss.or.kr/report/viewer.do?rcpNo=" + rcp + "&dcmNo=11577485&eleId=9&offset=0&length=10&dtd=HTML"),
        "eleId=2": (b"<meta charset='utf-8'><body>second tree declared section sufficient</body>", "text/html; charset=utf-8", "https://dart.fss.or.kr/report/viewer.do?rcpNo=" + rcp + "&dcmNo=11577485&eleId=2&offset=10&length=10&dtd=HTML"),
    }))
    assert [(x["ele_id"], x["toc_no"]) for x in packet["sections"]] == [("9", "1"), ("2", "2")]


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
    packet_dir = checkpoint.parent
    assert source.completed_packet(checkpoint, rcp) is not None
    copied = tmp_path / "other" / f"{rcp}.json"; copied.parent.mkdir(); copied.write_bytes(checkpoint.read_bytes())
    assert source.completed_packet(copied, rcp) is None
    metadata = json.loads(checkpoint.read_text())
    raw = packet_dir / f"{rcp}.viewer.0000.raw"; raw.write_bytes(b"corrupt")
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
    packet_dir = checkpoint.parent
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
    escaped = tmp_path / "escaped.raw"; escaped.write_bytes((packet_dir / f"{rcp}.viewer.0000.raw").read_bytes())
    (packet_dir / f"{rcp}.viewer.0000.raw").unlink(); (packet_dir / f"{rcp}.viewer.0000.raw").symlink_to(escaped)
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
    assert (checkpoint.parent / f"{rcp}.viewer.txt").read_bytes().decode("utf-8") == packet["text"]


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


def synthetic_packet():
    def fetch(url):
        return (main_page() if 'main.do' in url else b'<body>complete substantive disclosure source body</body>'), 'text/html; charset=utf-8', url
    return source.source_packet('20260911800823', fetch)


def test_v3_generations_preserve_history_and_retry(tmp_path):
    directory = tmp_path / '20260911'; directory.mkdir()
    history = {name: b'historical immutable receipt' for name in ('20260911800823.json', '20260911800823.viewer.txt', '20260911800823.main.raw')}
    for name, content in history.items(): (directory / name).write_bytes(content)
    first = source.write_packet(synthetic_packet(), directory)
    before = {p: p.read_bytes() for p in first.parent.iterdir()}
    second = source.write_packet(synthetic_packet(), directory)
    assert first.parent != second.parent and first.parent.name.startswith('v3-')
    assert all(p.read_bytes() == content for p, content in before.items())
    assert all((directory / name).read_bytes() == content for name, content in history.items())


def test_interrupted_publish_leaves_no_visible_partial_generation(tmp_path, monkeypatch):
    directory = tmp_path / '20260911'
    def interrupt(*args): raise OSError('interrupted publish')
    with monkeypatch.context() as patch:
        patch.setattr(source.os, 'rename', interrupt)
        with pytest.raises(OSError, match='interrupted'): source.write_packet(synthetic_packet(), directory)
    assert not list(directory.glob('v3-*'))
    assert source.completed_packet(source.write_packet(synthetic_packet(), directory), '20260911800823')


def test_generation_collision_never_overwrites_published_files(tmp_path, monkeypatch):
    monkeypatch.setattr(source.uuid, 'uuid4', lambda: type('Id', (), {'hex': 'a' * 32})())
    first = source.write_packet(synthetic_packet(), tmp_path / '20260911')
    before = {p: p.read_bytes() for p in first.parent.iterdir()}
    with pytest.raises((FileExistsError, source.SourceError)):
        source.write_packet(synthetic_packet(), tmp_path / '20260911')
    assert all(p.read_bytes() == content for p, content in before.items())


def test_arbitrary_tree_variable_names_and_push_order():
    fields = {'rcpNo': '20260911800823', 'dcmNo': '123', 'offset': '0', 'length': '10', 'dtd': 'HTML', 'tocNo': '1', 'atocId': '1'}
    chunks = []
    for name, ele in [('node2', '8'), ('section17', '2')]:
        chunks.append(f'var {name} = {{}};' + ''.join(f'{name}.{key} = "{value}";' for key, value in (fields | {'eleId': ele}).items()))
    sections = source.declared_viewer_sections(''.join(chunks) + 'treeData.push(section17); treeData.push(node2);', fields['rcpNo'])
    assert [s['ele_id'] for s in sections] == ['2', '8']


@pytest.mark.parametrize('tree', ['var treeData = [BROKEN];', 'var node2 = {}; node2["rcpNo"] = "20260911800823"; treeData.push(node2);', 'treeData.push(unknown);', '$().jstree({data: BROKEN});'])
def test_malformed_authoritative_tree_never_uses_decoy(tree):
    with pytest.raises(source.SourceError, match='tree'):
        source.declared_viewer_sections(tree + main_page().decode(), '20260911800823')


def test_snapshot_retains_each_file_once_and_rejects_symlinks_and_drift(tmp_path, monkeypatch):
    path = source.write_packet(synthetic_packet(), tmp_path / '20260911')
    opened = []
    original = source.os.open
    def record(name, flags, *args, **kwargs):
        opened.append(str(name))
        return original(name, flags, *args, **kwargs)
    monkeypatch.setattr(source.os, 'open', record)
    with source.RetainedPacketSnapshot(path, trusted_root=tmp_path) as snapshot:
        metadata = source.completed_packet_snapshot(path, '20260911800823', snapshot.packet_bytes, snapshot.read_sibling)
        assert metadata
        text_path = pathlib.Path(metadata['text_path'])
        assert snapshot.read_sibling(text_path) == synthetic_packet()['text'].encode()
        assert opened.count(text_path.name) == 1
        text_path.write_bytes(b'replaced')
        with pytest.raises(ValueError, match='drift'): snapshot.verify()
    text_path.unlink(); text_path.symlink_to(path)
    assert source.completed_packet(path, '20260911800823', trusted_root=tmp_path) is None


def test_v2_historical_first_section_stays_readable(tmp_path):
    packet = synthetic_packet()
    rcp = packet['rcp_no']; directory = tmp_path / rcp[:8]; directory.mkdir()
    metadata = {k:v for k,v in packet.items() if not k.startswith('_') and k not in {'text', 'sections'}}
    metadata['schema_version'] = 'giraffe-dart-source-packet-v2'
    for field, suffix, content in [('raw_path', 'viewer.raw', packet['_sections'][0]['_raw']), ('text_path', 'viewer.txt', packet['text'].encode()), ('main_raw_path', 'main.raw', packet['_main_raw'])]:
        target = directory / f'{rcp}.{suffix}'; target.write_bytes(content); metadata[field] = str(target)
    path = directory / f'{rcp}.json'; path.write_text(json.dumps(metadata))
    before = {p: p.read_bytes() for p in directory.iterdir()}
    assert source.completed_packet(path, rcp)
    source.write_packet(synthetic_packet(), directory)
    assert source.completed_packet(path, rcp)
    assert all(p.read_bytes() == data for p, data in before.items())


def test_interruption_during_stage_file_fsync_never_publishes(tmp_path, monkeypatch):
    directory = tmp_path / '20260911'
    def fail(_fd): raise OSError('fsync interrupted')
    with monkeypatch.context() as patch:
        patch.setattr(source.os, 'fsync', fail)
        with pytest.raises(OSError, match='fsync interrupted'):
            source.write_packet(synthetic_packet(), directory)
    assert list(directory.iterdir()) == []
    assert source.completed_packet(source.write_packet(synthetic_packet(), directory), '20260911800823')


def test_malformed_tree_initializer_rejected_even_with_valid_push():
    rcp = '20260911800823'
    fields = {'rcpNo': rcp, 'dcmNo': '123', 'eleId': '1', 'offset': '0', 'length': '10', 'dtd': 'HTML', 'tocNo': '1', 'atocId': '1'}
    tree = 'var treeData = [BROKEN]; var node2 = {};' + ''.join(f'node2.{key} = "{value}";' for key, value in fields.items()) + 'treeData.push(node2);'
    with pytest.raises(source.SourceError, match='tree'):
        source.declared_viewer_sections(tree, rcp)


def test_snapshot_rederives_section_text_from_retained_raw(tmp_path):
    path = source.write_packet(synthetic_packet(), tmp_path / '20260911')
    metadata = json.loads(path.read_bytes())
    section = metadata['sections'][0]
    forged = '<body>forged substituted text with sufficient body</body>'
    forged_bytes = forged.encode()
    digest = source.hashlib.sha256(forged_bytes).hexdigest()
    pathlib.Path(section['text_path']).write_bytes(forged_bytes)
    pathlib.Path(metadata['text_path']).write_bytes(forged_bytes)
    for target in (metadata, section):
        target.update(text_sha256=digest, text_chars=len(forged), visible_chars=len('forged substituted text with sufficient body'))
    path.write_text(json.dumps(metadata))
    assert source.completed_packet(path, '20260911800823') is None


def test_truncated_node_declaration_without_push_never_falls_back():
    tree = 'var node2 = {}; node2["rcpNo"] = "20260911800823";'
    with pytest.raises(source.SourceError, match='tree'):
        source.declared_viewer_sections(tree + main_page().decode(), '20260911800823')


@pytest.mark.parametrize('truncated', ['var node2 = {}; node2.rcpNo = "20260911800823";', 'var node1 = {}; node1.rcpNo = "20260911800823"; var node1 = {};'])
def test_valid_tree_prefix_cannot_hide_unpushed_receipt_node(truncated):
    fields = {'rcpNo': '20260911800823', 'dcmNo': '123', 'eleId': '1', 'offset': '0', 'length': '10', 'dtd': 'HTML', 'tocNo': '1', 'atocId': '1'}
    valid = 'var node1 = {};' + ''.join(f'node1.{key} = "{value}";' for key, value in fields.items()) + 'treeData.push(node1);'
    with pytest.raises(source.SourceError, match='tree'):
        source.declared_viewer_sections(valid + truncated + main_page().decode(), fields['rcpNo'])


def test_new_control_directory_is_fsynced_in_parent(tmp_path, monkeypatch):
    synced = []
    original = source._fsync_directory
    def record(path):
        synced.append(path)
        original(path)
    monkeypatch.setattr(source, '_fsync_directory', record)
    source.write_packet(synthetic_packet(), tmp_path / '20260911')
    assert tmp_path in synced


def test_interruption_after_rename_retains_complete_generation_on_retry(tmp_path, monkeypatch):
    directory = tmp_path / '20260911'; directory.mkdir()
    original = source._fsync_directory
    def interrupt_after_rename(path):
        if path == directory:
            raise OSError('post rename interrupted')
        original(path)
    with monkeypatch.context() as patch:
        patch.setattr(source, '_fsync_directory', interrupt_after_rename)
        with pytest.raises(OSError, match='post rename interrupted'):
            source.write_packet(synthetic_packet(), directory)
    first, = directory.glob('v3-*/20260911800823.json')
    assert source.completed_packet(first, '20260911800823')
    before = {p: p.read_bytes() for p in first.parent.iterdir()}
    second = source.write_packet(synthetic_packet(), directory)
    assert second.parent != first.parent
    assert all(p.read_bytes() == data for p, data in before.items())
    assert source.completed_packet(second, '20260911800823')
