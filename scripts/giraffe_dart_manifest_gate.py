#!/usr/bin/env python3
"""Cron prehook: collect complete previous/current KST DART manifests for Giraffe."""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
from giraffe_dart_manifest import ManifestError, collect_manifest  # noqa: E402
from giraffe_dart_source import SourceError, fetch_with_retry, write_packet  # noqa: E402

OUTPUT_ROOT = Path.home() / ".hermes" / "runs" / "giraffe-7923" / "dart-manifests"
SOURCE_ROOT = Path.home() / ".hermes" / "runs" / "giraffe-7923" / "dart-source-packets"


def target_dates() -> list[str]:
    override = os.environ.get("GIRAFFE_DART_GATE_DATES", "").strip()
    if override:
        dates = [item.strip().replace("-", "") for item in override.split(",") if item.strip()]
        if not dates or any(len(item) != 8 or not item.isdigit() for item in dates):
            raise ManifestError("GIRAFFE_DART_GATE_DATES must contain YYYYMMDD dates")
        return list(dict.fromkeys(dates))
    now = datetime.now(ZoneInfo("Asia/Seoul"))
    return [(now - timedelta(days=1)).strftime("%Y%m%d"), now.strftime("%Y%m%d")]


def main() -> int:
    try:
        summaries = []
        for date in target_dates():
            manifest = collect_manifest(date)
            output = OUTPUT_ROOT / f"{date}.json"
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            packet_dir = SOURCE_ROOT / date
            source_packets = []
            source_errors = []
            for candidate in manifest["material_candidate_records"]:
                rcp_no = candidate["rcp_no"]
                try:
                    source_packets.append(str(write_packet(fetch_with_retry(rcp_no), packet_dir)))
                except SourceError as exc:
                    source_errors.append({"rcp_no": rcp_no, "code": exc.code, "error": str(exc)})
            if source_errors:
                raise ManifestError("DART source validity failed: " + json.dumps(source_errors, ensure_ascii=False))
            summaries.append({
                "date": date,
                "declared_total": manifest["declared_total"],
                "declared_pages": manifest["declared_pages"],
                "pages_collected": manifest["pages_collected"],
                "page_counts": manifest["page_counts"],
                "unique_receipts": manifest["unique_receipts"],
                "material_candidate_count": manifest["material_candidate_count"],
                "source_packet_paths": source_packets,
                "source_valid_count": len(source_packets),
                "source_error_count": len(source_errors),
                "complete": manifest["complete"],
                "manifest_path": str(output),
            })
    except ManifestError as exc:
        print(json.dumps({"gate": "GIRAFFE_DART_GATE_V1", "complete": False, "error": str(exc)}, ensure_ascii=False))
        return 2
    print(json.dumps({"gate": "GIRAFFE_DART_GATE_V1", "complete": True, "dates": summaries}, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
