#!/usr/bin/env python3
"""Cron prehook: collect complete previous/current KST DART manifests for Giraffe."""

from __future__ import annotations

import json
import hashlib
import os
import sys
import urllib.request
import urllib.error
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
sys.path.insert(0, str(SCRIPT_DIR.parent))
from giraffe_dart_manifest import ManifestError, collect_manifest  # noqa: E402
from giraffe_dart_source import SourceError, completed_packet, fetch_with_retry, write_packet  # noqa: E402
from kr_stock_autotrader.krx_calendar import CalendarError, admitted_backlog_dates  # noqa: E402

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



def register_research_run(run_key: str, contract: dict) -> None:
    """Register the immutable canonical run before any agent/LLM work begins."""
    base, key = os.environ.get("GIRAFFE_URL", "").strip().rstrip("/"), os.environ.get("RESEARCH_CONTROL_KEY", "")
    if not base or not key:
        raise ManifestError("GIRAFFE_URL and RESEARCH_CONTROL_KEY are required for deterministic registration")
    body = canonical_bytes({"control_contract": contract})
    request = urllib.request.Request(base + "/api/internal/research-runs/" + run_key + "/register", data=body, method="POST", headers={"Content-Type": "application/json", "X-Research-Control-Key": key})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            if response.status < 200 or response.status >= 300: raise ManifestError("deterministic research registration failed")
            value = json.load(response)
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ValueError) as exc:
        raise ManifestError("deterministic research registration failed") from exc
    if not isinstance(value, dict) or value.get("run_key") != run_key or value.get("kind") != "research" or value.get("status") not in {"started", "done", "error"}:
        raise ManifestError("deterministic research registration response invalid")


def target_dates(now: datetime | None = None) -> list[str]:
    """Automatic runs use calendar admission; override is explicit recovery only."""
    override = os.environ.get("GIRAFFE_DART_GATE_DATES", "").strip()
    if override:
        if os.environ.get("GIRAFFE_DART_GATE_RECOVERY", "") != "1":
            raise ManifestError("GIRAFFE_DART_GATE_DATES is recovery-only")
        dates = [item.strip().replace("-", "") for item in override.split(",") if item.strip()]
        if not dates or any(len(item) != 8 or not item.isdigit() for item in dates) or len(dates) != len(set(dates)):
            raise ManifestError("GIRAFFE_DART_GATE_DATES must be unique YYYYMMDD dates")
        return dates
    current = (now or datetime.now(ZoneInfo("Asia/Seoul"))).astimezone(ZoneInfo("Asia/Seoul"))
    try:
        return admitted_backlog_dates(current.date())
    except CalendarError as exc:
        raise ManifestError(str(exc)) from exc


def main() -> int:
    try:
        dates = target_dates()
        summaries = []
        for date in dates:
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
        if str(exc) == "KRX market closed":
            return 0
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
    try:
        register_research_run(run_key, contract)
    except ManifestError as exc:
        print(json.dumps({"gate": "GIRAFFE_DART_GATE_V1", "complete": False, "error": str(exc)}, ensure_ascii=False))
        return 2
    print(json.dumps({"gate": "GIRAFFE_DART_GATE_V1", "complete": True, "dates": summaries,
                      "control_contract": contract, "control_contract_path": str(contract_path),
                      "control_contract_sha256": digest}, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
