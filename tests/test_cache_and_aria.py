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


def test_auth_shell_is_login_only_without_signup_controls_or_code():
    html = TestClient(app).get("/").text
    assert html.count('type="submit"') == 1
    assert "fetch('/api/login'" in html
    for forbidden in ("회원가입", "signup", "login-tab", "signup-tab", "role=\"group\"", "role=\"tablist\"", "role=\"tab\"", "aria-controls=\"login-panel\"", "aria-controls=\"signup-panel\""):
        assert forbidden not in html


def test_decision_card_dashboard_has_scrollable_tabs_and_no_legacy_execution_ctas():
    client = TestClient(app)
    assert client.post("/api/signup", json={"email":"aria-stepper@test.com","password":"long-password"}).status_code == 200
    html = client.get("/app").text
    assert 'aria-label="카드 필터"' in html and 'overflow-x:auto' in html
    assert "새 매수 계획" not in html and "시세 입력" not in html

def test_decision_card_accessibility_controls_and_mobile_rules():
    client = TestClient(app)
    assert client.post("/api/signup", json={"email":"form-accessibility@test.com","password":"long-password"}).status_code == 200
    html = client.get("/app").text
    for marker in ("min-height:44px", ":focus-visible", "overflow-x:hidden", "@media(max-width:390px)", "prefers-reduced-motion"):
        assert marker in html

def test_decision_card_ui_uses_safe_korean_formatting_and_no_raw_json():
    from kr_stock_autotrader.ui import APP_HTML
    assert 'function korean' in APP_HTML and '해당 없음' in APP_HTML and 'innerHTML=JSON.stringify' not in APP_HTML

def test_decision_card_ui_has_order_type_select_and_kst_conversion():
    from kr_stock_autotrader.ui import APP_HTML
    assert 'name="order_type"' in APP_HTML and 'function isoKst' in APP_HTML and 'function localValue' in APP_HTML

def test_decision_card_ui_has_order_draft_and_explicit_close_confirmation():
    from kr_stock_autotrader.ui import APP_HTML
    assert '근거 무효화 조건' in APP_HTML and '수동 전량 매도' in APP_HTML and 'confirm(' in APP_HTML
