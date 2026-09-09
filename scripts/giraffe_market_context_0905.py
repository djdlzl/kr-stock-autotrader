#!/usr/bin/env python3
"""Narrow 09:05 KST market-context cron runner; it has no trading operations."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")
SOURCE_TOPIC = "telegram:mac:7923"
PROMPT_SHA256 = "f7a1c1f16e168577222295af43aa05ca5ceaa6a7fc3d20b6a116b222e6fc3a7c"
PROMPT_PATH = Path(__file__).resolve().parents[1] / "prompts" / "giraffe-market-context-scheduler-v1.md"


class RunFailure(Exception):
    def __init__(self, stage: str, reason: str, count: int = 0):
        self.stage, self.reason, self.count = stage, reason, count
        super().__init__(reason)


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


def api(env: Dict[str, str], method: str, path: str, payload: Dict[str, Any] | None = None) -> Dict[str, Any]:
    body = None if payload is None else json.dumps(payload, separators=(",", ":")).encode("utf-8")
    request = Request(
        env["GIRAFFE_URL"].rstrip("/") + path,
        data=body,
        method=method,
        headers={"X-Internal-API-Key": env["INTERNAL_API_KEY"], "Content-Type": "application/json"},
    )
    try:
        with urlopen(request, timeout=20) as response:
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


def finish_error(env: Dict[str, str], aggregate_key: str, count: int, stage: str, reasons: list[str]) -> None:
    try:
        api(env, "POST", "/api/internal/scheduler-runs/%s/finish" % aggregate_key,
            {"status": "error", "count": count, "detail": {"stage": stage, "reasons": reasons}})
    except RunFailure:
        pass


def execute(*, env: Dict[str, str], now: datetime, source_topic: str, preflight: bool) -> str:
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
    latest = api(env, "GET", "/api/internal/scheduler-runs/latest?" + urlencode({"kind": "card", "date": date}))
    if latest.get("status") != "done":
        raise RunFailure("cards", "same_day_card_run_not_done")
    ids = card_ids(latest)
    aggregate_key = run_key(date)
    started = api(env, "POST", "/api/internal/scheduler-runs/%s/start" % aggregate_key, {"kind": "market_context"})
    if started.get("kind") != "market_context" or started.get("run_key") != aggregate_key:
        finish_error(env, aggregate_key, 0, "aggregate", ["aggregate_start_invalid"])
        raise RunFailure("aggregate", "aggregate_start_invalid")

    verified, reasons = 0, []
    for card_id in ids:
        key = card_run_key(date, card_id)
        try:
            api(env, "POST", "/api/internal/cards/%s/market-context" % card_id, {"run_key": key, "as_of": as_of})
            readback = api(env, "GET", "/api/internal/market-context-runs/%s" % key)
            if (readback.get("run_key") != key or readback.get("card_id") != card_id
                    or readback.get("requested_as_of") != as_of):
                raise RunFailure("readback", "card_%s_mismatch" % card_id)
            verified += 1
        except RunFailure as exc:
            reasons.append(exc.reason)

    if verified != len(ids):
        finish_error(env, aggregate_key, verified, "cards", reasons)
        raise RunFailure("cards", ",".join(reasons), verified)
    api(env, "POST", "/api/internal/scheduler-runs/%s/finish" % aggregate_key,
        {"status": "done", "count": verified, "detail": {"cards": {"ids": ids}, "as_of": as_of}})
    return "시장맥락 완료 count=%s" % verified


def main(argv: list[str] | None = None, *, now: datetime | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-topic", default=SOURCE_TOPIC)
    parser.add_argument("--env-file", type=Path, default=Path.home() / ".hermes" / ".env")
    parser.add_argument("--preflight", action="store_true", help="local guard check only; makes no API requests")
    args = parser.parse_args(argv)
    try:
        env = load_env(args.env_file)
        report = execute(env=env, now=now or datetime.now(KST), source_topic=args.source_topic, preflight=args.preflight)
    except RunFailure as exc:
        print(failure_report(exc.stage, exc.count, [exc.reason]))
        return 1
    print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
