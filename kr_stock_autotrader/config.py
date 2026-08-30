"""Runtime configuration; the application is permanently paper-only."""
import os
from pathlib import Path

DATABASE_PATH = Path(os.getenv("DATABASE_PATH", "autotrader.db"))
SESSION_SECRET = os.getenv("SESSION_SECRET", "")
MIN_SESSION_SECRET_BYTES = 32
SESSION_CLOCK_SKEW_SECONDS = 30
if SESSION_SECRET == "dev-only-change-me" or len(SESSION_SECRET.encode()) < MIN_SESSION_SECRET_BYTES:
    raise RuntimeError(
        "SESSION_SECRET must be set to a non-public secret of at least 32 bytes; refusing to start."
    )
SESSION_TTL_SECONDS = int(os.getenv("SESSION_TTL_SECONDS", "28800"))
COOKIE_SECURE = os.getenv("COOKIE_SECURE", "false").lower() == "true"
# Production is single-user by default. Tests/dev must explicitly opt in.
SIGNUP_ENABLED = os.getenv("SIGNUP_ENABLED", "false").lower() == "true"
KR_HOLIDAYS = frozenset(x.strip() for x in os.getenv("KR_HOLIDAYS", "").split(",") if x.strip())
LIVE_TRADING = False
