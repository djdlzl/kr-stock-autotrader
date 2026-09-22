"""Deterministic, exact-set DART receipt terminal-audit planning."""

from __future__ import annotations

from datetime import datetime
import math
from urllib.parse import urlparse

from kr_stock_autotrader.dart_report_classification import is_correction_single_sale_supply_contract


class TerminalAuditError(ValueError):
    """Raised for malformed or incomplete receipt-level audit input."""


_STANDARD_FACTS = frozenset({"binding_contract", "contract_amount", "prior_revenue", "ratio_percent", "term"})
_AMENDMENT_FACTS = frozenset({
    "binding_contract", "economic_basis", "original_contract_amount", "amended_contract_amount",
    "incremental_contract_amount", "prior_revenue", "incremental_ratio_percent", "original_term", "amended_term",
})
_NON_SUPPLY_DISPOSITIONS = frozenset({"negative_risk", "below_threshold", "timing_unresolved"})


def _timestamp(value: object) -> bool:
    if not isinstance(value, str) or not value:
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() is not None


def _reason(value: object) -> str:
    if not isinstance(value, str) or value.strip() != value or not 0 < len(value) <= 1000:
        raise TerminalAuditError("economic_reason must be a bounded trimmed string")
    return value


def _source_url(value: object) -> str:
    if not isinstance(value, str) or value.strip() != value or not 0 < len(value) <= 2000:
        raise TerminalAuditError("source_url must be a bounded trimmed HTTPS URL")
    parsed = urlparse(value)
    if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password or parsed.fragment:
        raise TerminalAuditError("source_url must be a bounded trimmed HTTPS URL")
    return value


def _standard_qualifies(facts: object) -> bool:
    if not isinstance(facts, dict) or set(facts) != _STANDARD_FACTS:
        raise TerminalAuditError("supply-contract economic_facts have the wrong shape")
    if not isinstance(facts["binding_contract"], bool):
        raise TerminalAuditError("binding_contract must be boolean")
    for key in ("contract_amount", "prior_revenue"):
        if not isinstance(facts[key], int) or isinstance(facts[key], bool) or facts[key] <= 0:
            raise TerminalAuditError(f"{key} must be a positive integer")
    if (not isinstance(facts["ratio_percent"], (int, float)) or isinstance(facts["ratio_percent"], bool)
            or not math.isfinite(float(facts["ratio_percent"])) or not 0 <= facts["ratio_percent"] <= 100000):
        raise TerminalAuditError("ratio_percent must be numeric")
    if (not isinstance(facts["term"], str) or facts["term"].strip() != facts["term"]
            or not 0 < len(facts["term"]) <= 500):
        raise TerminalAuditError("term must be a nonempty trimmed string")
    return facts["binding_contract"] and facts["contract_amount"] * 2 >= facts["prior_revenue"]


def _amendment_qualifies(facts: object) -> bool:
    if not isinstance(facts, dict) or set(facts) != _AMENDMENT_FACTS or facts.get("economic_basis") != "amendment_delta":
        raise TerminalAuditError("amendment economic_facts have the wrong shape")
    if not isinstance(facts["binding_contract"], bool):
        raise TerminalAuditError("binding_contract must be boolean")
    for key in ("original_contract_amount", "amended_contract_amount", "incremental_contract_amount", "prior_revenue"):
        if not isinstance(facts[key], int) or isinstance(facts[key], bool):
            raise TerminalAuditError(f"{key} must be an integer")
    if facts["original_contract_amount"] <= 0 or facts["amended_contract_amount"] <= 0 or facts["prior_revenue"] <= 0:
        raise TerminalAuditError("amendment amounts and prior_revenue must be positive")
    if facts["incremental_contract_amount"] != facts["amended_contract_amount"] - facts["original_contract_amount"]:
        raise TerminalAuditError("incremental_contract_amount must equal amended minus original")
    if (not isinstance(facts["incremental_ratio_percent"], (int, float)) or isinstance(facts["incremental_ratio_percent"], bool)
            or not math.isfinite(float(facts["incremental_ratio_percent"]))
            or not -100000 <= facts["incremental_ratio_percent"] <= 100000):
        raise TerminalAuditError("incremental_ratio_percent must be numeric")
    if abs(float(facts["incremental_ratio_percent"]) - facts["incremental_contract_amount"] * 100 / facts["prior_revenue"]) > 0.005:
        raise TerminalAuditError("incremental_ratio_percent must match the amendment delta")
    if any(not isinstance(facts[key], str) or facts[key].strip() != facts[key] or not 0 < len(facts[key]) <= 500 for key in ("original_term", "amended_term")):
        raise TerminalAuditError("amendment terms must be nonempty trimmed strings")
    return facts["binding_contract"] and facts["incremental_contract_amount"] > 0 and facts["incremental_contract_amount"] * 2 >= facts["prior_revenue"]


def _non_supply_terminal(source: dict, audit: object, rcp_no: str) -> dict:
    """Require a source-grounded human audit; never infer non-supply economics."""
    if not isinstance(audit, dict) or set(audit) != {"source_url", "economic_disposition", "economic_reason", "disposition"}:
        raise TerminalAuditError("non-supply audit must contain exactly source_url, economic_disposition, economic_reason, disposition")
    _source_url(audit["source_url"])
    reason = _reason(audit["economic_reason"])
    if audit["economic_disposition"] not in _NON_SUPPLY_DISPOSITIONS:
        raise TerminalAuditError("non-supply economic_disposition is not allowed")
    if audit["disposition"] not in {"rejected", "hold"}:
        raise TerminalAuditError("non-supply disposition must be rejected or hold")
    if audit["economic_disposition"] == "timing_unresolved" and audit["disposition"] != "hold":
        raise TerminalAuditError("timing_unresolved non-supply audit must hold")
    return {"action": "terminal", "item": {"rcp_no": rcp_no, "disposition": audit["disposition"], "evidence_id": None,
            "economic_disposition": audit["economic_disposition"], "economic_reason": reason, "economic_facts": None}}


def terminal_audit_plan(source: object, audit: object) -> dict:
    """Plan one safe action without inventing an announcement or publication time."""
    if not isinstance(source, dict) or not isinstance(audit, dict):
        raise TerminalAuditError("source and audit must be objects")
    rcp_no, report_class, report_name = source.get("rcp_no"), source.get("report_class"), source.get("report_name")
    if not isinstance(rcp_no, str) or len(rcp_no) != 14 or not rcp_no.isdigit():
        raise TerminalAuditError("source rcp_no must be exactly 14 digits")
    if report_class == "other":
        return _non_supply_terminal(source, audit, rcp_no)
    if report_class != "dart_single_sale_supply_contract" or not isinstance(report_name, str) or not 0 < len(report_name) <= 500:
        raise TerminalAuditError("source report class is not an authoritative DART class")
    reason = _reason(audit.get("economic_reason"))
    facts = audit.get("economic_facts")
    qualifies = _amendment_qualifies(facts) if is_correction_single_sale_supply_contract(report_name) else _standard_qualifies(facts)
    if not qualifies:
        return {"action": "terminal", "item": {"rcp_no": rcp_no, "disposition": "rejected", "evidence_id": None,
                "economic_disposition": "below_threshold", "economic_reason": reason, "economic_facts": facts}}
    published_at = audit.get("published_at")
    if published_at is None:
        return {"action": "terminal", "item": {"rcp_no": rcp_no, "disposition": "hold", "evidence_id": None,
                "economic_disposition": "timing_unresolved", "economic_reason": reason, "economic_facts": facts}}
    if not _timestamp(published_at):
        raise TerminalAuditError("published_at must be a timezone-aware ISO timestamp or null")
    return {"action": "evidence_add_required", "rcp_no": rcp_no, "published_at": published_at,
            "economic_disposition": "qualifying_A_or_better", "economic_reason": reason, "economic_facts": facts}


def _audit_map(audits: object, expected: list[str]) -> dict[str, dict]:
    if isinstance(audits, dict):
        if set(audits) != set(expected) or any(not isinstance(key, str) or not isinstance(value, dict) for key, value in audits.items()):
            raise TerminalAuditError("audit map receipt IDs are not the exact source set")
        return audits
    if not isinstance(audits, list):
        raise TerminalAuditError("audits must be an exact receipt map or list")
    mapped: dict[str, dict] = {}
    for entry in audits:
        if not isinstance(entry, dict) or set(entry) != {"rcp_no", "audit"} or not isinstance(entry["rcp_no"], str) or not isinstance(entry["audit"], dict):
            raise TerminalAuditError("audit list entries must contain exactly rcp_no and audit")
        if entry["rcp_no"] in mapped:
            raise TerminalAuditError("duplicate audit receipt ID")
        mapped[entry["rcp_no"]] = entry["audit"]
    if set(mapped) != set(expected):
        raise TerminalAuditError("audit list receipt IDs are not the exact source set")
    return mapped


def terminal_audit_batch(sources: object, audits: object) -> dict:
    """Process every immutable control receipt once and expose only terminal-safe items.

    Timestamped qualifying receipts are emitted solely as evidence requirements;
    callers cannot place them in ``terminal_items`` before evidence readback.
    """
    if not isinstance(sources, list) or not sources:
        raise TerminalAuditError("sources must be a nonempty immutable control list")
    ids = []
    for source in sources:
        if not isinstance(source, dict):
            raise TerminalAuditError("sources must contain objects")
        rcp_no = source.get("rcp_no")
        if not isinstance(rcp_no, str) or len(rcp_no) != 14 or not rcp_no.isdigit():
            raise TerminalAuditError("source rcp_no must be exactly 14 digits")
        ids.append(rcp_no)
    if len(set(ids)) != len(ids):
        raise TerminalAuditError("duplicate source receipt ID")
    by_id = _audit_map(audits, ids)
    terminal_items, evidence_requirements = [], []
    for source in sources:
        plan = terminal_audit_plan(source, by_id[source["rcp_no"]])
        if plan["action"] == "terminal":
            terminal_items.append(plan["item"])
        else:
            evidence_requirements.append({key: plan[key] for key in ("rcp_no", "published_at", "economic_disposition", "economic_reason", "economic_facts")})
    return {"terminal_items": terminal_items, "evidence_requirements": evidence_requirements}
