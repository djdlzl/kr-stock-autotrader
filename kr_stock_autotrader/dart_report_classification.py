"""Authoritative OpenDART report-name classification."""

from __future__ import annotations

_SINGLE_SALE_SUPPLY_CONTRACT = "단일판매공급계약체결"
_CORRECTION_PREFIX = "[기재정정]"


def authoritative_report_class(report_name: object) -> str | None:
    """Classify the exact standard title, with one bounded leading correction marker."""
    if not isinstance(report_name, str) or not 0 < len(report_name) <= 500:
        return None
    core = report_name.removeprefix(_CORRECTION_PREFIX)
    normalized = "".join(char for char in core if not char.isspace() and char not in {"ㆍ", "·", "/"})
    return "dart_single_sale_supply_contract" if normalized == _SINGLE_SALE_SUPPLY_CONTRACT else "other"


def is_correction_single_sale_supply_contract(report_name: object) -> bool:
    """True only for the one authoritative correction form of the standard title."""
    return (
        isinstance(report_name, str)
        and report_name.startswith(_CORRECTION_PREFIX)
        and authoritative_report_class(report_name) == "dart_single_sale_supply_contract"
    )
