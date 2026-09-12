"""Executable 08:00 KRX admission gate tests."""
from datetime import datetime
import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]


@pytest.fixture
def gate():
    spec = importlib.util.spec_from_file_location("giraffe_decision_card_gate", ROOT / "scripts" / "giraffe_decision_card_gate.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("closed", ["2026-05-01T08:00:00+09:00", "2026-07-17T08:00:00+09:00"])
def test_closed_day_08_gate_returns_silent_noop_before_command(gate, closed):
    calls = []
    assert gate.execute(now=datetime.fromisoformat(closed), run=lambda: calls.append("side-effect")) == ""
    assert calls == []


def test_open_day_08_gate_runs_only_after_calendar_admission(gate):
    calls = []
    assert gate.execute(now=datetime.fromisoformat("2026-07-20T08:00:00+09:00"), run=lambda: calls.append("side-effect")) == "08 admission complete"
    assert calls == ["side-effect"]


def test_08_gate_fails_closed_for_unavailable_calendar_before_command(gate, monkeypatch):
    calls = []
    monkeypatch.setattr(gate, "is_krx_business_date", lambda _: (_ for _ in ()).throw(gate.CalendarError("bad calendar")))
    with pytest.raises(gate.AdmissionError, match="calendar admission failed"):
        gate.execute(now=datetime.fromisoformat("2027-01-04T08:00:00+09:00"), run=lambda: calls.append("side-effect"))
    assert calls == []
