"""Deterministic secure runtime configuration for the test process."""
import os
import tempfile

# Config intentionally fails closed during import, so this must exist before app imports.
os.environ.setdefault("SESSION_SECRET", "test-session-secret-that-is-at-least-thirty-two-bytes-long")
os.environ.setdefault("DATABASE_PATH", tempfile.mktemp(suffix=".db"))
