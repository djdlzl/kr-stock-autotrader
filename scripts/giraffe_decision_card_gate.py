#!/usr/bin/env python3
"""Executable KRX admission boundary for the 08:00 card workflow.

The cron wrapper invokes this gate before it starts the scheduler agent.  The
optional callable keeps the side-effect boundary explicit and testable: closed
or malformed calendar input never reaches it.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Callable, Sequence
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from kr_stock_autotrader.krx_calendar import CalendarError, is_krx_business_date

KST = ZoneInfo("Asia/Seoul")


class AdmissionError(RuntimeError):
    pass


def execute(*, now: datetime, run: Callable[[], object]) -> str:
    """Run the 08 job only on an admitted KRX business date."""
    current = now.astimezone(KST)
    try:
        admitted = is_krx_business_date(current.date())
    except CalendarError as exc:
        raise AdmissionError("calendar admission failed") from exc
    if not admitted:
        return ""
    run()
    return "08 admission complete"


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
    parser = argparse.ArgumentParser(description="KRX admission wrapper for the 08:00 scheduler")
    parser.add_argument("--at", help="test/recovery timestamp as ISO-8601 with timezone")
    parser.add_argument("command", nargs=argparse.REMAINDER, help="scheduler command after --")
    args = parser.parse_args(argv)
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("an 08 scheduler command is required after --")
    try:
        result = execute(now=_parse_now(args.at), run=lambda: subprocess.run(command, check=True))
    except (AdmissionError, subprocess.CalledProcessError) as exc:
        print(json.dumps({"gate": "GIRAFFE_0800_ADMISSION_V1", "complete": False, "error": str(exc)}))
        return 2
    if result:
        print(json.dumps({"gate": "GIRAFFE_0800_ADMISSION_V1", "complete": True}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
