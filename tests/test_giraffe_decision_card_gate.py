"""Hermes-compatible 08:00 KRX admission prehook tests."""
import contextlib
from datetime import datetime
import importlib.util
import io
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]


def parse_hermes_wake_gate(script_output: str) -> bool:
    """Exact scheduler_prompt._parse_wake_gate stdout contract."""
    lines = [line for line in script_output.splitlines() if line.strip()]
    if not lines:
        return True
    try:
        gate = json.loads(lines[-1].strip())
    except (json.JSONDecodeError, ValueError):
        return True
    return not isinstance(gate, dict) or gate.get("wakeAgent", True) is not False


@pytest.fixture
def gate():
    spec = importlib.util.spec_from_file_location("giraffe_decision_card_gate", ROOT / "scripts" / "giraffe_decision_card_gate.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("closed", ["2026-05-01T08:00:00+09:00", "2026-07-17T08:00:00+09:00"])
def test_closed_day_08_prehook_suppresses_hermes_wake_before_command(gate, closed):
    calls = []
    payload = gate.evaluate(now=datetime.fromisoformat(closed), run=lambda: calls.append("side-effect"))
    output = "diagnostic\n" + json.dumps(payload) + "\n"
    assert payload["wakeAgent"] is False
    assert parse_hermes_wake_gate(output) is False
    assert calls == []


def test_open_day_08_prehook_wakes_hermes_without_a_wrapper_command(gate):
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        assert gate.main(["--at", "2026-07-20T08:00:00+09:00"]) == 0
    payload = json.loads(output.getvalue())
    assert payload["wakeAgent"] is True
    assert parse_hermes_wake_gate(output.getvalue()) is True


def test_open_day_08_optional_recovery_command_runs_after_admission(gate):
    calls = []
    payload = gate.evaluate(now=datetime.fromisoformat("2026-07-20T08:00:00+09:00"), run=lambda: calls.append("side-effect"))
    assert payload["wakeAgent"] is True
    assert calls == ["side-effect"]


@pytest.mark.parametrize(
    ("argv", "error"),
    [
        (["--at", "2027-01-04T08:00:00+09:00"], "calendar admission failed"),
        (["--at", "not-a-timestamp"], "invalid KST timestamp"),
    ],
)
def test_08_prehook_fails_nonzero_for_unsupported_or_malformed_calendar_input(gate, argv, error):
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        assert gate.main(argv) == 2
    payload = json.loads(output.getvalue())
    assert payload["complete"] is False
    assert payload["error"] == error
