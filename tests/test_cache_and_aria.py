"""Privacy-cache and truthful ARIA regression coverage."""
from fastapi.testclient import TestClient

from app import app
from kr_stock_autotrader.api import merge_vary


PRIVATE_CACHE_CONTROL = "no-store, private"


def contrast_ratio(foreground: str, background: str) -> float:
    def luminance(color: str) -> float:
        channels = [int(color[index:index + 2], 16) / 255 for index in (1, 3, 5)]
        linear = [value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4 for value in channels]
        return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]

    lighter, darker = sorted((luminance(foreground), luminance(background)), reverse=True)
    return (lighter + 0.05) / (darker + 0.05)


def assert_private_response(response):
    assert response.headers["cache-control"] == PRIVATE_CACHE_CONTROL
    assert "cookie" in {token.strip().lower() for token in response.headers["vary"].split(",")}


def test_vary_merge_preserves_existing_tokens_without_duplicate_cookie():
    assert merge_vary("Accept-Encoding, cookie", "Cookie") == "Accept-Encoding, cookie"
    assert merge_vary("Accept-Encoding", "Cookie") == "Accept-Encoding, Cookie"


def test_session_sensitive_routes_and_auth_posts_are_private_and_vary_by_cookie():
    anonymous = TestClient(app)
    assert_private_response(anonymous.get("/"))
    assert_private_response(anonymous.get("/app", follow_redirects=False))
    assert_private_response(anonymous.get("/api/plans"))

    signup = anonymous.post(
        "/api/signup", json={"email": "cache-owner@test.com", "password": "long-password"}
    )
    assert signup.status_code == 200
    assert_private_response(signup)
    assert_private_response(anonymous.get("/", follow_redirects=False))
    assert_private_response(anonymous.get("/app"))
    assert_private_response(anonymous.get("/api/plans"))
    assert_private_response(anonymous.post("/api/logout"))


def test_enabled_auth_shell_has_signup_mode_without_tab_panels():
    html = TestClient(app).get("/").text
    assert html.count('type="submit"') == 1
    for required in ("회원가입", "'/api/login'", "'/api/signup'", "id=\"login-mode\"", "id=\"signup-mode\"", "role=\"group\""):
        assert required in html
    for forbidden in ("login-tab", "signup-tab", "role=\"tablist\"", "role=\"tab\"", "aria-controls=\"login-panel\"", "aria-controls=\"signup-panel\""):
        assert forbidden not in html


def test_release0_dashboard_is_change_first_and_has_no_execution_controls():
    client = TestClient(app)
    assert client.post("/api/signup", json={"email":"aria-stepper@test.com","password":"long-password"}).status_code == 200
    html = client.get("/app").text
    for required in ('투자 판단 오피스', '오늘 변경', '아직 미확인', 'role="dialog"', 'aria-modal="true"'):
        assert required in html
    for forbidden in ('새 매수 계획', '시세 입력', '매수 승인', '수동 매도'):
        assert forbidden not in html


def test_release0_accessibility_controls_and_mobile_rules():
    client = TestClient(app)
    assert client.post("/api/signup", json={"email":"form-accessibility@test.com","password":"long-password"}).status_code == 200
    html = client.get("/app").text
    for marker in ("min-height:44px", ":focus-visible", "@media(max-width:390px)", "prefers-reduced-motion", "visibleFocusable", "inert"):
        assert marker in html


def test_release0_ui_uses_safe_korean_formatting_and_no_raw_json():
    from kr_stock_autotrader.ui import APP_HTML
    assert 'function korean' in APP_HTML and '확인 필요' in APP_HTML and 'innerHTML=JSON.stringify' not in APP_HTML
