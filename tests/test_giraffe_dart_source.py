import importlib.util
import pathlib
import sys

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
    packet = source.source_packet(rcp, fetch_from({"main.do": (main_page(rcp), "text/html; charset=utf-8", "https://dart.fss.or.kr/dsaf001/main.do?rcpNo=" + rcp), "viewer.do": (viewer.encode(charset), "text/html; charset=" + charset, "https://dart.fss.or.kr/report/viewer.do?rcpNo=" + rcp + "&dcmNo=11577485")}))
    assert packet["source_valid"] is True and packet["charset"].lower() == charset


def test_utf8_and_meta_fallback_are_valid():
    rcp = "20260911800823"
    viewer = "<html><meta charset='utf-8'><body>UTF8 DART disclosure body is long enough.</body></html>"
    packet = source.source_packet(rcp, fetch_from({"main.do": (main_page(rcp), None, "https://dart.fss.or.kr/dsaf001/main.do?rcpNo=" + rcp), "viewer.do": (viewer.encode(), None, "https://dart.fss.or.kr/report/viewer.do?rcpNo=" + rcp + "&dcmNo=11577485")}))
    assert packet["charset"] == "utf-8"

@pytest.mark.parametrize("viewer,content_type,code", [
    (b"<html><body>\xff\xfe\xff</body></html>", "text/html; charset=utf-8", "DECODE"),
    ("<html><meta charset='utf-8'><body>占쏙옙 document body longer enough</body></html>".encode(), None, "DECODE"),
    (b"<html><meta charset='utf-8'><body>login please now with enough body</body></html>", None, "EXTRACT"),
])
def test_wrong_charset_mojibake_and_login_are_typed_failures(viewer, content_type, code):
    rcp = "20260911800823"
    with pytest.raises(source.SourceError) as exc:
        source.source_packet(rcp, fetch_from({"main.do": (main_page(rcp), "text/html; charset=utf-8", "https://dart.fss.or.kr/dsaf001/main.do?rcpNo=" + rcp), "viewer.do": (viewer, content_type, "https://dart.fss.or.kr/report/viewer.do?rcpNo=" + rcp + "&dcmNo=11577485")}))
    assert exc.value.code == code


def test_missing_dcm_and_wrong_final_url_fail_closed():
    with pytest.raises(source.SourceError, match="EXTRACT"):
        source.canonical_viewer_url("<html></html>", "20260911800823")
    with pytest.raises(source.SourceError, match="SOURCE_FETCH"):
        source.validate_viewer("valid document body with enough content", "https://dart.fss.or.kr/report/viewer.do?rcpNo=20260911800823", "https://evil.example/viewer", "20260911800823")
