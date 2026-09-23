#!/usr/bin/env python3
"""Read-only end-to-end LLM-visible byte/character proxy for a Giraffe run."""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from kr_stock_autotrader.giraffe_review_queue import (canonical_bytes, compact_gate_payload, compact_review_manifest, open_review_packet)


def git_bytes(rev_path: str) -> bytes:
    return subprocess.check_output(["git", "show", rev_path], cwd=ROOT)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Measure complete raw versus compact Giraffe LLM-visible proxy")
    parser.add_argument("contract", type=Path)
    parser.add_argument("--base", default="b179205")
    parser.add_argument("--page-size", type=int, default=25)
    args = parser.parse_args(argv)
    raw_contract = args.contract.read_bytes()
    contract = json.loads(raw_contract.decode("utf-8"))
    digest = hashlib.sha256(canonical_bytes(contract)).hexdigest()
    control_root = args.contract.parent
    source_root = Path(contract["sources"][0]["packet_path"]).parents[1] if contract["sources"] else Path.home()
    base_prompt = git_bytes(f"{args.base}:prompts/giraffe-material-discovery-v1.md")
    base_cron = git_bytes(f"{args.base}:ops/giraffe-cron-07-prompt.txt")
    current_prompt = (ROOT / "prompts/giraffe-material-discovery-v1.md").read_bytes()
    current_cron = (ROOT / "ops/giraffe-cron-07-prompt.txt").read_bytes()
    raw_texts, compact_texts, receipts = [], [], []
    for source in contract["sources"]:
        if "packet_path" not in source:
            continue
        packet = open_review_packet(contract["run_key"], digest, source["rcp_no"], control_root=control_root, source_root=source_root)
        # completed packet validation inside open_review_packet has already bound this text artifact.
        raw_texts.append(Path(source["packet_path"]).with_name(source["rcp_no"] + ".viewer.txt").read_bytes())
        compact_texts.append(packet["text"].encode("utf-8")); receipts.append(source["rcp_no"])
    manifest = compact_review_manifest(contract["run_key"], digest, control_root=control_root, page_size=args.page_size)
    gate = canonical_bytes(compact_gate_payload(contract["run_key"], digest, contract["control_count"], len(receipts), contract["control_count"] - len(receipts)))
    queue_pages = [canonical_bytes(manifest["items"][n:n + args.page_size]) for n in range(0, len(manifest["items"]), args.page_size)]
    before_parts = [base_prompt, base_cron, raw_contract, *raw_texts]
    after_parts = [current_prompt, current_cron, gate, *queue_pages, *compact_texts]
    before = b"".join(before_parts); after = b"".join(after_parts)
    out = {"metric": "UTF-8 byte/character proxy; not tokenizer measurement", "run_key": contract["run_key"], "control_count": contract["control_count"], "packet_count": len(receipts),
           "raw_viewer_bytes": sum(map(len, raw_texts)), "compact_visible_bytes": sum(map(len, compact_texts)),
           "raw_viewer_chars": len(b"".join(raw_texts).decode("utf-8")), "compact_visible_chars": len(b"".join(compact_texts).decode("utf-8")),
           "before_bytes": len(before), "after_bytes": len(after), "before_chars": len(before.decode("utf-8")), "after_chars": len(after.decode("utf-8")),
           "reduction_percent": round(100 * (1 - len(after) / len(before)), 4), "page_count": manifest["page_count"], "page_chain_sha256": manifest["page_chain_sha256"],
           "all_compaction_completed": len(compact_texts) == len(receipts) and all(compact_texts)}
    print(json.dumps(out, ensure_ascii=False, sort_keys=True)); return 0

if __name__ == "__main__": raise SystemExit(main())
