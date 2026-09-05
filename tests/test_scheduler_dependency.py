"""07:00 research completion gate contracts for the 08:00 card scheduler."""
from pathlib import Path


def test_cli_scheduler_latest_builds_exact_readonly_date_query(monkeypatch, capsys):
    from kr_stock_autotrader import cli

    seen = {}
    monkeypatch.setattr(
        cli,
        "call",
        lambda method, path, payload=None: seen.update(method=method, path=path, payload=payload)
        or {"run_key": "research-2026-09-05-0700-kst", "status": "done", "count": 0},
    )

    assert cli.main(["scheduler-latest", "research", "--date", "2026-09-05"]) == 0
    assert seen == {
        "method": "GET",
        "path": "/api/internal/scheduler-runs/latest?kind=research&date=2026-09-05",
        "payload": None,
    }
    assert '"status": "done"' in capsys.readouterr().out


def test_0800_prompt_requires_same_day_completed_research_before_pending_cards():
    prompt = (Path(__file__).parents[1] / "prompts/giraffe-decision-card-scheduler-v1.md").read_text()
    latest = 'scheduler-latest research --date YYYY-MM-DD'
    assert latest in prompt
    assert "same-day latest `research` run" in prompt
    assert "`status=done`" in prompt
    assert "count=0" in prompt
    assert "missing/error/not done" in prompt
    assert "scheduler-finish ... error" in prompt
    assert prompt.index(latest) < prompt.index("pending-cards")
