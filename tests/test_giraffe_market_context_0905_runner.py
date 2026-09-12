import importlib.util
import json
import sqlite3
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
                    latest_payloads=None, readback_source_topic="mac:7923", expected_status="HOLD_MISSING_INPUT",
                    expected_status_by_card=None, expected_override=None, fail_finish=False):
    calls = []
    as_of_by_key = {}
    finished_payload = {}
    starts = starts if starts is not None else []
    latest_payloads = list(latest_payloads or [])

    def fake_urlopen(request, timeout):
        path = request.full_url.replace("http://giraffe.test", "")
        method = request.get_method()
        payload = json.loads(request.data) if request.data else None
        calls.append((method, path, payload, dict(request.header_items())))
        if path.startswith("/api/internal/scheduler-runs/latest?"):
            if latest_payloads:
                latest = latest_payloads.pop(0)
                if latest.get("status") == "done" and latest.get("detail") == {"count": len(ids)}:
                    latest["detail"] = finished_payload.copy()
                return Response(latest)
            return Response(card_latest(ids))
        if path.endswith("/start"):
            starts.append(payload)
            return Response({"run_key": path.split("/")[-2], "kind": "market_context", "status": "done" if len(starts) > 1 else "started", "idempotent": len(starts) > 1})
        if "/cards/" in path and path.endswith("/market-context"):
            assert isinstance(payload, dict)
            card_id = int(path.split("/")[4])
            if card_id == failed_card:
                from urllib.error import HTTPError
                raise HTTPError(request.full_url, 409, "blocked", None, None)
            as_of_by_key[payload["run_key"]] = payload["as_of"]
            return Response({"run_key": payload["run_key"]})
        if "/market-context-runs/" in path:
            key = path.rsplit("/", 1)[1]
            if key == missing_key or key not in as_of_by_key:
                from urllib.error import HTTPError
                raise HTTPError(request.full_url, 404, "missing", None, None)
            card_id = int(key.rsplit("-", 1)[1])
            status = (expected_status_by_card or {}).get(card_id, expected_status)
            expected = expected_override if expected_override is not None else {
                "run_key": key + "-expected-price", "card_id": card_id, "evidence_id": 10 + card_id,
                "filter_id": 20 + card_id, "requested_as_of": as_of_by_key[key],
                "source_topic": "mac:7923", "status": status,
                "result": {"status": status},
            }
            return Response({"run_key": key, "card_id": card_id, "evidence_id": 10 + card_id,
                             "filter_id": 20 + card_id, "requested_as_of": as_of_by_key[key],
                             "source_topic": readback_source_topic, "expected_price": expected})
        if path.endswith("/finish"):
            if fail_finish:
                from urllib.error import HTTPError
                raise HTTPError(request.full_url, 503, "unavailable", None, None)
            finished_payload.update(payload)
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
        ("/api/internal/cards/101/market-context", {"run_key": "market-context-2026-09-09-0905-kst-topic7923-card-101", "as_of": "2026-09-09T09:05:17+09:00"}),
        ("/api/internal/cards/202/market-context", {"run_key": "market-context-2026-09-09-0905-kst-topic7923-card-202", "as_of": "2026-09-09T09:05:17+09:00"}),
    ]
    assert [path for _, path, _, _ in calls if "/market-context-runs/" in path] == [
        "/api/internal/market-context-runs/market-context-2026-09-09-0905-kst-topic7923-card-101",
        "/api/internal/market-context-runs/market-context-2026-09-09-0905-kst-topic7923-card-101",
        "/api/internal/market-context-runs/market-context-2026-09-09-0905-kst-topic7923-card-202",
        "/api/internal/market-context-runs/market-context-2026-09-09-0905-kst-topic7923-card-202",
    ]
    assert calls[-2][2]["status"] == "done"
    assert calls[-1][1] == "/api/internal/scheduler-runs/latest?kind=market_context&date=2026-09-09"
    card_steps = [(method, path) for method, path, _, _ in calls if "/cards/" in path or "/market-context-runs/" in path]
    assert card_steps == [
        ("GET", "/api/internal/market-context-runs/market-context-2026-09-09-0905-kst-topic7923-card-101"),
        ("POST", "/api/internal/cards/101/market-context"),
        ("GET", "/api/internal/market-context-runs/market-context-2026-09-09-0905-kst-topic7923-card-101"),
        ("GET", "/api/internal/market-context-runs/market-context-2026-09-09-0905-kst-topic7923-card-202"),
        ("POST", "/api/internal/cards/202/market-context"),
        ("GET", "/api/internal/market-context-runs/market-context-2026-09-09-0905-kst-topic7923-card-202"),
    ]


def test_ordinary_58_second_cron_dispatch_has_the_full_operational_budget(runner, monkeypatch):
    """09:05:58 dispatch remains on-time; only 09:25 ends observation authority."""
    aggregate_key = "market-context-2026-09-09-0905-kst-topic7923"
    calls = install_network(monkeypatch, runner, ids=(101,), latest_payloads=[
        card_latest((101,)), {"run_key": aggregate_key, "status": "done", "detail": {"count": 1}},
    ])
    dispatched = datetime.fromisoformat("2026-09-09T09:05:58.462976+09:00")
    assert runner.execute(
        env={"GIRAFFE_URL": "http://giraffe.test", "INTERNAL_API_KEY": "secret"}, now=dispatched,
        source_topic="telegram:mac:7923", preflight=False, monotonic=lambda: 0.0,
    ) == "시장맥락 완료 count=1"
    posted = [payload for method, path, payload, _ in calls if method == "POST" and "/cards/" in path]
    assert posted == [{"run_key": "market-context-2026-09-09-0905-kst-topic7923-card-101",
                       "as_of": "2026-09-09T09:05:58.462976+09:00"}]


def test_cards_use_injected_actual_dispatch_times_and_aggregate_replays_them(runner, monkeypatch):
    aggregate_key = "market-context-2026-09-09-0905-kst-topic7923"
    calls = install_network(monkeypatch, runner, latest_payloads=[
        card_latest(), {"run_key": aggregate_key, "status": "done", "detail": {"count": 2}},
    ])
    observations = iter((
        datetime.fromisoformat("2026-09-09T09:05:17.123456+09:00"),
        datetime.fromisoformat("2026-09-09T09:24:59.999999+09:00"),
    ))
    assert runner.execute(
        env={"GIRAFFE_URL": "http://giraffe.test", "INTERNAL_API_KEY": "secret"}, now=now(),
        source_topic="telegram:mac:7923", preflight=False, monotonic=lambda: 0.0,
        wall_clock=lambda: next(observations),
    ) == "시장맥락 완료 count=2"
    posted = [payload for method, path, payload, _ in calls if method == "POST" and "/cards/" in path]
    assert [payload["as_of"] for payload in posted] == [
        "2026-09-09T09:05:17.123456+09:00", "2026-09-09T09:24:59.999999+09:00",
    ]
    finish = [payload for method, path, payload, _ in calls if method == "POST" and path.endswith("/finish")][-1]
    assert finish["detail"]["cards"]["observation_as_of"] == {
        "101": "2026-09-09T09:05:17.123456+09:00", "202": "2026-09-09T09:24:59.999999+09:00",
    }


def test_late_card_dispatch_at_0925_terminalizes_without_posting_card(runner, monkeypatch):
    aggregate_key = "market-context-2026-09-09-0905-kst-topic7923"
    calls = install_network(monkeypatch, runner, ids=(101,), latest_payloads=[
        card_latest((101,)), {"run_key": aggregate_key, "status": "error", "detail": {"count": 0}},
    ])
    with pytest.raises(runner.RunFailure, match="observation_outside_0905_0925_kst_window"):
        runner.execute(
            env={"GIRAFFE_URL": "http://giraffe.test", "INTERNAL_API_KEY": "secret"}, now=now(),
            source_topic="telegram:mac:7923", preflight=False, monotonic=lambda: 0.0,
            wall_clock=lambda: datetime.fromisoformat("2026-09-09T09:25:00+09:00"),
        )
    assert not [path for method, path, _, _ in calls if method == "POST" and "/cards/" in path]


def test_exact_authoritative_empty_cards_terminalize_done_zero_with_readback(runner, env_file, monkeypatch, capsys):
    aggregate_key = "market-context-2026-09-09-0905-kst-topic7923"
    calls = install_network(monkeypatch, runner, ids=(), latest_payloads=[
        card_latest(()), {"run_key": aggregate_key, "status": "done", "detail": {"count": 0}},
    ])

    assert runner.main(["--source-topic", "telegram:mac:7923", "--env-file", str(env_file)], now=now()) == 0
    assert capsys.readouterr().out.strip() == "시장맥락 완료 count=0"
    assert [(method, path) for method, path, _, _ in calls if "/cards/" in path or "/market-context-runs/" in path] == []
    finishes = [payload for method, path, payload, _ in calls if method == "POST" and path.endswith("/finish")]
    assert finishes == [{"status": "done", "count": 0, "detail": {"cards": {"ids": [], "observation_as_of": {}}}}]
    assert calls[-1][1] == "/api/internal/scheduler-runs/latest?kind=market_context&date=2026-09-09"


@pytest.mark.parametrize("run,reason", [
    ({}, "authoritative_card_ids_missing"),
    ({"detail": {"detail": {"cards": {}}}}, "authoritative_card_ids_missing"),
    ({"detail": {"detail": {"cards": {"ids": "[]"}}}}, "authoritative_card_ids_invalid"),
    ({"detail": {"detail": {"cards": {"ids": None}}}}, "authoritative_card_ids_invalid"),
    ({"detail": {"detail": {"cards": {"ids": [101, "202"]}}}}, "authoritative_card_ids_invalid"),
    ({"detail": {"detail": {"cards": {"ids": [0]}}}}, "authoritative_card_ids_invalid"),
    ({"detail": {"detail": {"cards": {"ids": [-1]}}}}, "authoritative_card_ids_invalid"),
    ({"detail": {"detail": {"cards": {"ids": [101, 101]}}}}, "authoritative_card_ids_duplicate"),
])
def test_card_authority_rejects_absent_or_malformed_contract(runner, run, reason):
    with pytest.raises(runner.RunFailure, match=reason):
        runner.card_ids(run)


@pytest.mark.parametrize("status", ["HOLD_MISSING_INPUT", "HOLD_INVALID_INPUT", "COMPUTED"])
def test_expected_price_terminal_statuses_complete_with_preflight_and_readback(runner, env_file, monkeypatch, capsys, status):
    aggregate_key = "market-context-2026-09-09-0905-kst-topic7923"
    calls = install_network(monkeypatch, runner, ids=(101,), expected_status=status, latest_payloads=[
        card_latest((101,)), {"run_key": aggregate_key, "status": "done", "detail": {"count": 1}},
    ])
    assert runner.main(["--source-topic", "telegram:mac:7923", "--env-file", str(env_file)], now=now()) == 0
    assert capsys.readouterr().out.strip() == "시장맥락 완료 count=1"
    assert [(method, path) for method, path, _, _ in calls if "/cards/" in path or "/market-context-runs/" in path] == [
        ("GET", "/api/internal/market-context-runs/market-context-2026-09-09-0905-kst-topic7923-card-101"),
        ("POST", "/api/internal/cards/101/market-context"),
        ("GET", "/api/internal/market-context-runs/market-context-2026-09-09-0905-kst-topic7923-card-101"),
    ]


def test_mixed_computed_and_hold_expected_prices_complete_with_preflight_and_readback(runner, env_file, monkeypatch, capsys):
    aggregate_key = "market-context-2026-09-09-0905-kst-topic7923"
    calls = install_network(monkeypatch, runner, expected_status_by_card={101: "COMPUTED", 202: "HOLD_MISSING_INPUT"}, latest_payloads=[
        card_latest(), {"run_key": aggregate_key, "status": "done", "detail": {"count": 2}},
    ])
    assert runner.main(["--source-topic", "telegram:mac:7923", "--env-file", str(env_file)], now=now()) == 0
    assert capsys.readouterr().out.strip() == "시장맥락 완료 count=2"
    assert [(method, path) for method, path, _, _ in calls if "/cards/" in path or "/market-context-runs/" in path] == [
        ("GET", "/api/internal/market-context-runs/market-context-2026-09-09-0905-kst-topic7923-card-101"),
        ("POST", "/api/internal/cards/101/market-context"),
        ("GET", "/api/internal/market-context-runs/market-context-2026-09-09-0905-kst-topic7923-card-101"),
        ("GET", "/api/internal/market-context-runs/market-context-2026-09-09-0905-kst-topic7923-card-202"),
        ("POST", "/api/internal/cards/202/market-context"),
        ("GET", "/api/internal/market-context-runs/market-context-2026-09-09-0905-kst-topic7923-card-202"),
    ]


def test_expected_price_failure_or_lineage_mismatch_terminalizes_aggregate(runner, env_file, monkeypatch, capsys):
    aggregate_key = "market-context-2026-09-09-0905-kst-topic7923"
    calls = install_network(monkeypatch, runner, ids=(101,), expected_override={
        "run_key": "wrong", "card_id": 101, "evidence_id": 111, "filter_id": 121,
        "requested_as_of": "2026-09-09T09:05:53+09:00", "source_topic": "mac:7923",
        "status": "CALCULATION_ERROR", "result": {"status": "CALCULATION_ERROR"},
    }, latest_payloads=[
        card_latest((101,)), {"run_key": aggregate_key, "status": "error", "detail": {"count": 0}},
    ])
    assert runner.main(["--source-topic", "telegram:mac:7923", "--env-file", str(env_file)], now=now()) == 1
    assert "card_101_expected_price_mismatch" in capsys.readouterr().out
    assert calls[-2][2]["status"] == "error"
    assert [(method, path) for method, path, _, _ in calls if "/cards/" in path or "/market-context-runs/" in path] == [
        ("GET", "/api/internal/market-context-runs/market-context-2026-09-09-0905-kst-topic7923-card-101"),
        ("POST", "/api/internal/cards/101/market-context"),
        ("GET", "/api/internal/market-context-runs/market-context-2026-09-09-0905-kst-topic7923-card-101"),
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


def test_request_budget_caps_urlopen_timeout_after_mandatory_request_slot(runner, monkeypatch):
    calls = []
    monkeypatch.setattr(runner, "urlopen", lambda request, timeout: calls.append(timeout) or Response({"ok": True}))
    budget = runner.RequestBudget(deadline=105.0, monotonic=lambda: 102.5, minimum_seconds=1.0)
    assert runner.api({"GIRAFFE_URL": "http://giraffe.test", "INTERNAL_API_KEY": "secret"}, "GET", "/safe", budget=budget) == {"ok": True}
    assert calls == [1.5]


def test_request_budget_reserves_finish_and_readback_slots(runner, monkeypatch):
    """The first request cannot consume the slots needed to finish and read back."""
    clock = [0.0]
    calls = []

    def fake_urlopen(request, timeout):
        calls.append((request.full_url, timeout))
        clock[0] += timeout
        return Response({"ok": True})

    monkeypatch.setattr(runner, "urlopen", fake_urlopen)
    budget = runner.RequestBudget(deadline=10.0, monotonic=lambda: clock[0], minimum_seconds=1.0)
    env = {"GIRAFFE_URL": "http://giraffe.test", "INTERNAL_API_KEY": "secret"}

    assert runner.api(env, "GET", "/work", budget=budget, reserve_slots=2) == {"ok": True}
    assert runner.api(env, "POST", "/finish", budget=budget, reserve_slots=1) == {"ok": True}
    assert runner.api(env, "GET", "/readback", budget=budget) == {"ok": True}
    assert calls == [
        ("http://giraffe.test/work", 7.0),
        ("http://giraffe.test/finish", 1.0),
        ("http://giraffe.test/readback", 1.0),
    ]


def test_execute_does_not_start_network_when_0925_deadline_budget_is_insufficient(runner, monkeypatch):
    calls = []
    monkeypatch.setattr(runner, "urlopen", lambda *args, **kwargs: calls.append((args, kwargs)))
    clock = iter((0.0, 1140.5))
    near_boundary = datetime.fromisoformat("2026-09-09T09:05:59.500000+09:00")
    with pytest.raises(runner.RunFailure, match="deadline_insufficient"):
        runner.execute(env={"GIRAFFE_URL": "http://giraffe.test", "INTERNAL_API_KEY": "secret"}, now=near_boundary,
                       source_topic="telegram:mac:7923", preflight=False, monotonic=lambda: next(clock))
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


def test_closed_day_returns_before_prompt_or_api(runner, monkeypatch):
    calls = []
    monkeypatch.setattr(runner, "check_prompt", lambda: calls.append("prompt"))
    monkeypatch.setattr(runner, "urlopen", lambda *_: calls.append("api"))
    closed = datetime.fromisoformat("2026-05-01T09:05:00+09:00")
    assert runner.execute(env={"GIRAFFE_URL": "http://giraffe.test", "INTERNAL_API_KEY": "secret"}, now=closed,
                          source_topic="telegram:mac:7923", preflight=False) == ""
    assert calls == []


def test_prompt_integrity_guard_fails_before_api(runner, monkeypatch, tmp_path):
    prompt = tmp_path / "scheduler-prompt.md"
    prompt.write_text("mutated", encoding="utf-8")
    monkeypatch.setattr(runner, "PROMPT_PATH", prompt)
    monkeypatch.setattr(runner, "urlopen", lambda *_: pytest.fail("prompt mismatch must precede API"))
    with pytest.raises(runner.RunFailure, match="prompt_sha256_mismatch"):
        runner.execute(env={"GIRAFFE_URL": "http://giraffe.test", "INTERNAL_API_KEY": "secret"}, now=now(),
                       source_topic="telegram:mac:7923", preflight=False)


def test_monotonic_mid_card_exhaustion_verifies_aggregate_error(runner, monkeypatch):
    """A card request cannot spend the two slots needed to terminalize."""
    clock = [0.0]
    aggregate: dict[str, str | None] = {"status": None}
    calls = []
    key = "market-context-2026-09-09-0905-kst-topic7923"

    def fake_urlopen(request, timeout):
        path = request.full_url.replace("http://giraffe.test", "")
        payload = json.loads(request.data) if request.data else None
        calls.append(path)
        if path.startswith("/api/internal/scheduler-runs/latest?"):
            if "kind=card" in path:
                return Response(card_latest((101,)))
            return Response({"run_key": key, "status": aggregate["status"], "detail": {"count": 0}})
        if path.endswith("/start"):
            aggregate["status"] = "started"
            clock[0] = 1144.0  # Only error finish + fresh latest remain.
            return Response({"run_key": key, "kind": "market_context", "status": "started"})
        if path.endswith("/finish"):
            aggregate["status"] = payload["status"]
            return Response({"status": payload["status"]})
        pytest.fail(path)

    monkeypatch.setattr(runner, "urlopen", fake_urlopen)
    near = datetime.fromisoformat("2026-09-09T09:05:53+09:00")
    with pytest.raises(runner.RunFailure, match="deadline_insufficient"):
        runner.execute(env={"GIRAFFE_URL": "http://giraffe.test", "INTERNAL_API_KEY": "secret"}, now=near,
                       source_topic="telegram:mac:7923", preflight=False, monotonic=lambda: clock[0])
    assert aggregate["status"] == "error"
    assert not any("/cards/" in path for path in calls)


def test_monotonic_done_needs_fresh_readback_or_never_leaves_started(runner, monkeypatch):
    """The done finish cannot consume the last slot reserved for its readback."""
    clock = [0.0]
    aggregate: dict[str, str | None] = {"status": None}
    key = "market-context-2026-09-09-0905-kst-topic7923"
    latest_calls = []

    def fake_urlopen(request, timeout):
        path = request.full_url.replace("http://giraffe.test", "")
        payload = json.loads(request.data) if request.data else None
        if path.startswith("/api/internal/scheduler-runs/latest?"):
            latest_calls.append(path)
            if "kind=card" in path:
                return Response(card_latest((101,)))
            return Response({"run_key": key, "status": aggregate["status"], "detail": {"count": 1, "detail": {"cards": {"ids": [101], "observation_as_of": {"101": "2026-09-09T09:05:53+09:00"}}}}})
        if path.endswith("/start"):
            aggregate["status"] = "started"
            return Response({"run_key": key, "kind": "market_context", "status": "started"})
        if "/cards/" in path:
            return Response({"run_key": payload["run_key"]})
        if "/market-context-runs/" in path:
            return Response({"run_key": path.rsplit("/", 1)[1], "card_id": 101,
                             "evidence_id": 111, "filter_id": 121,
                             "requested_as_of": "2026-09-09T09:05:53+09:00", "source_topic": "mac:7923",
                             "expected_price": {"run_key": path.rsplit("/", 1)[1] + "-expected-price", "card_id": 101,
                                                "evidence_id": 111, "filter_id": 121, "requested_as_of": "2026-09-09T09:05:53+09:00",
                                                "source_topic": "mac:7923", "status": "HOLD_MISSING_INPUT",
                                                "result": {"status": "HOLD_MISSING_INPUT"}}})
        if path.endswith("/finish"):
            aggregate["status"] = payload["status"]
            clock[0] = 1147.0  # Done was sent; its fresh readback has no slot.
            return Response({"status": payload["status"]})
        pytest.fail(path)

    monkeypatch.setattr(runner, "urlopen", fake_urlopen)
    near = datetime.fromisoformat("2026-09-09T09:05:53+09:00")
    with pytest.raises(runner.RunFailure, match="error_terminalization_failed:deadline_deadline_insufficient"):
        runner.execute(env={"GIRAFFE_URL": "http://giraffe.test", "INTERNAL_API_KEY": "secret"}, now=near,
                       source_topic="telegram:mac:7923", preflight=False, monotonic=lambda: clock[0])
    assert aggregate["status"] == "done"  # terminal, never silently left started
    assert len(latest_calls) == 1  # no unbudgeted post-done readback was started


def test_monotonic_slot_accounting_success_has_fresh_done_readback(runner, monkeypatch):
    """One-slot fake requests fit exactly only when every terminal slot survives."""
    clock = [0.0]
    aggregate: dict[str, str | None] = {"status": None}
    key = "market-context-2026-09-09-0905-kst-topic7923"
    market_latest = []

    def fake_urlopen(request, timeout):
        path = request.full_url.replace("http://giraffe.test", "")
        payload = json.loads(request.data) if request.data else {}
        clock[0] += 1.0
        if path.startswith("/api/internal/scheduler-runs/latest?"):
            if "kind=card" in path:
                return Response(card_latest((101,)))
            market_latest.append(aggregate["status"])
            return Response({"run_key": key, "status": aggregate["status"], "detail": {"count": 1, "detail": {"cards": {"ids": [101], "observation_as_of": {"101": "2026-09-09T09:05:53+09:00"}}}}})
        if path.endswith("/start"):
            aggregate["status"] = "started"
            return Response({"run_key": key, "kind": "market_context", "status": "started"})
        if "/cards/" in path:
            return Response({"run_key": payload["run_key"]})
        if "/market-context-runs/" in path:
            return Response({"run_key": path.rsplit("/", 1)[1], "card_id": 101,
                             "evidence_id": 111, "filter_id": 121,
                             "requested_as_of": "2026-09-09T09:05:53+09:00", "source_topic": "mac:7923",
                             "expected_price": {"run_key": path.rsplit("/", 1)[1] + "-expected-price", "card_id": 101,
                                                "evidence_id": 111, "filter_id": 121, "requested_as_of": "2026-09-09T09:05:53+09:00",
                                                "source_topic": "mac:7923", "status": "HOLD_MISSING_INPUT",
                                                "result": {"status": "HOLD_MISSING_INPUT"}}})
        if path.endswith("/finish"):
            aggregate["status"] = payload["status"]
            return Response({"status": payload["status"]})
        pytest.fail(path)

    monkeypatch.setattr(runner, "urlopen", fake_urlopen)
    near = datetime.fromisoformat("2026-09-09T09:05:53+09:00")
    assert runner.execute(env={"GIRAFFE_URL": "http://giraffe.test", "INTERNAL_API_KEY": "secret"}, now=near,
                          source_topic="telegram:mac:7923", preflight=False, monotonic=lambda: clock[0]) == "시장맥락 완료 count=1"
    assert aggregate["status"] == "done"
    assert market_latest == ["done"]


def test_ambiguous_start_is_recovered_to_verified_error(runner, monkeypatch):
    """A lost start response is checked by deterministic key and terminalized."""
    from urllib.error import URLError

    aggregate: dict[str, str | None] = {"status": None}
    key = "market-context-2026-09-09-0905-kst-topic7923"
    calls = []

    def fake_urlopen(request, timeout):
        path = request.full_url.replace("http://giraffe.test", "")
        payload = json.loads(request.data) if request.data else None
        calls.append(path)
        if path.startswith("/api/internal/scheduler-runs/latest?"):
            if "kind=card" in path:
                return Response(card_latest((101,)))
            return Response({"run_key": key, "status": aggregate["status"], "detail": {"count": 0}})
        if path.endswith("/start"):
            aggregate["status"] = "started"  # persisted before response loss
            raise URLError("lost response")
        if path.endswith("/finish"):
            aggregate["status"] = payload["status"]
            return Response({"status": payload["status"]})
        pytest.fail(path)

    monkeypatch.setattr(runner, "urlopen", fake_urlopen)
    with pytest.raises(runner.RunFailure, match="aggregate_start_request_failed"):
        runner.execute(env={"GIRAFFE_URL": "http://giraffe.test", "INTERNAL_API_KEY": "secret"}, now=now(),
                       source_topic="telegram:mac:7923", preflight=False, monotonic=lambda: 0.0)
    assert aggregate["status"] == "error"
    assert calls[-2].endswith("/finish")
    assert "kind=market_context" in calls[-1]


def test_monotonic_reserve_refuses_before_aggregate_start(runner, monkeypatch):
    """The prerequisite lookup leaves start ambiguity recovery slots intact."""
    calls = []
    monkeypatch.setattr(runner, "urlopen", lambda *args, **kwargs: calls.append(args))
    clock = iter((0.0, 1145.0))
    near = datetime.fromisoformat("2026-09-09T09:05:55+09:00")
    with pytest.raises(runner.RunFailure, match="deadline_insufficient"):
        runner.execute(env={"GIRAFFE_URL": "http://giraffe.test", "INTERNAL_API_KEY": "secret"}, now=near,
                       source_topic="telegram:mac:7923", preflight=False, monotonic=lambda: next(clock))
    assert calls == []


def test_manual_expected_price_uses_only_latest_same_day_card_run_and_reads_back_details(runner, monkeypatch, tmp_path):
    """Manual recovery must never widen its target to historical cards."""
    from kr_stock_autotrader import db as dbmod
    from kr_stock_autotrader.decision_cards import create_evidence, save_card, save_filter
    from tests.test_decision_card_invariants import card, raw

    db_path = tmp_path / "manual.sqlite"
    monkeypatch.setattr(dbmod, "DATABASE_PATH", str(db_path))
    db = dbmod.connect()
    known = "2026-09-09T08:00:00+09:00"
    cards = []
    for sequence in range(3):
        evidence = create_evidence(db, {
            "symbol": f"0059{sequence:02d}", "name": "fixture", "kind": "disclosure",
            "title": f"fixture-{sequence}", "summary": "fixture", "source": "dart",
            "source_url": "https://example.test", "announcement_at": known, "collected_at": known,
            "known_at": known, "snapshot": {"economic_terms": {}},
            "dedupe_key": f"manual-target-{sequence}",
        })
        filt = save_filter(db, evidence["id"], raw(announcement_at=known, market_data_known_at=known), known, known)
        cards.append(save_card(db, card(evidence["id"], filt["id"])))
    db.execute("INSERT INTO scheduler_runs(run_key,kind,status,started_at,finished_at,detail) VALUES(?,?,?,?,?,?)", (
        "card-2026-09-08", "card", "done", "2026-09-08T08:00:00+09:00", "2026-09-08T08:01:00+09:00",
        json.dumps({"detail": {"cards": {"ids": [cards[0]["id"]]}}}),
    ))
    db.execute("INSERT INTO scheduler_runs(run_key,kind,status,started_at,finished_at,detail) VALUES(?,?,?,?,?,?)", (
        "card-2026-09-09", "card", "done", known, "2026-09-09T08:01:00+09:00",
        json.dumps({"detail": {"cards": {"ids": [cards[1]["id"], cards[2]["id"]]}}}),
    ))
    db.commit()
    db.close()

    report = runner.manual_expected_price(db_path, tmp_path / "report.json", now=now())

    assert report["counts"] == {
        "target": 2, "computed": 0, "hold_missing_input": 2, "hold_invalid_input": 0,
        "calculation_error": 0, "persisted": 2, "readback": 2,
    }
    assert [item["card_id"] for item in report["results"]] == [cards[1]["id"], cards[2]["id"]]
    assert all(item["reason"] == "missing_persisted_valuation_inputs" for item in report["results"])
    assert all(item["missing_fields"] == ["economic_terms.expected_price_inputs"] for item in report["results"])
    assert all(item["invalid_fields"] == [] and item["calculated_value"] is None for item in report["results"])


@pytest.mark.parametrize("status,detail,reason", [
    ("started", '{"detail":{"cards":{"ids":[1]}}}', "same_day_card_run_not_done"),
    ("done", "not-json", "same_day_card_run_malformed"),
])
def test_manual_card_ids_fails_closed_for_nonterminal_or_malformed_scheduler_contract(runner, status, detail, reason):
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute("CREATE TABLE scheduler_runs (id INTEGER PRIMARY KEY, kind TEXT, status TEXT, started_at TEXT, finished_at TEXT, detail TEXT)")
    db.execute("INSERT INTO scheduler_runs(kind,status,started_at,finished_at,detail) VALUES(?,?,?,?,?)", (
        "card", status, "2026-09-09T08:00:00+09:00", "2026-09-09T08:01:00+09:00", detail,
    ))
    with pytest.raises(runner.RunFailure, match=reason):
        runner.manual_card_ids(db, date="2026-09-09")
    db.close()
