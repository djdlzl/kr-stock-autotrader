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


def test_0800_prompt_preserves_expected_price_input_object_for_0905_evaluation():
    prompt = (Path(__file__).parents[1] / "prompts/giraffe-decision-card-scheduler-v1.md").read_text()
    assert "expected_price_inputs" in prompt
    assert "drop/rename/stringify하지 않고 보존" in prompt


def test_0800_prompt_does_not_fabricate_observation_anchors_without_complete_market_inputs():
    prompt = (Path(__file__).parents[1] / "prompts/giraffe-decision-card-scheduler-v1.md").read_text()
    for marker in ("`post_close_market`", "`pre_event_low`", "`pre_event_close`", "`event_window_high`", "모두 있을 때만", "만들지 않는다"):
        assert marker in prompt


def test_research_latest_ignores_newer_same_day_generic_run():
    import os, tempfile
    os.environ.setdefault("DATABASE_PATH", tempfile.mktemp(suffix=".db"))
    os.environ.setdefault("INTERNAL_API_KEY", "test-key")
    os.environ.setdefault("SESSION_SECRET", "test-session-secret-that-is-at-least-thirty-two-bytes-long")
    from fastapi.testclient import TestClient
    from app import app
    headers = {"X-Internal-API-Key": os.environ["INTERNAL_API_KEY"]}
    client = TestClient(app)
    generic = "research-2026-09-15-manual"
    assert client.post(f"/api/internal/scheduler-runs/{generic}/start", json={"kind":"research"}, headers=headers).status_code == 200
    assert client.post(f"/api/internal/scheduler-runs/{generic}/finish", json={"status":"done","count":0,"detail":{"legacy":True}}, headers=headers).status_code == 200
    # No canonical validated 07:00 run exists, so the generic record cannot satisfy 08:00.
    assert client.get("/api/internal/scheduler-runs/latest?kind=research&date=2026-09-15", headers=headers).status_code == 404
