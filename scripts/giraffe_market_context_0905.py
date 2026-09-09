#!/usr/bin/env python3
"""Narrow 09:05 KST market-context cron runner; it has no trading operations."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from datetime import datetime
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


class RunFailure(Exception):
    def __init__(self, stage: str, reason: str, count: int = 0):
        self.stage, self.reason, self.count = stage, reason, count
        super().__init__(reason)


class RequestBudget:
    """Monotonic budget that prevents starting a request too close to 09:06."""

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
    try:
        ids = run["detail"]["detail"]["cards"]["ids"]
    except (KeyError, TypeError):
        raise RunFailure("cards", "authoritative_card_ids_missing")
    if not isinstance(ids, list) or not ids or any(type(item) is not int or item <= 0 for item in ids):
        raise RunFailure("cards", "authoritative_card_ids_invalid")
    if len(set(ids)) != len(ids):
        raise RunFailure("cards", "authoritative_card_ids_duplicate")
    return ids


def run_key(date: str) -> str:
    return "market-context-%s-0905-kst-topic7923" % date


def card_run_key(date: str, card_id: int) -> str:
    return "market-context-%s-0905-kst-topic7923-card-%s" % (date, card_id)


def failure_report(stage: str, count: int, reasons: Iterable[str]) -> str:
    compact = ",".join(str(reason) for reason in reasons if reason) or "unknown"
    return "시장맥락 오류 stage=%s count=%s reasons=%s" % (stage, count, compact)


def scheduler_latest(env: Dict[str, str], *, kind: str, date: str, budget: RequestBudget,
                     reserve_slots: int = 0) -> Dict[str, Any]:
    return api(env, "GET", "/api/internal/scheduler-runs/latest?" + urlencode({"kind": kind, "date": date}),
               budget=budget, reserve_slots=reserve_slots)


def terminal_readback_matches(latest: Dict[str, Any], *, aggregate_key: str, status: str, count: int) -> bool:
    detail = latest.get("detail")
    return (latest.get("run_key") == aggregate_key and latest.get("status") == status
            and isinstance(detail, dict) and detail.get("count") == count)


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
            monotonic: Callable[[], float] = time.monotonic) -> str:
    if source_topic != SOURCE_TOPIC:
        raise RunFailure("topic", "source_topic_not_allowed")
    check_prompt()
    current = now.astimezone(KST)
    if preflight:
        return "시장맥락 사전점검 완료"
    if not (current.hour == 9 and current.minute == 5):
        raise RunFailure("window", "outside_0905_kst_window")

    date = current.date().isoformat()
    as_of = date + "T09:05:00+09:00"
    hard_deadline = current.replace(hour=9, minute=6, second=0, microsecond=0)
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
    for card_id in ids:
        key = card_run_key(date, card_id)
        try:
            api(env, "POST", "/api/internal/cards/%s/market-context" % card_id,
                {"run_key": key, "as_of": as_of}, budget=budget, reserve_slots=2)
            readback = api(env, "GET", "/api/internal/market-context-runs/%s" % key,
                           budget=budget, reserve_slots=2)
            if (readback.get("run_key") != key or readback.get("card_id") != card_id
                    or readback.get("requested_as_of") != as_of
                    or readback.get("source_topic") != API_SOURCE_TOPIC):
                raise RunFailure("readback", "card_%s_mismatch" % card_id)
            verified += 1
        except RunFailure as exc:
            reasons.append(exc.reason)

    if verified != len(ids):
        raise_after_error_terminalization(
            env, aggregate_key=aggregate_key, date=date,
            failure=RunFailure("cards", ",".join(reasons), verified), budget=budget,
        )
    try:
        api(env, "POST", "/api/internal/scheduler-runs/%s/finish" % aggregate_key,
            {"status": "done", "count": verified, "detail": {"cards": {"ids": ids}, "as_of": as_of}},
            budget=budget, reserve_slots=1)
        fresh = scheduler_latest(env, kind="market_context", date=date, budget=budget)
        if not terminal_readback_matches(fresh, aggregate_key=aggregate_key, status="done", count=verified):
            raise RunFailure("aggregate", "aggregate_done_readback_mismatch", verified)
    except RunFailure as exc:
        raise_after_error_terminalization(env, aggregate_key=aggregate_key, date=date, failure=exc, budget=budget)
    return "시장맥락 완료 count=%s" % verified


def manual_expected_price(db_path: Path, artifact: Path, *, now: datetime) -> dict[str, Any]:
    """Explicit out-of-window local-fixture path: no HTTP/KIS/backfill calls."""
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
        cards = [dict(row) for row in db.execute("SELECT * FROM decision_cards ORDER BY id")]
        counts = {"target": len(cards), "computed": 0, "hold_missing_input": 0, "hold_invalid_input": 0,
                  "calculation_error": 0, "persisted": 0, "readback": 0}
        results = []
        for card in cards:
            key = "expected-price-manual-%s-card-%s" % (now.date().isoformat(), card["id"])
            result = evaluate_and_persist_expected_price(db=db, run_key=key, card=card,
                evidence=evidence_detail(db, card["evidence_id"]), filter_result=filter_detail(db, card["filter_id"]),
                requested_as_of=now.isoformat())
            status = result["status"]
            counts[{"COMPUTED":"computed", "HOLD_MISSING_INPUT":"hold_missing_input", "HOLD_INVALID_INPUT":"hold_invalid_input", "CALCULATION_ERROR":"calculation_error"}[status]] += 1
            counts["persisted"] += 1
            row = db.execute("SELECT result_json FROM expected_price_runs WHERE run_key=?", (key,)).fetchone()
            if row and json.loads(row["result_json"]) == result["result"]:
                counts["readback"] += 1
            results.append({"card_id": card["id"], "status": status, "run_key": key})
        report = {"mode":"manual_out_of_window_local_fixture", "network_calls":0, "kis_calls":0, "intraday_calls":0,
                  "backfill_calls":0, "counts":counts, "results":results,
                  "status":"error" if counts["calculation_error"] or counts["persisted"] != counts["target"] or counts["readback"] != counts["target"] else "done"}
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
        report = execute(env=env, now=current, source_topic=args.source_topic, preflight=args.preflight)
    except RunFailure as exc:
        print(failure_report(exc.stage, exc.count, [exc.reason]))
        return 1
    print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
