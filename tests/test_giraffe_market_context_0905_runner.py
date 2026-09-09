import importlib.util
import json
from datetime import datetime
from pathlib import Path

import pytest


SCRIPT = Path(__file__).parents[1] / "scripts" / "giraffe_market_context_0905.py"


@pytest.fixture()
def runner():
    spec = importlib.util.spec_from_file_location("market_context_runner", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def env_file(tmp_path):
    path = tmp_path / ".env"
    path.write_text("GIRAFFE_URL=http://giraffe.test\nINTERNAL_API_KEY=secret-do-not-print\n")
    return path


def now():
    return datetime.fromisoformat("2026-09-09T09:05:17+09:00")


def card_latest(ids=(101, 202)):
    return {"status": "done", "detail": {"detail": {"cards": {"ids": list(ids)}}}}


class Response:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self, *_):
        return json.dumps(self.payload).encode()


def install_network(monkeypatch, runner, *, ids=(101, 202), missing_key=None, failed_card=None, starts=None,
                    latest_payloads=None, readback_source_topic="mac:7923", fail_finish=False):
    calls = []
    starts = starts if starts is not None else []
    latest_payloads = list(latest_payloads or [])

    def fake_urlopen(request, timeout):
        path = request.full_url.replace("http://giraffe.test", "")
        method = request.get_method()
        payload = json.loads(request.data) if request.data else None
        calls.append((method, path, payload, dict(request.header_items())))
        if path.startswith("/api/internal/scheduler-runs/latest?"):
            if latest_payloads:
                return Response(latest_payloads.pop(0))
            return Response(card_latest(ids))
        if path.endswith("/start"):
            starts.append(payload)
            return Response({"run_key": path.split("/")[-2], "kind": "market_context", "status": "done" if len(starts) > 1 else "started", "idempotent": len(starts) > 1})
        if "/cards/" in path and path.endswith("/market-context"):
            card_id = int(path.split("/")[4])
            if card_id == failed_card:
                from urllib.error import HTTPError
                raise HTTPError(request.full_url, 409, "blocked", None, None)
            return Response({"run_key": payload["run_key"]})
        if "/market-context-runs/" in path:
            key = path.rsplit("/", 1)[1]
            if key == missing_key:
                from urllib.error import HTTPError
                raise HTTPError(request.full_url, 404, "missing", None, None)
            card_id = int(key.rsplit("-", 1)[1])
            return Response({"run_key": key, "card_id": card_id, "requested_as_of": "2026-09-09T09:05:00+09:00", "source_topic": readback_source_topic})
        if path.endswith("/finish"):
            if fail_finish:
                from urllib.error import HTTPError
                raise HTTPError(request.full_url, 503, "unavailable", None, None)
            return Response({"status": payload["status"]})
        raise AssertionError(path)

    monkeypatch.setattr(runner, "urlopen", fake_urlopen)
    return calls


def test_success_uses_authoritative_cards_and_exact_readbacks(runner, env_file, monkeypatch, capsys):
    aggregate_key = "market-context-2026-09-09-0905-kst-topic7923"
    calls = install_network(monkeypatch, runner, latest_payloads=[
        card_latest(),
        {"run_key": aggregate_key, "status": "done", "detail": {"count": 2}},
    ])
    assert runner.main(["--source-topic", "telegram:mac:7923", "--env-file", str(env_file)], now=now()) == 0
    assert capsys.readouterr().out.strip() == "시장맥락 완료 count=2"
    posted = [(path, payload) for method, path, payload, _ in calls if method == "POST" and "/cards/" in path]
    assert posted == [
        ("/api/internal/cards/101/market-context", {"run_key": "market-context-2026-09-09-0905-kst-topic7923-card-101", "as_of": "2026-09-09T09:05:00+09:00"}),
        ("/api/internal/cards/202/market-context", {"run_key": "market-context-2026-09-09-0905-kst-topic7923-card-202", "as_of": "2026-09-09T09:05:00+09:00"}),
    ]
    assert [path for _, path, _, _ in calls if "/market-context-runs/" in path] == [
        "/api/internal/market-context-runs/market-context-2026-09-09-0905-kst-topic7923-card-101",
        "/api/internal/market-context-runs/market-context-2026-09-09-0905-kst-topic7923-card-202",
    ]
    assert calls[-2][2]["status"] == "done"
    assert calls[-1][1] == "/api/internal/scheduler-runs/latest?kind=market_context&date=2026-09-09"
    card_steps = [(method, path) for method, path, _, _ in calls if "/cards/" in path or "/market-context-runs/" in path]
    assert card_steps == [
        ("POST", "/api/internal/cards/101/market-context"),
        ("GET", "/api/internal/market-context-runs/market-context-2026-09-09-0905-kst-topic7923-card-101"),
        ("POST", "/api/internal/cards/202/market-context"),
        ("GET", "/api/internal/market-context-runs/market-context-2026-09-09-0905-kst-topic7923-card-202"),
    ]


def test_card_readback_requires_api_canonical_source_topic(runner, env_file, monkeypatch, capsys):
    aggregate_key = "market-context-2026-09-09-0905-kst-topic7923"
    calls = install_network(monkeypatch, runner, ids=(101,), readback_source_topic="telegram:mac:7923", latest_payloads=[
        card_latest((101,)),
        {"run_key": aggregate_key, "status": "error", "detail": {"count": 0}},
    ])
    assert runner.main(["--source-topic", "telegram:mac:7923", "--env-file", str(env_file)], now=now()) == 1
    assert "card_101_mismatch" in capsys.readouterr().out
    assert calls[-1][1] == "/api/internal/scheduler-runs/latest?kind=market_context&date=2026-09-09"


def test_missing_card_run_finishes_aggregate_error(runner, env_file, monkeypatch, capsys):
    missing = "market-context-2026-09-09-0905-kst-topic7923-card-202"
    aggregate_key = "market-context-2026-09-09-0905-kst-topic7923"
    calls = install_network(monkeypatch, runner, missing_key=missing, latest_payloads=[
        card_latest(),
        {"run_key": aggregate_key, "status": "error", "detail": {"count": 1}},
    ])
    assert runner.main(["--source-topic", "telegram:mac:7923", "--env-file", str(env_file)], now=now()) == 1
    assert "stage=cards count=1 reasons=http_404" in capsys.readouterr().out
    assert calls[-2][1].endswith("/finish")
    assert calls[-2][2]["status"] == "error"
    assert calls[-2][2]["count"] == 1
    assert calls[-1][1] == "/api/internal/scheduler-runs/latest?kind=market_context&date=2026-09-09"


def test_partial_failure_finishes_aggregate_error(runner, env_file, monkeypatch, capsys):
    aggregate_key = "market-context-2026-09-09-0905-kst-topic7923"
    calls = install_network(monkeypatch, runner, failed_card=202, latest_payloads=[
        card_latest(),
        {"run_key": aggregate_key, "status": "error", "detail": {"count": 1}},
    ])
    assert runner.main(["--source-topic", "telegram:mac:7923", "--env-file", str(env_file)], now=now()) == 1
    assert "stage=cards count=1 reasons=http_409" in capsys.readouterr().out
    assert calls[-2][2]["status"] == "error"


def test_error_terminalization_failure_is_reported(runner, env_file, monkeypatch, capsys):
    calls = install_network(monkeypatch, runner, failed_card=101, fail_finish=True)
    assert runner.main(["--source-topic", "telegram:mac:7923", "--env-file", str(env_file)], now=now()) == 1
    assert "error_terminalization_failed:api_http_503" in capsys.readouterr().out
    assert calls[-1][1].endswith("/finish")


def test_error_terminalization_readback_mismatch_is_reported(runner, env_file, monkeypatch, capsys):
    calls = install_network(monkeypatch, runner, ids=(101,), failed_card=101, latest_payloads=[
        card_latest((101,)), {"run_key": "wrong", "status": "error", "detail": {"count": 0}},
    ])
    assert runner.main(["--source-topic", "telegram:mac:7923", "--env-file", str(env_file)], now=now()) == 1
    assert "error_terminalization_failed:readback_mismatch" in capsys.readouterr().out
    assert calls[-1][1] == "/api/internal/scheduler-runs/latest?kind=market_context&date=2026-09-09"


@pytest.mark.parametrize("current", [100.0, 100.5, 101.0])
def test_request_budget_does_not_start_calls_when_expired_or_insufficient(runner, monkeypatch, current):
    calls = []
    monkeypatch.setattr(runner, "urlopen", lambda *args, **kwargs: calls.append((args, kwargs)))
    budget = runner.RequestBudget(deadline=101.0, monotonic=lambda: current, minimum_seconds=1.0)
    with pytest.raises(runner.RunFailure, match="deadline_insufficient"):
        runner.api({"GIRAFFE_URL": "http://giraffe.test", "INTERNAL_API_KEY": "secret"}, "GET", "/safe", budget=budget)
    assert calls == []


def test_request_budget_caps_urlopen_timeout_to_remaining_time(runner, monkeypatch):
    calls = []
    monkeypatch.setattr(runner, "urlopen", lambda request, timeout: calls.append(timeout) or Response({"ok": True}))
    budget = runner.RequestBudget(deadline=105.0, monotonic=lambda: 102.5, minimum_seconds=1.0)
    assert runner.api({"GIRAFFE_URL": "http://giraffe.test", "INTERNAL_API_KEY": "secret"}, "GET", "/safe", budget=budget) == {"ok": True}
    assert calls == [2.5]


def test_execute_does_not_start_network_when_0906_deadline_budget_is_insufficient(runner, monkeypatch):
    calls = []
    monkeypatch.setattr(runner, "urlopen", lambda *args, **kwargs: calls.append((args, kwargs)))
    near_boundary = datetime.fromisoformat("2026-09-09T09:05:59.500000+09:00")
    with pytest.raises(runner.RunFailure, match="deadline_insufficient"):
        runner.execute(env={"GIRAFFE_URL": "http://giraffe.test", "INTERNAL_API_KEY": "secret"}, now=near_boundary,
                       source_topic="telegram:mac:7923", preflight=False, monotonic=lambda: 100.0)
    assert calls == []


def test_idempotent_rerun_reuses_deterministic_keys(runner, env_file, monkeypatch, capsys):
    starts = []
    aggregate_key = "market-context-2026-09-09-0905-kst-topic7923"
    calls = install_network(monkeypatch, runner, ids=(101,), starts=starts, latest_payloads=[
        card_latest((101,)), {"run_key": aggregate_key, "status": "done", "detail": {"count": 1}},
        card_latest((101,)), {"run_key": aggregate_key, "status": "done", "detail": {"count": 1}},
    ])
    args = ["--source-topic", "telegram:mac:7923", "--env-file", str(env_file)]
    assert runner.main(args, now=now()) == 0
    assert runner.main(args, now=now()) == 0
    assert len(starts) == 2
    assert {payload["kind"] for payload in starts} == {"market_context"}
    card_posts = [payload for method, path, payload, _ in calls if method == "POST" and "/cards/" in path]
    assert [payload["run_key"] for payload in card_posts] == [
        "market-context-2026-09-09-0905-kst-topic7923-card-101",
        "market-context-2026-09-09-0905-kst-topic7923-card-101",
    ]
    assert capsys.readouterr().out.count("시장맥락 완료 count=1") == 2


def test_secret_redaction_and_topic_fail_closed(runner, env_file, capsys):
    assert runner.main(["--source-topic", "telegram:mac:other", "--env-file", str(env_file)], now=now()) == 1
    output = capsys.readouterr().out
    assert "source_topic_not_allowed" in output
    assert "secret-do-not-print" not in output
    assert "INTERNAL_API_KEY" not in output


def test_preflight_outside_window_makes_no_network_requests(runner, env_file, monkeypatch, capsys):
    monkeypatch.setattr(runner, "urlopen", lambda *_: pytest.fail("preflight must not call API"))
    outside = datetime.fromisoformat("2026-09-09T08:00:00+09:00")
    assert runner.main(["--env-file", str(env_file), "--preflight"], now=outside) == 0
    assert capsys.readouterr().out.strip() == "시장맥락 사전점검 완료"
