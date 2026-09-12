import importlib.util
import json
import pathlib
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

ROOT = pathlib.Path(__file__).parents[1]
spec = importlib.util.spec_from_file_location("dart_source", ROOT / "scripts" / "giraffe_dart_source.py")
source = importlib.util.module_from_spec(spec)
assert spec and spec.loader
sys.modules["dart_source"] = source
spec.loader.exec_module(source)


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
