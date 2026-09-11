#!/usr/bin/env python3
"""09:05-started KST market-context runner; it has no trading operations."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from datetime import datetime, time as clock_time
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, NoReturn
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")
SOURCE_TOPIC = "telegram:mac:7923"
API_SOURCE_TOPIC = "mac:7923"
PROMPT_SHA256 = "f7a1c1f16e168577222295af43aa05ca5ceaa6a7fc3d20b6a116b222e6fc3a7c"
PROMPT_PATH = Path(__file__).resolve().parents[1] / "prompts" / "giraffe-market-context-scheduler-v1.md"
MIN_REQUEST_BUDGET_SECONDS = 1.0
MAX_REQUEST_TIMEOUT_SECONDS = 20.0
OBSERVATION_WINDOW_START = clock_time(9, 5)
OBSERVATION_WINDOW_END = clock_time(9, 25)


class RunFailure(Exception):
    def __init__(self, stage: str, reason: str, count: int = 0):
        self.stage, self.reason, self.count = stage, reason, count
        super().__init__(reason)


class RequestBudget:
    """Monotonic budget that prevents starting a request too close to 09:25."""

    def __init__(self, *, deadline: float, monotonic: Callable[[], float] = time.monotonic,
                 minimum_seconds: float = MIN_REQUEST_BUDGET_SECONDS):
        self.deadline = deadline
        self.monotonic = monotonic
        self.minimum_seconds = minimum_seconds

    def timeout(self, *, reserve_slots: int = 0) -> float:
        """Reserve whole minimum-duration request slots after this request.

        A request is not allowed to borrow the time needed for mandatory
        terminalization/readback requests.  The strict inequality keeps the
        existing rule that a request needs more than one full minimum slot.
        """
        if reserve_slots < 0:
            raise ValueError("reserve_slots must be non-negative")
        remaining = self.deadline - self.monotonic()
        if remaining <= self.minimum_seconds * (reserve_slots + 1):
            raise RunFailure("deadline", "deadline_insufficient")
        return min(MAX_REQUEST_TIMEOUT_SECONDS,
                   remaining - self.minimum_seconds * (reserve_slots + 1))


def load_env(path: Path) -> Dict[str, str]:
    """Read only the central env values this runner needs; never print them."""
    if not path.is_file():
        raise RunFailure("env", "central_env_missing")
    values: Dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key in {"GIRAFFE_URL", "INTERNAL_API_KEY"}:
            values[key] = value.strip().strip('"').strip("'")
    if not values.get("GIRAFFE_URL") or not values.get("INTERNAL_API_KEY"):
        raise RunFailure("env", "required_env_missing")
    return values


def check_prompt() -> None:
    try:
        actual = hashlib.sha256(PROMPT_PATH.read_bytes()).hexdigest()
    except OSError:
        raise RunFailure("prompt", "prompt_missing")
    if actual != PROMPT_SHA256:
        raise RunFailure("prompt", "prompt_sha256_mismatch")


def api(env: Dict[str, str], method: str, path: str, payload: Dict[str, Any] | None = None,
        *, budget: RequestBudget, reserve_slots: int = 0) -> Dict[str, Any]:
    body = None if payload is None else json.dumps(payload, separators=(",", ":")).encode("utf-8")
    request = Request(
        env["GIRAFFE_URL"].rstrip("/") + path,
        data=body,
        method=method,
        headers={"X-Internal-API-Key": env["INTERNAL_API_KEY"], "Content-Type": "application/json"},
    )
    try:
        with urlopen(request, timeout=budget.timeout(reserve_slots=reserve_slots)) as response:
            result = json.load(response)
    except HTTPError as exc:
        raise RunFailure("api", "http_%s" % exc.code)
    except (URLError, OSError, ValueError):
        raise RunFailure("api", "request_failed")
    if not isinstance(result, dict):
        raise RunFailure("api", "invalid_response")
    return result


def card_ids(run: Dict[str, Any]) -> list[int]:
    """Accept only the exact scheduler-owned card list, including its empty no-op."""
    try:
        ids = run["detail"]["detail"]["cards"]["ids"]
    except (KeyError, TypeError):
        raise RunFailure("cards", "authoritative_card_ids_missing")
    if not isinstance(ids, list):
        raise RunFailure("cards", "authoritative_card_ids_invalid")
    if any(type(item) is not int or item <= 0 for item in ids):
        raise RunFailure("cards", "authoritative_card_ids_invalid")
    if len(set(ids)) != len(ids):
        raise RunFailure("cards", "authoritative_card_ids_duplicate")
    return ids


def run_key(date: str) -> str:
    return "market-context-%s-0905-kst-topic7923" % date


def card_run_key(date: str, card_id: int) -> str:
    return "market-context-%s-0905-kst-topic7923-card-%s" % (date, card_id)


def expected_price_run_key(market_context_key: str) -> str:
    return "%s-expected-price" % market_context_key


EXPECTED_PRICE_TERMINAL_STATUSES = {"COMPUTED", "HOLD_MISSING_INPUT", "HOLD_INVALID_INPUT"}


def expected_price_readback_matches(readback: Dict[str, Any], *, market_context_key: str,
                                    card_id: int, requested_as_of: str) -> bool:
    """The child is carried by the existing market-context readback, never fetched separately."""
    expected = readback.get("expected_price")
    if not isinstance(expected, dict):
        return False
    result = expected.get("result")
    return (
        expected.get("run_key") == expected_price_run_key(market_context_key)
        and expected.get("card_id") == card_id
        and expected.get("evidence_id") == readback.get("evidence_id")
        and expected.get("filter_id") == readback.get("filter_id")
        and expected.get("requested_as_of") == requested_as_of
        and expected.get("source_topic") == API_SOURCE_TOPIC
        and expected.get("status") in EXPECTED_PRICE_TERMINAL_STATUSES
        and isinstance(result, dict)
        and result.get("status") == expected.get("status")
    )


def validate_child(readback: Dict[str, Any], *, key: str, card_id: int, date: str,
                   as_of: str | None = None) -> str:
    persisted = readback.get("requested_as_of")
    try:
        observed = datetime.fromisoformat(persisted)
        if observed.utcoffset() != KST.utcoffset(observed):
            raise ValueError("not KST")
        observation_as_of(observed, date=date)
    except (TypeError, ValueError):
        raise RunFailure("readback", "card_%s_mismatch" % card_id)
    if (readback.get("run_key") != key or readback.get("card_id") != card_id
            or readback.get("source_topic") != API_SOURCE_TOPIC
            or (as_of is not None and persisted != as_of)
            or any(type(readback.get(field)) is not int or readback[field] <= 0
                   for field in ("evidence_id", "filter_id"))):
        raise RunFailure("readback", "card_%s_mismatch" % card_id)
    if not expected_price_readback_matches(
        readback, market_context_key=key, card_id=card_id, requested_as_of=persisted,
    ):
        raise RunFailure("expected_price", "card_%s_expected_price_mismatch" % card_id)
    return persisted


def failure_report(stage: str, count: int, reasons: Iterable[str]) -> str:
    compact = ",".join(str(reason) for reason in reasons if reason) or "unknown"
    return "시장맥락 오류 stage=%s count=%s reasons=%s" % (stage, count, compact)


def scheduler_latest(env: Dict[str, str], *, kind: str, date: str, budget: RequestBudget,
                     reserve_slots: int = 0) -> Dict[str, Any]:
    return api(env, "GET", "/api/internal/scheduler-runs/latest?" + urlencode({"kind": kind, "date": date}),
               budget=budget, reserve_slots=reserve_slots)


def terminal_readback_matches(latest: Dict[str, Any], *, aggregate_key: str, status: str, count: int,
                              ids: list[int] | None = None, observations: dict[str, str] | None = None) -> bool:
    detail = latest.get("detail")
    return (latest.get("run_key") == aggregate_key and latest.get("status") == status
            and isinstance(detail, dict) and detail.get("count") == count
            and (status != "done" or (
                ids is not None and observations is not None
                and isinstance(detail.get("detail"), dict)
                and detail["detail"].get("cards") == {"ids": ids, "observation_as_of": observations})))


def observation_as_of(current: datetime, *, date: str) -> str:
    """Use the dispatch wall clock, never a scheduled timestamp, for a card."""
    observed = current.astimezone(KST)
    if (observed.date().isoformat() != date or observed.time() < OBSERVATION_WINDOW_START
            or observed.time() >= OBSERVATION_WINDOW_END):
        raise RunFailure("window", "observation_outside_0905_0925_kst_window")
    return observed.isoformat()


def finish_error(env: Dict[str, str], aggregate_key: str, date: str, count: int, stage: str,
                 reasons: list[str], budget: RequestBudget) -> str | None:
    try:
        api(env, "POST", "/api/internal/scheduler-runs/%s/finish" % aggregate_key,
            {"status": "error", "count": count, "detail": {"stage": stage, "reasons": reasons}},
            budget=budget, reserve_slots=1)
        latest = scheduler_latest(env, kind="market_context", date=date, budget=budget)
        if not terminal_readback_matches(latest, aggregate_key=aggregate_key, status="error", count=count):
            return "readback_mismatch"
    except RunFailure as exc:
        return "%s_%s" % (exc.stage, exc.reason)
    return None


def raise_after_error_terminalization(env: Dict[str, str], *, aggregate_key: str, date: str,
                                      failure: RunFailure, budget: RequestBudget) -> None:
    terminalization_error = finish_error(
        env, aggregate_key, date, failure.count, failure.stage, [failure.reason], budget,
    )
    if terminalization_error:
        raise RunFailure(
            failure.stage,
            "%s,error_terminalization_failed:%s" % (failure.reason, terminalization_error),
            failure.count,
        )
    raise failure


def recover_ambiguous_start(env: Dict[str, str], *, aggregate_key: str, date: str,
                            failure: RunFailure, budget: RequestBudget) -> NoReturn:
    """Prove an uncertain start did not leave this deterministic key started."""
    try:
        latest = scheduler_latest(env, kind="market_context", date=date, budget=budget, reserve_slots=2)
    except RunFailure as exc:
        raise RunFailure(
            "aggregate", "%s,aggregate_start_verification_failed:%s_%s" %
            (failure.reason, exc.stage, exc.reason), failure.count,
        )
    if latest.get("run_key") != aggregate_key:
        raise RunFailure(
            "aggregate", "%s,aggregate_start_verification_failed:latest_mismatch" % failure.reason,
            failure.count,
        )
    if latest.get("status") in {"done", "error"}:
        raise failure
    terminalization_error = finish_error(
        env, aggregate_key, date, failure.count, "aggregate", [failure.reason], budget,
    )
    if terminalization_error:
        raise RunFailure(
            "aggregate", "%s,aggregate_start_verification_failed:%s" %
            (failure.reason, terminalization_error), failure.count,
        )
    raise failure


def execute(*, env: Dict[str, str], now: datetime, source_topic: str, preflight: bool,
            monotonic: Callable[[], float] = time.monotonic,
            wall_clock: Callable[[], datetime] | None = None) -> str:
    if source_topic != SOURCE_TOPIC:
        raise RunFailure("topic", "source_topic_not_allowed")
    check_prompt()
    current = now.astimezone(KST)
    if preflight:
        return "시장맥락 사전점검 완료"
    # Cron is intentionally launched in its scheduled 09:05 minute. The
    # longer window is for cards already in that run, not for late reruns.
    if not (current.hour == 9 and current.minute == 5):
        raise RunFailure("window", "outside_0905_kst_window")

    date = current.date().isoformat()
    hard_deadline = current.replace(hour=9, minute=25, second=0, microsecond=0)
    budget = RequestBudget(
        deadline=monotonic() + max(0.0, (hard_deadline - current).total_seconds()),
        monotonic=monotonic,
    )
    # The card prerequisite must leave a start slot and three ambiguous-start
    # recovery slots (latest, finish(error), final latest).
    latest = scheduler_latest(env, kind="card", date=date, budget=budget, reserve_slots=4)
    if latest.get("status") != "done":
        raise RunFailure("cards", "same_day_card_run_not_done")
    ids = card_ids(latest)
    aggregate_key = run_key(date)
    try:
        # A start response can be lost after persistence.  Leave latest,
        # finish(error), and final latest slots before attempting it.
        started = api(env, "POST", "/api/internal/scheduler-runs/%s/start" % aggregate_key,
                      {"kind": "market_context"}, budget=budget, reserve_slots=3)
    except RunFailure as exc:
        recover_ambiguous_start(
            env, aggregate_key=aggregate_key, date=date,
            failure=RunFailure("aggregate", "aggregate_start_%s" % exc.reason), budget=budget,
        )
    if started.get("kind") != "market_context" or started.get("run_key") != aggregate_key:
        recover_ambiguous_start(
            env, aggregate_key=aggregate_key, date=date,
            failure=RunFailure("aggregate", "aggregate_start_invalid"), budget=budget,
        )

    verified, reasons = 0, []
    card_as_of: dict[str, str] = {}
    for card_id in ids:
        key = card_run_key(date, card_id)
        try:
            child_path = "/api/internal/market-context-runs/%s" % key
            existing = None
            try:
                existing = api(env, "GET", child_path, budget=budget, reserve_slots=2)
            except RunFailure as exc:
                if exc.stage != "api" or exc.reason != "http_404":
                    raise
            if existing is not None:
                as_of = validate_child(existing, key=key, card_id=card_id, date=date)
            else:
                as_of = observation_as_of(wall_clock() if wall_clock is not None else current, date=date)
            post_failure = None
            try:
                api(env, "POST", "/api/internal/cards/%s/market-context" % card_id,
                    {"run_key": key, "as_of": as_of}, budget=budget, reserve_slots=3)
            except RunFailure as exc:
                if exc.stage != "api" or exc.reason not in {"request_failed", "invalid_response", "http_409"}:
                    raise
                post_failure = exc
            try:
                readback = api(env, "GET", child_path, budget=budget, reserve_slots=2)
            except RunFailure:
                if post_failure is not None:
                    raise post_failure
                raise
            validate_child(readback, key=key, card_id=card_id, date=date, as_of=as_of)
            if existing is not None and any(readback[field] != existing[field] for field in ("evidence_id", "filter_id")):
                raise RunFailure("readback", "card_%s_mismatch" % card_id)
            verified += 1
            card_as_of[str(card_id)] = as_of
        except RunFailure as exc:
            reasons.append(exc.reason)

    if verified != len(ids):
        raise_after_error_terminalization(
            env, aggregate_key=aggregate_key, date=date,
            failure=RunFailure("cards", ",".join(reasons), verified), budget=budget,
        )
    try:
        api(env, "POST", "/api/internal/scheduler-runs/%s/finish" % aggregate_key,
            {"status": "done", "count": verified,
             "detail": {"cards": {"ids": ids, "observation_as_of": card_as_of}}},
            budget=budget, reserve_slots=1)
        fresh = scheduler_latest(env, kind="market_context", date=date, budget=budget)
        if not terminal_readback_matches(fresh, aggregate_key=aggregate_key, status="done", count=verified,
                                         ids=ids, observations=card_as_of):
            raise RunFailure("aggregate", "aggregate_done_readback_mismatch", verified)
    except RunFailure as exc:
        raise_after_error_terminalization(env, aggregate_key=aggregate_key, date=date, failure=exc, budget=budget)
    return "시장맥락 완료 count=%s" % verified


def manual_card_ids(db: Any, *, date: str) -> list[int]:
    """Read only the latest same-day card scheduler contract; never widen to card history."""
    run = db.execute(
        """SELECT status, finished_at, detail FROM scheduler_runs
           WHERE kind='card' AND substr(started_at, 1, 10)=?
           ORDER BY id DESC LIMIT 1""",
        (date,),
    ).fetchone()
    if not run:
        raise RunFailure("manual", "same_day_card_run_missing")
    if run["status"] != "done" or not run["finished_at"]:
        raise RunFailure("manual", "same_day_card_run_not_done")
    try:
        return card_ids({"detail": json.loads(run["detail"])})
    except (RunFailure, TypeError, ValueError, json.JSONDecodeError):
        raise RunFailure("manual", "same_day_card_run_malformed")


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def candidate_identifier(root: Path) -> str:
    try:
        return subprocess.check_output(
            ["git", "-C", str(root), "rev-parse", "HEAD"], text=True, stderr=subprocess.DEVNULL,
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return "unavailable"


def manual_expected_price(db_path: Path, artifact: Path, *, now: datetime) -> dict[str, Any]:
    """Explicit local-copy path: no HTTP/KIS/backfill calls and no historical-card scan."""
    root = str(Path(__file__).resolve().parents[1])
    if root not in sys.path:
        sys.path.insert(0, root)
    if not db_path.is_file():
        raise RunFailure("manual", "local_fixture_db_missing")
    from kr_stock_autotrader import db as dbmod
    from kr_stock_autotrader.decision_cards import evidence_detail, filter_detail
    from kr_stock_autotrader.expected_price_runtime import evaluate_and_persist_expected_price
    dbmod.DATABASE_PATH = str(db_path)
    db = dbmod.connect()
    try:
        target_ids = manual_card_ids(db, date=now.date().isoformat())
        placeholders = ",".join("?" for _ in target_ids)
        selected = {row["id"]: dict(row) for row in db.execute(
            "SELECT * FROM decision_cards WHERE id IN (%s)" % placeholders, target_ids,
        )}
        if len(selected) != len(target_ids):
            raise RunFailure("manual", "same_day_card_ids_missing")
        cards = [selected[card_id] for card_id in target_ids]
        counts = {"target": len(cards), "computed": 0, "hold_missing_input": 0, "hold_invalid_input": 0,
                  "calculation_error": 0, "persisted": 0, "readback": 0}
        results = []
        for card in cards:
            key = "expected-price-manual-rework-%s-card-%s" % (now.date().isoformat(), card["id"])
            result = evaluate_and_persist_expected_price(db=db, run_key=key, card=card,
                evidence=evidence_detail(db, card["evidence_id"]), filter_result=filter_detail(db, card["filter_id"]),
                requested_as_of=now.isoformat())
            status = result["status"]
            counts[{"COMPUTED":"computed", "HOLD_MISSING_INPUT":"hold_missing_input", "HOLD_INVALID_INPUT":"hold_invalid_input", "CALCULATION_ERROR":"calculation_error"}[status]] += 1
            row = db.execute("SELECT result_json FROM expected_price_runs WHERE run_key=?", (key,)).fetchone()
            if row:
                counts["persisted"] += 1
                persisted = json.loads(row["result_json"])
                if persisted == result["result"]:
                    counts["readback"] += 1
                results.append({
                    "card_id": card["id"], "run_key": key, "status": status,
                    "reason": persisted.get("reason"),
                    "missing_fields": persisted.get("missing_fields", []),
                    "invalid_fields": persisted.get("invalid_fields", []),
                    "calculated_value": persisted.get("calculated_value"),
                })
            else:
                results.append({"card_id": card["id"], "run_key": key, "status": status,
                                "reason": None, "missing_fields": [], "invalid_fields": [],
                                "calculated_value": None})
        report = {"mode":"manual_out_of_window_local_fixture", "network_calls":0, "kis_calls":0, "intraday_calls":0,
                  "backfill_calls":0, "counts":counts, "results":results,
                  "status":"error" if counts["calculation_error"] or counts["persisted"] != counts["target"] or counts["readback"] != counts["target"] else "done",
                  "manifest": {
                      "candidate_identifier": candidate_identifier(Path(root)),
                      "evaluator_module_sha256": sha256_file(Path(root) / "kr_stock_autotrader" / "expected_price_runtime.py"),
                      "command": "giraffe_market_context_0905.py --manual-out-of-window",
                      "mode": "manual_out_of_window_local_fixture",
                      "local_db_sha256": sha256_file(db_path),
                      "local_db_provenance": "explicit_local_fixture_copy",
                      "scheduler_run_key": "card-%s" % now.date().isoformat(),
                      "date": now.date().isoformat(),
                      "card_ids": target_ids,
                  }}
    finally:
        db.close()
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2), encoding="utf-8")
    return report


def main(argv: list[str] | None = None, *, now: datetime | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-topic", default=SOURCE_TOPIC)
    parser.add_argument("--env-file", type=Path, default=Path.home() / ".hermes" / ".env")
    parser.add_argument("--preflight", action="store_true", help="local guard check only; makes no API requests")
    parser.add_argument("--manual-out-of-window", action="store_true", help="require explicit local-fixture, no-network expected-price evaluation")
    parser.add_argument("--manual-db", type=Path, help="local fixture SQLite database only")
    parser.add_argument("--manual-artifact", type=Path, help="run-local JSON report path")
    args = parser.parse_args(argv)
    current = now or datetime.now(KST)
    if args.manual_out_of_window:
        if not args.manual_db or not args.manual_artifact:
            print(failure_report("manual", 0, ["manual_db_and_artifact_required"]))
            return 1
        try:
            report = manual_expected_price(args.manual_db, args.manual_artifact, now=current.astimezone(KST))
        except RunFailure as exc:
            print(failure_report(exc.stage, exc.count, [exc.reason]))
            return 1
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
        return 0 if report["status"] == "done" else 1
    try:
        env = load_env(args.env_file)
        # Production card timestamps are sampled at each dispatch; an explicit
        # `now` is a deterministic test clock and intentionally stays fixed.
        report = execute(
            env=env, now=current, source_topic=args.source_topic, preflight=args.preflight,
            wall_clock=(lambda: current) if now is not None else (lambda: datetime.now(KST)),
        )
    except RunFailure as exc:
        print(failure_report(exc.stage, exc.count, [exc.reason]))
        return 1
    print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
