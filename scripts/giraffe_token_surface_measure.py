#!/usr/bin/env python3
"""Read-only character/byte proxy for Giraffe's compact prehook surface."""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from kr_stock_autotrader.giraffe_review_queue import canonical_bytes, compact_gate_payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Measure full-control versus compact-gate payload bytes")
    parser.add_argument("contract", type=Path)
    args = parser.parse_args(argv)
    raw = args.contract.read_bytes()
    contract = json.loads(raw)
    canonical = canonical_bytes(contract)
    sources = contract.get("sources")
    if not isinstance(sources, list):
        raise SystemExit("contract has no source list")
    compact = compact_gate_payload(contract["run_key"], hashlib.sha256(canonical).hexdigest(),
                                   contract["control_count"], sum("packet_path" in source for source in sources),
                                   sum("source_error_code" in source for source in sources))
    encoded = canonical_bytes(compact)
    print(json.dumps({"metric": "UTF-8 byte/character proxy; not tokenizer tokens", "full_contract_bytes": len(raw),
                      "full_contract_chars": len(raw.decode("utf-8")), "compact_gate_bytes": len(encoded),
                      "compact_gate_chars": len(encoded.decode("utf-8")), "reduction_percent": round(100 * (1 - len(encoded) / len(raw)), 4),
                      "control_count": contract["control_count"], "source_valid_count": compact["source_valid_count"],
                      "source_error_count": compact["source_error_count"]}, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
