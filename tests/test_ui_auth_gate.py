"""Auth-gate and source-level UX regression coverage."""
from fastapi.testclient import TestClient

from app import app


def test_unauthenticated_root_exposes_temporary_signup_when_enabled_without_private_controls():
    response = TestClient(app).get("/")
    assert response.status_code == 200
    html = response.text
    for expected in (
        "모의투자 계획을 시작하세요", "로그인", "회원가입", "id=\"login-mode\"",
        "id=\"signup-mode\"", "role=\"group\"", "autocomplete=\"current-password\"",
        "'/api/signup'", "new-password", "min-height:44px",
        "Private Investment Office", "판단의 근거와 빈칸을 한 화면에서 관리하세요",
        "--graphite:#161b25", "--cobalt:#2457d6", "prefers-reduced-motion",
    ):
        assert expected in html
    for forbidden in ("내 계획", "시세 입력", "새 매수 계획", "감사 로그", "디버그 JSON"):
        assert forbidden not in html
    assert html.count('type="submit"') == 1
    assert "'/api/login'" in html
    assert TestClient(app).get("/app", follow_redirects=False).status_code == 303


def test_signup_login_logout_navigation_and_json_contracts():
    client = TestClient(app)
    signup = client.post("/api/signup", json={"email": "ux-gate@test.com", "password": "long-password"})
    assert signup.status_code == 200 and signup.json() == {"ok": True}
    duplicate = client.post("/api/signup", json={"email": "ux-gate@test.com", "password": "long-password"})
    assert duplicate.status_code == 409 and duplicate.json()["detail"] == "이미 가입된 이메일입니다"
    short_password = TestClient(app).post("/api/signup", json={"email": "short@test.com", "password": "short"})
    assert short_password.status_code == 422 and short_password.json()["detail"] == "입력 내용을 확인하세요"
    assert client.get("/", follow_redirects=False).headers["location"] == "/app"
    app_page = client.get("/app")
    assert app_page.status_code == 200
    for expected in ("투자 판단 오피스", "판단 상세", "로그아웃"):
        assert expected in app_page.text
    logout = client.post("/api/logout")
    assert logout.status_code == 200 and logout.json() == {"ok": True}
    assert client.get("/app", follow_redirects=False).headers["location"] == "/"
    login = client.post("/api/login", json={"email": "ux-gate@test.com", "password": "long-password"})
    assert login.status_code == 200 and login.json() == {"ok": True}


def test_app_shell_source_has_responsive_accessibility_and_record_only_controls():
    client = TestClient(app)
    assert client.post("/api/signup", json={"email": "ux-smoke@test.com", "password": "long-password"}).status_code == 200
    html = client.get("/app").text
    for required in (
        "--graphite:#161b25", "--porcelain:#f4f6f8", "--cobalt:#2457d6", "min-height:44px", ":focus-visible",
        "@media(max-width:390px)", "prefers-reduced-motion", "role=\"dialog\"", "aria-modal=\"true\"",
        "세션이 만료되었습니다", "현재 단계: 기록 전용", "주문 기능 없음", "투자 판단 오피스",
        "카드 생성", "판단 보류", "카드 미생성", "Provenance · 원문과 변경 이력",
    ):
        assert required in html
    for forbidden in ("디버그 JSON", "새 매수 계획", "매수 승인", "수동 매도"):
        assert forbidden not in html
