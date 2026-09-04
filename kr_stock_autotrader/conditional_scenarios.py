"""Evidence-only conditional scenarios for active judgment-hold cards.

These sets are immutable explanatory conditions, never valuation inputs and never
observable/current-state selectors.  They deliberately fail closed when the
persisted card/evidence cannot support three distinct source-backed conditions.
"""
from __future__ import annotations

from typing import Any

from fastapi import HTTPException

from .decision_cards import canon, now
from .event_scenarios import LABELS, _lineage, detail_by_id

KIND = "CONDITIONAL"
SCHEMA_VERSION = 1


def _texts(value: Any) -> list[str]:
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    if isinstance(value, (list, tuple)):
        return [item.strip() for item in value if isinstance(item, str) and item.strip()]
    if isinstance(value, dict):
        return [item.strip() for item in value.values() if isinstance(item, str) and item.strip()]
    return []


def _unique(items: list[str]) -> list[str]:
    seen: set[str] = set()
    return [item for item in items if not (item in seen or seen.add(item))]


def build_scenario_candidate(evidence: dict[str, Any], card: dict[str, Any]) -> dict[str, Any]:
    """Purely derive a conditional candidate or structured required inputs.

    The card's own unknowns/proof point, confirmed source summary/title, and
    false-positive/invalidation material are the only permitted text sources.
    No model call, defaults, numeric calculation, or semantic expansion occurs.
    """
    card_json = card.get("card") if isinstance(card.get("card"), dict) else card
    evidence = evidence if isinstance(evidence, dict) else {}
    card_json = card_json if isinstance(card_json, dict) else {}
    missing: list[str] = []
    good = _unique(_texts(card_json.get("proof_point")) + _texts(card_json.get("next_check")) + _texts(card_json.get("unknowns")))
    base = _unique(_texts(evidence.get("summary")) + _texts(evidence.get("title")) + _texts(card_json.get("headline")))
    bad = _unique(_texts(card_json.get("false_positive")) + _texts(card_json.get("evidence_invalidation")))
    source_url = evidence.get("source_url")
    if not good: missing.append("card.unknowns_or_next_proof_point")
    if not base: missing.append("evidence.confirmed_summary_or_title")
    if not bad: missing.append("card.false_positive_or_evidence_invalidation")
    if not isinstance(source_url, str) or not source_url.strip(): missing.append("evidence.source_url")
    for key in ("id", "symbol", "known_at", "announcement_at"):
        if not evidence.get(key): missing.append(f"evidence.{key}")
    if not card.get("id") or card.get("evidence_id") != evidence.get("id"):
        missing.append("card/evidence_exact_lineage")
    if missing:
        return {"status": "SCENARIO_INPUTS_REQUIRED", "missing": sorted(set(missing))}
    assert isinstance(source_url, str)
    return {
        "status": "COMPLETE",
        "payload": {
            "kind": KIND, "schema_version": SCHEMA_VERSION,
            "event_identity": f"CONDITIONAL:{evidence['id']}:{card['id']}", "version": 1,
            "symbol": evidence["symbol"], "event_type": KIND,
            "evidence_id": evidence["id"], "card_id": card["id"],
            "disclosed_at": evidence["announcement_at"], "known_at": evidence["known_at"],
            "source_url": source_url.strip(),
            "scenarios": [
                {"label": "BAD", "conditions": bad},
                {"label": "BASE", "conditions": base + [f"확인 필요: {item}" for item in good]},
                {"label": "GOOD", "conditions": [f"확인 조건: {item}" for item in good]},
            ],
        },
    }


def create(db, data):
    # _lineage owns active/non-invalidated exact lineage validation.
    required = _validate_without_lineage(data)
    _lineage(db, required)
    existing = db.execute("SELECT id,scenario_json,scenarios_json FROM event_scenario_sets WHERE card_id=? AND event_identity=? AND version=?", (required["card_id"], required["event_identity"], required["version"])).fetchone()
    scenario_json = canon({key: value for key, value in required.items() if key != "scenarios"})
    scenarios_json = canon(required["scenarios"])
    if existing:
        if existing["scenario_json"] == scenario_json and existing["scenarios_json"] == scenarios_json:
            result = detail_by_id(db, existing["id"]); result["idempotent"] = True; return result
        raise HTTPException(409, "immutable conditional scenario identity/version payload mismatch")
    head = db.execute("SELECT MAX(version) AS version FROM event_scenario_sets WHERE card_id=? AND event_identity=?", (required["card_id"], required["event_identity"])).fetchone()["version"]
    if required["version"] != (1 if head is None else head + 1):
        raise HTTPException(409, "conditional scenario parent is not current head")
    try:
        row = db.execute("""INSERT INTO event_scenario_sets(event_identity,version,symbol,event_type,profile_id,profile_version,evidence_id,card_id,disclosed_at,known_at,frozen_at,scenario_json,scenarios_json,expected_value_krw,scenario_kind,scenario_schema_version)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) RETURNING id""", (required["event_identity"], required["version"], required["symbol"], KIND, KIND, SCHEMA_VERSION, required["evidence_id"], required["card_id"], required["disclosed_at"], required["known_at"], now(), scenario_json, scenarios_json, None, KIND, SCHEMA_VERSION)).fetchone()
    except Exception as exc:
        raise HTTPException(409, "conditional scenario persistence conflict") from exc
    db.commit()
    result = detail_by_id(db, row["id"]); result["idempotent"] = False; return result


def _validate_without_lineage(data: Any) -> dict[str, Any]:
    # Kept separate to make pure format validation testable without a DB.
    required = {"kind", "schema_version", "event_identity", "version", "symbol", "event_type", "evidence_id", "card_id", "disclosed_at", "known_at", "source_url", "scenarios"}
    if not isinstance(data, dict) or set(data) != required or data.get("kind") != KIND or data.get("schema_version") != SCHEMA_VERSION or data.get("event_type") != KIND:
        raise HTTPException(422, "invalid conditional scenario set")
    if not isinstance(data["version"], int) or isinstance(data["version"], bool) or data["version"] <= 0:
        raise HTTPException(422, "invalid conditional scenario version")
    if not all(isinstance(data.get(key), str) and data[key].strip() for key in ("event_identity", "symbol", "disclosed_at", "known_at", "source_url")):
        raise HTTPException(422, "invalid conditional scenario source fields")
    if not isinstance(data.get("evidence_id"), int) or not isinstance(data.get("card_id"), int): raise HTTPException(422, "conditional scenario lineage required")
    scenarios = data.get("scenarios")
    if not isinstance(scenarios, list) or len(scenarios) != 3 or {item.get("label") for item in scenarios if isinstance(item, dict)} != set(LABELS): raise HTTPException(422, "exactly GOOD BASE BAD conditions required")
    for item in scenarios:
        if set(item) != {"label", "conditions"} or not isinstance(item["conditions"], list) or not item["conditions"] or not all(isinstance(text, str) and text.strip() for text in item["conditions"]): raise HTTPException(422, "invalid conditional scenario conditions")
    return data
