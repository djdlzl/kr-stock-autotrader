"""Deterministic DART receipt terminal-audit planning."""

from __future__ import annotations

from datetime import datetime
import math

from kr_stock_autotrader.dart_report_classification import is_correction_single_sale_supply_contract


class TerminalAuditError(ValueError):
    """Raised for malformed or incomplete receipt-level audit input."""


_STANDARD_FACTS = frozenset({"binding_contract", "contract_amount", "prior_revenue", "ratio_percent", "term"})
_AMENDMENT_FACTS = frozenset({
    "binding_contract", "economic_basis", "original_contract_amount", "amended_contract_amount",
    "incremental_contract_amount", "prior_revenue", "incremental_ratio_percent", "original_term", "amended_term",
})


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


def terminal_audit_plan(source: object, audit: object) -> dict:
    """Return one safe receipt action without inventing a DART publication time.

    Non-supply filings are closed as receipt-level non-material audits.  Supply
    contracts must carry validated economic facts; qualifying contracts only
    request evidence storage after an event-specific published_at is supplied.
    """
    if not isinstance(source, dict) or not isinstance(audit, dict):
        raise TerminalAuditError("source and audit must be objects")
    rcp_no, report_class, report_name = source.get("rcp_no"), source.get("report_class"), source.get("report_name")
    if not isinstance(rcp_no, str) or len(rcp_no) != 14 or not rcp_no.isdigit():
        raise TerminalAuditError("source rcp_no must be exactly 14 digits")
    if report_class == "other":
        return {"action": "terminal", "item": {"rcp_no": rcp_no, "disposition": "rejected", "evidence_id": None,
                "economic_disposition": "negative_risk", "economic_reason": "non-supply DART filing is not a supply-contract candidate", "economic_facts": None}}
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