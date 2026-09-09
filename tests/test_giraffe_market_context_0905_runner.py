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


def install_network(monkeypatch, runner, *, ids=(101, 202), missing_key=None, failed_card=None, starts=None):
    calls = []
    starts = starts if starts is not None else []

    def fake_urlopen(request, timeout):
        path = request.full_url.replace("http://giraffe.test", "")
        method = request.get_method()
        payload = json.loads(request.data) if request.data else None
        calls.append((method, path, payload, dict(request.header_items())))
        if path.startswith("/api/internal/scheduler-runs/latest?"):
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
            return Response({"run_key": key, "card_id": card_id, "requested_as_of": "2026-09-09T09:05:00+09:00"})
        if path.endswith("/finish"):
            return Response({"status": payload["status"]})
        raise AssertionError(path)

    monkeypatch.setattr(runner, "urlopen", fake_urlopen)
    return calls


def test_success_uses_authoritative_cards_and_exact_readbacks(runner, env_file, monkeypatch, capsys):
    calls = install_network(monkeypatch, runner)
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
    assert calls[-1][2]["status"] == "done"


def test_missing_card_run_finishes_aggregate_error(runner, env_file, monkeypatch, capsys):
    missing = "market-context-2026-09-09-0905-kst-topic7923-card-202"
    calls = install_network(monkeypatch, runner, missing_key=missing)
    assert runner.main(["--source-topic", "telegram:mac:7923", "--env-file", str(env_file)], now=now()) == 1
    assert "stage=cards count=1 reasons=http_404" in capsys.readouterr().out
    assert calls[-1][1].endswith("/finish")
    assert calls[-1][2]["status"] == "error"
    assert calls[-1][2]["count"] == 1


def test_partial_failure_finishes_aggregate_error(runner, env_file, monkeypatch, capsys):
    calls = install_network(monkeypatch, runner, failed_card=202)
    assert runner.main(["--source-topic", "telegram:mac:7923", "--env-file", str(env_file)], now=now()) == 1
    assert "stage=cards count=1 reasons=http_409" in capsys.readouterr().out
    assert calls[-1][2]["status"] == "error"


def test_idempotent_rerun_reuses_deterministic_keys(runner, env_file, monkeypatch, capsys):
    starts = []
    calls = install_network(monkeypatch, runner, ids=(101,), starts=starts)
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
