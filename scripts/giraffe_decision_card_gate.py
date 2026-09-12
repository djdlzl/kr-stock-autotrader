#!/usr/bin/env python3
"""Hermes cron prehook for the 08:00 KRX decision-card workflow.

Hermes skips the scheduler agent only when the final non-empty stdout line is
JSON containing ``{"wakeAgent": false}``.  This script is therefore directly
installable as a cron ``script`` prehook; an optional command remains available
for explicit non-cron recovery use.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Callable, Sequence
from zoneinfo import ZoneInfo


def _add_deployed_repo_to_path() -> None:
    """Locate the deployed package after this prehook is copied to Hermes scripts."""
    candidates = (
        Path(__file__).resolve().parents[1],
        Path.cwd(),
        Path(os.environ["GIRAFFE_DEPLOYED_REPO"]).expanduser()
        if os.environ.get("GIRAFFE_DEPLOYED_REPO")
        else None,
    )
    for root in candidates:
        if root is not None and (root / "kr_stock_autotrader" / "krx_calendar.py").is_file():
            sys.path.insert(0, str(root))
            return
    raise ImportError("cannot locate deployed kr_stock_autotrader package")


_add_deployed_repo_to_path()
from kr_stock_autotrader.krx_calendar import CalendarError, is_krx_business_date

KST = ZoneInfo("Asia/Seoul")
GATE_NAME = "GIRAFFE_0800_ADMISSION_V1"


class AdmissionError(RuntimeError):
    pass


def evaluate(*, now: datetime, run: Callable[[], object] | None = None) -> dict:
    """Return the Hermes wake-gate payload and run an optional recovery command."""
    current = now.astimezone(KST)
    try:
        admitted = is_krx_business_date(current.date())
    except CalendarError as exc:
        raise AdmissionError("calendar admission failed") from exc
    payload = {"gate": GATE_NAME, "date": current.date().isoformat(), "wakeAgent": admitted}
    if admitted and run is not None:
        run()
    return payload


def _parse_now(value: str | None) -> datetime:
    if value is None:
        return datetime.now(KST)
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise AdmissionError("invalid KST timestamp") from exc
    if parsed.tzinfo is None:
        raise AdmissionError("timestamp must include timezone")
    return parsed.astimezone(KST)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="KRX admission prehook for the 08:00 scheduler")
    parser.add_argument("--at", help="test/recovery timestamp as ISO-8601 with timezone")
    parser.add_argument("command", nargs=argparse.REMAINDER, help="optional recovery command after --")
    args = parser.parse_args(argv)
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    try:
        payload = evaluate(
            now=_parse_now(args.at),
            run=(lambda: subprocess.run(command, check=True)) if command else None,
        )
    except (AdmissionError, subprocess.CalledProcessError) as exc:
        # Hermes parses only the final non-empty stdout line, even after a failed script exit.
        # Keep this explicit fail-closed gate so scheduler_prompt never starts an agent/LLM.
        print(
            json.dumps(
                {"gate": GATE_NAME, "complete": False, "error": str(exc), "wakeAgent": False},
                sort_keys=True,
            )
        )
        return 2
    print(json.dumps(payload, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
