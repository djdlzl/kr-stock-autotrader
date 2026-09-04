"""Deterministic secure runtime configuration for the test process."""
import os
import tempfile
import pytest

os.environ.setdefault("SESSION_SECRET", "test-session-secret-that-is-at-least-thirty-two-bytes-long")
os.environ.setdefault("DATABASE_PATH", tempfile.mktemp(suffix=".db"))
os.environ.setdefault("SIGNUP_ENABLED", "true")

@pytest.fixture(autouse=True)
def _isolate_database_path(tmp_path, monkeypatch):
    """No test may restore a mutable DB module path to an unwritable production path."""
    from kr_stock_autotrader import db as dbmod
    monkeypatch.setattr(dbmod, "DATABASE_PATH", str(tmp_path / "test.db"))
