"""Production Compose must carry all mandatory Giraffe thresholds."""
import json
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
THRESHOLDS = {
    "GIRAFFE_MIN_TRADING_VALUE_KRW": "1000",
    "GIRAFFE_MIN_MARKET_CAP_KRW": "2000000000",
    "GIRAFFE_MAX_MARKET_CAP_KRW": "3000000000",
    "GIRAFFE_MAX_RECENT_RISE_PCT": "30",
    "GIRAFFE_MAX_GAP_PCT": "10",
    "GIRAFFE_FILTER_CONFIG_VERSION": "giraffe-premarket-filter-v2-short-term-priced-in",
    "GIRAFFE_SHORT_TERM_RISE_SESSIONS": "2",
    "GIRAFFE_MAX_SHORT_TERM_EXCESS_RISE_PCT": "10",
    "GIRAFFE_MAX_PRE_RETURN_PCT": "30",
}
REQUIRED_RUNTIME_ENV = {
    "SESSION_SECRET": "test-session-secret-that-is-at-least-thirty-two-bytes-long",
    "INTERNAL_API_KEY": "test-internal-api-key",
    "KIS_APP_KEY": "test-kis-app-key",
    "KIS_APP_SECRET": "test-kis-app-secret",
    "KIS_ACCOUNT_NO": "12345678",
}


def compose_config(env):
    return subprocess.run(
        ["docker", "compose", "-f", "compose.prod.yml", "config", "--format", "json"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
    )


def fixture_env():
    env = os.environ.copy()
    for name in THRESHOLDS:
        env.pop(name, None)
    return env | REQUIRED_RUNTIME_ENV | THRESHOLDS


def test_production_compose_renders_all_mandatory_giraffe_thresholds():
    result = compose_config(fixture_env())
    assert result.returncode == 0, result.stderr
    environment = json.loads(result.stdout)["services"]["app"]["environment"]
    assert {name: environment.get(name) for name in THRESHOLDS} == THRESHOLDS
    assert environment["LIVE_TRADING"] == "false"


@pytest.mark.parametrize("missing", THRESHOLDS)
def test_production_compose_fails_closed_when_a_giraffe_threshold_is_missing(missing):
    env = fixture_env()
    env.pop(missing)
    result = compose_config(env)
    assert result.returncode != 0
    assert missing in result.stderr
