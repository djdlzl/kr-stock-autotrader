"""Hermes-compatible 08:00 KRX admission prehook tests."""
import contextlib
from datetime import datetime
import importlib.util
import io
import json
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).parents[1]


def _load_hermes_wake_parser():
    """Use the installed Hermes parser when present; retain its exact fallback contract."""
    scheduler_prompt = Path.home() / ".hermes" / "hermes-agent" / "cron" / "scheduler_prompt.py"
    if scheduler_prompt.is_file():
        spec = importlib.util.spec_from_file_location("installed_hermes_scheduler_prompt", scheduler_prompt)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module._parse_wake_gate

    def exact_contract(script_output: str) -> bool:
        lines = [line for line in script_output.splitlines() if line.strip()]
        if not lines:
            return True
        try:
            gate = json.loads(lines[-1].strip())
        except (json.JSONDecodeError, ValueError):
            return True
        return not isinstance(gate, dict) or gate.get("wakeAgent", True) is not False

    return exact_contract


parse_hermes_wake_gate = _load_hermes_wake_parser()


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
        (
            [
                "--at",
                "2026-07-20T08:00:00+09:00",
                "--",
                sys.executable,
                "-c",
                "import sys; sys.exit(9)",
            ],
            "returned non-zero exit status 9",
        ),
    ],
)
def test_08_prehook_errors_exit_2_and_suppress_actual_hermes_wake_gate(gate, argv, error):
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        assert gate.main(argv) == 2
    lines = [line for line in output.getvalue().splitlines() if line.strip()]
    payload = json.loads(lines[-1])
    assert payload["complete"] is False
    assert payload["wakeAgent"] is False
    assert error in payload["error"]
    assert parse_hermes_wake_gate(output.getvalue()) is False
