"""07:00 research completion gate contracts for the 08:00 card scheduler."""
import json
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


def test_research_latest_selects_highest_versioned_same_day_run_not_newest_row(monkeypatch, tmp_path):
    import os
    from fastapi.testclient import TestClient
    import kr_stock_autotrader.db as db_module
    from kr_stock_autotrader.db import connect
    from app import app

    monkeypatch.setattr(db_module, "DATABASE_PATH", str(tmp_path / "latest-research.db"))
    db = connect()
    try:
        # Insert a newer generic/manual row last: date lookup must use the
        # closed canonical grammar and numeric rerun order, not row recency.
        for key in (
            "research-2026-09-17-0700-kst",
            "research-2026-09-17-0700-kst-r2",
            "research-2026-09-17-0700-kst-r10",
            "research-2026-09-17-manual",
            "research-2026-09-17-0700-kst-r0",
            "research-2026-09-17-0700-kst-manual",
        ):
            db.execute(
                "INSERT INTO scheduler_runs(run_key,kind,status,started_at,detail) VALUES(?,?,?,?,?)",
                (key, "research", "error", "2026-09-17T07:00:00+09:00", json.dumps({"key": key})),
            )
        db.commit()
    finally:
        db.close()

    headers = {"X-Internal-API-Key": os.environ["INTERNAL_API_KEY"]}
    response = TestClient(app).get(
        "/api/internal/scheduler-runs/latest?kind=research&date=2026-09-17", headers=headers
    )
    assert response.status_code == 200
    assert response.json()["run_key"] == "research-2026-09-17-0700-kst-r10"


def test_07_prompts_define_contract_hash_as_canonical_parsed_json_only():
    root = Path(__file__).parents[1]
    required = "canonical JSON of parsed `control_contract`"
    assert required in (root / "prompts/giraffe-material-discovery-v1.md").read_text(encoding="utf-8")
    assert required in (root / "ops/giraffe-cron-07-prompt.txt").read_text(encoding="utf-8")
