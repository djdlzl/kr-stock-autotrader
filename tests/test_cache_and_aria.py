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


def test_auth_switch_is_a_pressed_button_group_not_false_tabs():
    html = TestClient(app).get("/").text
    assert 'role="group"' in html
    assert 'aria-label="인증 방식"' in html
    assert 'id="login-tab" aria-pressed="true"' in html
    assert 'id="signup-tab" aria-pressed="false"' in html
    assert "setAttribute('aria-pressed'" in html
    for forbidden in ('role="tablist"', 'role="tab"', 'aria-controls="login-panel"', 'aria-controls="signup-panel"'):
        assert forbidden not in html


def test_plan_stepper_is_step_navigation_not_false_tabs():
    client = TestClient(app)
    assert client.post(
        "/api/signup", json={"email": "aria-stepper@test.com", "password": "long-password"}
    ).status_code == 200
    html = client.get("/app").text
    assert '<nav class="stepper" aria-label="계획 작성 단계">' in html
    assert 'data-step="0" aria-current="step"' in html
    assert "setAttribute('aria-current'" in html
    assert 'role="tablist"' not in html
    assert 'role="tab"' not in html
    assert 'aria-controls="' not in html


def test_primary_and_success_tokens_meet_aa_and_plan_interactions_validate():
    assert contrast_ratio("#1f1d1b", "#ff6f0f") >= 4.5
    assert contrast_ratio("#126b49", "#e9f8f0") >= 4.5
    client = TestClient(app)
    assert client.post(
        "/api/signup", json={"email": "form-accessibility@test.com", "password": "long-password"}
    ).status_code == 200
    html = client.get("/app").text
    assert ".primary{border:0;background:var(--primary);color:var(--ink)}" in html
    assert "backdrop-filter" not in html
    assert "<form id=\"plan-form\">" in html
    assert "panel.checkValidity()" in html and "panel.reportValidity()" in html
    assert "e.currentTarget.checkValidity()" in html and "e.currentTarget.reportValidity()" in html
    assert "matchMedia('(prefers-reduced-motion: reduce)').matches?'auto':'smooth'" in html
    assert " — " not in html
