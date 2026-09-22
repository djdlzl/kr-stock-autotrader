"""Hostile regressions for exact-set terminal-audit planning."""

import pytest

from kr_stock_autotrader.giraffe_terminal_audit import TerminalAuditError, terminal_audit_batch, terminal_audit_plan


def _source(rcp_no, report_class="dart_single_sale_supply_contract", report_name="단일판매ㆍ공급계약체결"):
    return {"rcp_no": rcp_no, "report_class": report_class, "report_name": report_name, "rcept_dt": "20260917"}


def _non_supply(disposition="hold", economic_disposition="timing_unresolved"):
    return {"source_url": "https://dart.fss.or.kr/positive-filing", "economic_disposition": economic_disposition,
            "economic_reason": "official filing describes a potentially material positive corporate event", "disposition": disposition}


def test_non_supply_requires_source_grounded_audit_and_does_not_blanket_reject_positive_classes():
    for name in ("자기주식취득결정", "임상시험계획승인", "회사합병결정"):
        source = _source("20260917000001", "other", name)
        with pytest.raises(TerminalAuditError):
            terminal_audit_plan(source, {})
        plan = terminal_audit_plan(source, _non_supply())
        assert plan["item"]["disposition"] == "hold"
        assert plan["item"]["economic_disposition"] == "timing_unresolved"
        assert plan["item"]["economic_facts"] is None


def test_terminal_audit_batch_enforces_exact_set_order_and_excludes_timestamped_qualifiers():
    sources = [
        _source("20260917000001", "other", "자기주식취득결정"),
        _source("20260917000002"),
        _source("20260917000003"),
    ]
    qualifying = {"binding_contract": True, "contract_amount": 50, "prior_revenue": 100,
                  "ratio_percent": 50, "term": "2026-09-17 to 2027-09-16"}
    subthreshold = {"binding_contract": True, "contract_amount": 49, "prior_revenue": 100,
                    "ratio_percent": 49, "term": "2026-09-17 to 2027-09-16"}
    audits = [
        {"rcp_no": "20260917000001", "audit": _non_supply()},
        {"rcp_no": "20260917000002", "audit": {"economic_reason": "timestamped qualifying contract", "economic_facts": qualifying, "published_at": "2026-09-17T06:00:00+09:00"}},
        {"rcp_no": "20260917000003", "audit": {"economic_reason": "below threshold contract", "economic_facts": subthreshold}},
    ]
    result = terminal_audit_batch(sources, audits)
    assert [item["rcp_no"] for item in result["terminal_items"]] == ["20260917000001", "20260917000003"]
    assert [item["rcp_no"] for item in result["evidence_requirements"]] == ["20260917000002"]
    assert all(item["rcp_no"] != "20260917000002" for item in result["terminal_items"])
    assert result["evidence_requirements"][0]["published_at"] == "2026-09-17T06:00:00+09:00"
    for malformed in (
        audits[:-1],
        [*audits, {"rcp_no": "20260917009999", "audit": _non_supply()}],
        [*audits, audits[0]],
    ):
        with pytest.raises(TerminalAuditError):
            terminal_audit_batch(sources, malformed)


def test_terminal_audit_batch_rejects_duplicate_control_receipts():
    with pytest.raises(TerminalAuditError):
        terminal_audit_batch([_source("20260917000001", "other"), _source("20260917000001", "other")], {
            "20260917000001": _non_supply(),
        })
