"""Bounded, local-only backfill for conditional judgment-hold scenario sets."""
from __future__ import annotations

import argparse
import json

from .conditional_scenarios import build_scenario_candidate, create
from .db import connect
from .decision_cards import card_detail, evidence_detail


def run(db, *, apply: bool = False) -> dict:
    rows = list(db.execute("""SELECT c.id FROM decision_cards c JOIN material_evidence e ON e.id=c.evidence_id
        WHERE c.invalidated_at IS NULL AND e.invalidated_at IS NULL AND e.status != 'invalidated' AND c.verdict='판단 보류'
        ORDER BY c.id"""))
    report = {"dry_run": not apply, "eligible_cards": len(rows), "created": 0, "idempotent": 0, "inputs_required": 0, "items": []}
    for row in rows:
        card = card_detail(db, row["id"])
        candidate = build_scenario_candidate(evidence_detail(db, card["evidence_id"]), card)
        if candidate["status"] != "COMPLETE":
            report["inputs_required"] += 1
            report["items"].append({"card_id": card["id"], "status": candidate["status"], "missing": candidate["missing"]})
            continue
        if not apply:
            report["items"].append({"card_id": card["id"], "status": "WOULD_CREATE", "event_identity": candidate["payload"]["event_identity"]})
            continue
        saved = create(db, candidate["payload"])
        report["idempotent" if saved["idempotent"] else "created"] += 1
        report["items"].append({"card_id": card["id"], "status": "IDEMPOTENT" if saved["idempotent"] else "CREATED", "scenario_set_id": saved["id"]})
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description="Backfill only active judgment-hold conditional scenario sets")
    parser.add_argument("--apply", action="store_true", help="append complete conditional sets; default is dry-run")
    args = parser.parse_args(argv)
    db = connect()
    try:
        print(json.dumps(run(db, apply=args.apply), ensure_ascii=False, sort_keys=True))
    finally:
        db.close()


if __name__ == "__main__":
    main()
