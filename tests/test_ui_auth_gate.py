"""Auth-gate and source-level UX regression coverage."""
from fastapi.testclient import TestClient

from app import app


def test_unauthenticated_root_is_auth_shell_without_private_controls():
    response = TestClient(app).get("/")
    assert response.status_code == 200
    html = response.text
    for expected in ("모의투자 계획을 시작하세요", "로그인", "회원가입", "role=\"group\""):
        assert expected in html
    for forbidden in ("내 계획", "시세 입력", "새 매수 계획", "감사 로그", "디버그 JSON"):
        assert forbidden not in html
    assert TestClient(app).get("/app", follow_redirects=False).status_code == 303


def test_signup_login_logout_navigation_and_json_contracts():
    client = TestClient(app)
    signup = client.post("/api/signup", json={"email": "ux-gate@test.com", "password": "long-password"})
    assert signup.status_code == 200 and signup.json() == {"ok": True}
    assert client.get("/", follow_redirects=False).headers["location"] == "/app"
    app_page = client.get("/app")
    assert app_page.status_code == 200
    for expected in ("새 매수 계획", "시세 입력", "내 계획", "예약 대기", "로그아웃"):
        assert expected in app_page.text
    logout = client.post("/api/logout")
    assert logout.status_code == 200 and logout.json() == {"ok": True}
    assert client.get("/app", follow_redirects=False).headers["location"] == "/"
    login = client.post("/api/login", json={"email": "ux-gate@test.com", "password": "long-password"})
    assert login.status_code == 200 and login.json() == {"ok": True}


def test_app_shell_source_has_responsive_accessibility_and_plan_controls():
    client = TestClient(app)
    assert client.post("/api/signup", json={"email": "ux-smoke@test.com", "password": "long-password"}).status_code == 200
    html = client.get("/app").text
    for required in (
        "--primary:#ff6f0f", "--primary-low", "min-height:44px", ":focus-visible",
        "@media(max-width:768px)", "@media(max-width:390px)", "prefers-reduced-motion",
        "status-grid", "condition-row", "cancel", "조건 삭제", "confirm(",
        "data-panel=\"create\"", "data-panel=\"tick\"", "세션이 만료되어 로그인 화면으로 이동",
    ):
        assert required in html
    assert "디버그 JSON" not in html
