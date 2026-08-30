"""Runtime configuration; the application is permanently paper-only."""
import os
from pathlib import Path

DATABASE_PATH = Path(os.getenv("DATABASE_PATH", "autotrader.db"))
SESSION_SECRET = os.getenv("SESSION_SECRET", "dev-only-change-me")
SESSION_TTL_SECONDS = int(os.getenv("SESSION_TTL_SECONDS", "28800"))
COOKIE_SECURE = os.getenv("COOKIE_SECURE", "false").lower() == "true"
KR_HOLIDAYS = frozenset(x.strip() for x in os.getenv("KR_HOLIDAYS", "").split(",") if x.strip())
LIVE_TRADING = False
