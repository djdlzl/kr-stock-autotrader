#!/usr/bin/env python3
"""Cron prehook: collect complete previous/current KST DART manifests for Giraffe."""

from __future__ import annotations

import json
import hashlib
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
from giraffe_dart_manifest import ManifestError, collect_manifest  # noqa: E402
from giraffe_dart_source import SourceError, completed_packet, fetch_with_retry, write_packet  # noqa: E402

OUTPUT_ROOT = Path.home() / ".hermes" / "runs" / "giraffe-7923" / "dart-manifests"
SOURCE_ROOT = Path.home() / ".hermes" / "runs" / "giraffe-7923" / "dart-source-packets"
CONTROL_ROOT = Path.home() / ".hermes" / "runs" / "giraffe-7923" / "dart-control-contracts"


def canonical_bytes(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def control_contract(run_key: str, summaries: list[dict]) -> dict:
    sources = []
    allowed_dates = [item["date"] for item in summaries]
    for summary in summaries:
        for packet_path in summary["source_packet_paths"]:
            raw = Path(packet_path).read_bytes()
            metadata = json.loads(raw)
            rcp_no = metadata.get("rcp_no")
            source_date = metadata.get("source_date")
            if not isinstance(rcp_no, str) or source_date != rcp_no[:8] or source_date not in allowed_dates:
                raise ManifestError("DART source packet date does not match the control window")
            sources.append({"rcp_no": rcp_no, "date": source_date, "packet_path": packet_path,
                            "packet_sha256": hashlib.sha256(raw).hexdigest()})
    sources.sort(key=lambda item: item["rcp_no"])
    receipts = [item["rcp_no"] for item in sources]
    if len(receipts) != len(set(receipts)):
        raise ManifestError("duplicate DART receipt across control dates")
    return {"schema_version": "giraffe-research-control-v1", "run_key": run_key,
            "dates": [item["date"] for item in summaries], "source_valid": True,
            "expected_rcp_nos": receipts, "control_count": len(receipts), "sources": sources}


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
                    checkpoint = packet_dir / f"{rcp_no}.json"
                    if completed_packet(checkpoint, rcp_no) is None:
                        write_packet(fetch_with_retry(rcp_no), packet_dir)
                    source_packets.append(str(checkpoint))
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
    today = summaries[-1]["date"]
    run_key = f"research-{today[:4]}-{today[4:6]}-{today[6:]}-0700-kst"
    contract = control_contract(run_key, summaries)
    digest = hashlib.sha256(canonical_bytes(contract)).hexdigest()
    CONTROL_ROOT.mkdir(parents=True, exist_ok=True)
    contract_path = CONTROL_ROOT / f"{run_key}.json"
    temp = contract_path.with_suffix(".tmp")
    temp.write_bytes(canonical_bytes(contract) + b"\n")
    temp.replace(contract_path)
    print(json.dumps({"gate": "GIRAFFE_DART_GATE_V1", "complete": True, "dates": summaries,
                      "control_contract": contract, "control_contract_path": str(contract_path),
                      "control_contract_sha256": digest}, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
