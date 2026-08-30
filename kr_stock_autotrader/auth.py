"""Password and signed, expiring session helpers."""
import base64, hashlib, hmac, json, os
from datetime import timedelta
from fastapi import HTTPException, Request
from .config import SESSION_CLOCK_SKEW_SECONDS, SESSION_SECRET, SESSION_TTL_SECONDS
from .domain import now_kst


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1)
    return base64.b64encode(salt + digest).decode()


def verify_password(password: str, encoded: str) -> bool:
    try:
        raw = base64.b64decode(encoded)
        return hmac.compare_digest(raw[16:], hashlib.scrypt(password.encode(), salt=raw[:16], n=2**14, r=8, p=1))
    except (ValueError, TypeError):
        return False


def issue_session(user_id: int) -> str:
    issued = int(now_kst().timestamp())
    body = base64.urlsafe_b64encode(json.dumps({"uid": user_id, "iat": issued, "exp": issued + SESSION_TTL_SECONDS}, separators=(",", ":")).encode()).decode()
    signature = hmac.new(SESSION_SECRET.encode(), body.encode(), hashlib.sha256).hexdigest()
    return f"{body}.{signature}"


def current_user(request: Request) -> int:
    try:
        body, signature = request.cookies.get("session", "").rsplit(".", 1)
        expected = hmac.new(SESSION_SECRET.encode(), body.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise ValueError
        data = json.loads(base64.urlsafe_b64decode(body))
        uid, issued_at, expires_at = data["uid"], data["iat"], data["exp"]
        # Exact integers exclude bool and strings: session claims are never coerced.
        if any(type(value) is not int for value in (uid, issued_at, expires_at)):
            raise ValueError
        now = int(now_kst().timestamp())
        if issued_at > now + SESSION_CLOCK_SKEW_SECONDS or issued_at > expires_at or now > expires_at:
            raise ValueError
        return uid
    except (ValueError, KeyError, TypeError, json.JSONDecodeError):
        raise HTTPException(401, "로그인이 필요합니다")


def csrf_origin_ok(request: Request) -> None:
    """Absent Origin is supported for TestClient and documented CLI/API clients."""
    origin = request.headers.get("origin")
    if origin and origin.rstrip("/") != f"{request.url.scheme}://{request.headers.get('host')}".rstrip("/"):
        raise HTTPException(403, "다른 출처의 요청은 허용되지 않습니다")
