"""Immutable, server-calculated event scenarios. This module never touches orders."""
from __future__ import annotations

import json
import math
import sqlite3
from dataclasses import dataclass
from typing import Any, Callable

from fastapi import HTTPException

from .decision_cards import canon, now
from .domain import now_kst, parse_kst

LABELS = ("BAD", "BASE", "GOOD")
TOP_KEYS = {"event_identity","version","symbol","event_type","profile","disclosed_at","known_at","source_url","source_receipt","evidence_id","card_id","baseline","scenarios"}
BASELINE_KEYS = {"price_krw","price_known_at","shares_outstanding","shares_known_at","market_cap_krw","market_cap_known_at","financial_as_of","denominator"}
PROFILE_KEYS = {"id","version"}
ITEM_KEYS = {"label","probability","confirmed","assumptions","unknowns","invalidations"}
PRIMITIVES = {"SUPPLY_CONTRACT":{"amount_krw","duration_years","margin_pct","tax_rate","execution_probability","discount_rate","working_capital_krw","financing_cost_krw","follow_on"},"LICENSE":{"upfront_krw","tax_rate","discount_rate","milestones","royalty"},"APPROVAL":{"addressable_sales_krw","share_pct","margin_pct","commercialization_probability","years","launch_cost_krw","tax_rate","discount_rate"},"CAPACITY_EXPANSION":{"units","utilization_pct","unit_price_krw","margin_pct","years","capex_krw","financing_cost_krw","tax_rate","discount_rate"},"SHAREHOLDER_RETURN":{"shares_cancelled","buyback_cost_krw","dividend_increment_per_share_krw","sustainable_years","baseline_shares_outstanding","tax_rate","discount_rate"},"PREFERRED_BIDDER":{"expected_contract_krw","award_probability","margin_pct","tax_rate","years","discount_rate","bid_cost_krw"}}
NESTED = {"follow_on":{"amount_krw","probability","margin_pct","years"},"milestone":{"amount_krw","probability","due_years"},"royalty":{"annual_sales_krw","rate","probability","years"}}
DERIVED_KEYS = {"revenue_krw", "operating_profit_krw", "net_income_krw", "eps_krw", "per_share_value_krw", "return_range_pct", "price_path", "gates", "derived"}


def fail(message: str) -> None:
    raise HTTPException(422, message)


def number(value: Any, name: str, *, positive: bool = False, nonnegative: bool = False, ratio: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        fail(f"invalid {name}")
    result = float(value)
    if positive and result <= 0 or nonnegative and result < 0 or ratio and not 0 <= result <= 1:
        fail(f"invalid {name}")
    return result


def positive_integer(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0: fail(f"invalid {name}")
    return value


def exact(value: Any, name: str, keys: set[str]) -> dict[str, Any]:
    result = mapping(value, name)
    if set(result) != keys: fail(f"invalid {name} keys")
    return result


def strings(value: Any, name: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value): fail(f"invalid {name}")
    return value


def ts(value: Any, name: str, ceiling=None):
    try:
        result = parse_kst(value)
    except (TypeError, ValueError):
        fail(f"invalid {name}")
    if ceiling is not None and result > ceiling:
        fail(f"future {name}")
    return result


def mapping(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        fail(f"invalid {name}")
    return value


def text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        fail(f"invalid {name}")
    return value.strip()


def money(d: dict[str, Any], key: str, *, positive: bool = False, nonnegative: bool = False) -> float:
    return number(d.get(key), key, positive=positive, nonnegative=nonnegative)


@dataclass(frozen=True)
class Calculation:
    revenue: float
    operating: float
    net: float
    equity_value: float
    formula: str
    units: str = "KRW"
    policy_version: int = 1


def _supply(c: dict[str, Any]) -> Calculation:
    amount = money(c, "amount_krw", positive=True)
    years = positive_integer(c.get("duration_years"), "duration_years")
    margin, tax, execution, discount = (number(c.get(k), k, ratio=True) for k in ("margin_pct", "tax_rate", "execution_probability", "discount_rate"))
    wc, financing = money(c, "working_capital_krw", nonnegative=True), money(c, "financing_cost_krw", nonnegative=True)
    follow = mapping(c.get("follow_on"), "follow_on")
    f_amount = money(follow, "amount_krw", nonnegative=True)
    f_probability = number(follow.get("probability"), "follow_on.probability", ratio=True)
    f_margin = number(follow.get("margin_pct"), "follow_on.margin_pct", ratio=True)
    f_years = positive_integer(follow.get("years"), "follow_on.years")
    revenue, operating = amount / years, amount / years * margin
    net = (operating - wc - financing) * (1 - tax) * execution
    option = f_amount / f_years * f_margin * (1 - tax) * f_probability / (1 + discount) ** f_years
    return Calculation(revenue, operating, net, net / (1 + discount) ** years + option, "SUPPLY_CONTRACT_NPV_V1")


def _license(c: dict[str, Any]) -> Calculation:
    upfront, tax, discount = money(c, "upfront_krw", nonnegative=True), number(c.get("tax_rate"), "tax_rate", ratio=True), number(c.get("discount_rate"), "discount_rate", ratio=True)
    milestones = c.get("milestones")
    if not isinstance(milestones, list) or not milestones: fail("invalid milestones")
    milestone_value = 0.0
    for item in milestones:
        item = mapping(item, "milestone")
        milestone_value += money(item, "amount_krw", nonnegative=True) * number(item.get("probability"), "milestone.probability", ratio=True) / (1 + discount) ** positive_integer(item.get("due_years"), "milestone.due_years")
    royalty = mapping(c.get("royalty"), "royalty")
    sales, rate, probability, years = money(royalty, "annual_sales_krw", nonnegative=True), number(royalty.get("rate"), "royalty.rate", ratio=True), number(royalty.get("probability"), "royalty.probability", ratio=True), positive_integer(royalty.get("years"), "royalty.years")
    royalty_value = sum(sales * rate * probability / (1 + discount) ** year for year in range(1, years + 1))
    revenue, operating = upfront + milestone_value + sales * rate * probability, sales * rate * probability
    net, equity = operating * (1 - tax), (upfront + milestone_value + royalty_value) * (1 - tax)
    return Calculation(revenue, operating, net, equity, "LICENSE_V1")


def _approval(c: dict[str, Any]) -> Calculation:
    sales, share, margin, probability, years, launch = money(c, "addressable_sales_krw", positive=True), number(c.get("share_pct"), "share_pct", ratio=True), number(c.get("margin_pct"), "margin_pct", ratio=True), number(c.get("commercialization_probability"), "commercialization_probability", ratio=True), positive_integer(c.get("years"), "years"), money(c, "launch_cost_krw", nonnegative=True)
    tax, discount = number(c.get("tax_rate"), "tax_rate", ratio=True), number(c.get("discount_rate"), "discount_rate", ratio=True)
    annual = sales * share * margin * probability
    equity = sum(annual * (1 - tax) / (1 + discount) ** year for year in range(1, years + 1)) - launch
    return Calculation(sales * share * probability, annual, annual * (1 - tax), equity, "APPROVAL_V1")


def _capacity(c: dict[str, Any]) -> Calculation:
    units, utilization, price, margin, years = number(c.get("units"), "units", positive=True), number(c.get("utilization_pct"), "utilization_pct", ratio=True), money(c, "unit_price_krw", positive=True), number(c.get("margin_pct"), "margin_pct", ratio=True), positive_integer(c.get("years"), "years")
    capex, financing, tax, discount = money(c, "capex_krw", nonnegative=True), money(c, "financing_cost_krw", nonnegative=True), number(c.get("tax_rate"), "tax_rate", ratio=True), number(c.get("discount_rate"), "discount_rate", ratio=True)
    revenue, operating = units * utilization * price, units * utilization * price * margin
    equity = sum(operating * (1 - tax) / (1 + discount) ** year for year in range(1, years + 1)) - capex - financing
    return Calculation(revenue, operating, operating * (1 - tax), equity, "CAPACITY_EXPANSION_V1")


def _shareholder(c: dict[str, Any]) -> Calculation:
    cancelled, buyback, dividend, years, baseline = number(c.get("shares_cancelled"), "shares_cancelled", nonnegative=True), money(c, "buyback_cost_krw", nonnegative=True), money(c, "dividend_increment_per_share_krw", nonnegative=True), positive_integer(c.get("sustainable_years"), "sustainable_years"), number(c.get("baseline_shares_outstanding"), "baseline_shares_outstanding", positive=True)
    tax, discount = number(c.get("tax_rate"), "tax_rate", ratio=True), number(c.get("discount_rate"), "discount_rate", ratio=True)
    if cancelled >= baseline: fail("shares_cancelled must be below baseline")
    annual = dividend * (baseline - cancelled)
    equity = sum(annual * (1 - tax) / (1 + discount) ** year for year in range(1, years + 1)) - buyback
    return Calculation(0, 0, annual * (1 - tax), equity, "SHAREHOLDER_RETURN_V1")


def _preferred(c: dict[str, Any]) -> Calculation:
    contract, probability, margin, tax, years, discount, bid = money(c, "expected_contract_krw", positive=True), number(c.get("award_probability"), "award_probability", ratio=True), number(c.get("margin_pct"), "margin_pct", ratio=True), number(c.get("tax_rate"), "tax_rate", ratio=True), positive_integer(c.get("years"), "years"), number(c.get("discount_rate"), "discount_rate", ratio=True), money(c, "bid_cost_krw", nonnegative=True)
    revenue, operating = contract / years * probability, contract / years * probability * margin
    return Calculation(revenue, operating, operating * (1 - tax), operating * (1 - tax) / (1 + discount) ** years - bid, "PREFERRED_BIDDER_V1")


CALCULATORS: dict[str, Callable[[dict[str, Any]], Calculation]] = {"SUPPLY_CONTRACT": _supply, "LICENSE": _license, "APPROVAL": _approval, "CAPACITY_EXPANSION": _capacity, "SHAREHOLDER_RETURN": _shareholder, "PREFERRED_BIDDER": _preferred}
POLICY_BANDS = {"BAD": (0.92, 0.98), "BASE": (0.98, 1.02), "GOOD": (1.02, 1.10)}


def scenario_value(item: dict[str, Any]) -> float:
    """Compatibility helper used by focused calculator tests."""
    profile = mapping(item.get("profile"), "profile")
    calculator = CALCULATORS.get(profile.get("id"))
    if profile.get("version") != 1 or calculator is None: fail("unknown profile version")
    if DERIVED_KEYS.intersection(item): fail("client derived fields forbidden")
    return calculator(mapping(item.get("confirmed"), "confirmed")).equity_value


def _range(center: float, band: tuple[float, float]) -> dict[str, float]:
    return {"low": center * band[0], "high": center * band[1]}


def validate(data: dict[str, Any]):
    if not isinstance(data, dict) or set(data) != TOP_KEYS: fail("invalid scenario set")
    ceiling = now_kst(); disclosed, known = ts(data.get("disclosed_at"), "disclosed_at", ceiling), ts(data.get("known_at"), "known_at", ceiling)
    if known < disclosed: fail("known_at before disclosed_at")
    if isinstance(data.get("version"), bool) or not isinstance(data.get("version"), int) or data["version"] <= 0: fail("invalid version")
    profile = exact(data.get("profile"), "profile", PROFILE_KEYS); profile_id = profile.get("id")
    if profile.get("version") != 1 or profile_id not in CALCULATORS or data.get("event_type") != profile_id: fail("unknown profile version")
    for key in ("event_identity", "symbol", "source_url", "source_receipt"): text(data.get(key), key)
    baseline = exact(data.get("baseline"), "baseline", BASELINE_KEYS)
    price, shares, cap = number(baseline.get("price_krw"), "price_krw", positive=True), number(baseline.get("shares_outstanding"), "shares_outstanding", positive=True), number(baseline.get("market_cap_krw"), "market_cap_krw", positive=True)
    if not math.isclose(cap, price * shares, rel_tol=1e-6, abs_tol=max(1.0, price * shares * 1e-6)): fail("baseline market cap mismatch")
    if baseline.get("denominator") != "shares_outstanding" or not isinstance(baseline.get("financial_as_of"), str): fail("invalid denominator")
    for key in ("price_known_at", "shares_known_at", "market_cap_known_at"):
        if ts(baseline.get(key), key, ceiling) > known: fail("baseline known after scenario")
    items = data.get("scenarios")
    if not isinstance(items, list) or len(items) != 3 or {x.get("label") for x in items if isinstance(x, dict)} != set(LABELS): fail("exactly GOOD BASE BAD required")
    by = {x["label"]: x for x in items}; probability = sum(number(by[label].get("probability"), "probability", ratio=True) for label in LABELS)
    if not math.isclose(probability, 1.0, abs_tol=1e-9): fail("probabilities must sum to 1")
    rendered, values = [], {}
    for label in LABELS:
        item = exact(by[label], "scenario", ITEM_KEYS)
        if DERIVED_KEYS.intersection(item): fail("client derived fields forbidden")
        for narrative in ("assumptions", "unknowns", "invalidations"): strings(item.get(narrative), narrative)
        confirmed = exact(item.get("confirmed"), "confirmed", PRIMITIVES[profile_id])
        if profile_id == "SUPPLY_CONTRACT": exact(confirmed["follow_on"], "follow_on", NESTED["follow_on"])
        if profile_id == "LICENSE":
            if not isinstance(confirmed["milestones"], list) or not confirmed["milestones"]: fail("invalid milestones")
            for milestone in confirmed["milestones"]: exact(milestone, "milestone", NESTED["milestone"])
            exact(confirmed["royalty"], "royalty", NESTED["royalty"])
        calc = CALCULATORS[profile_id](confirmed)
        if profile_id == "SHAREHOLDER_RETURN" and not math.isclose(item["confirmed"]["baseline_shares_outstanding"], shares, rel_tol=1e-9): fail("shareholder baseline shares mismatch")
        center = price + calc.equity_value / shares
        result = {"label": label, "probability": number(item.get("probability"), "probability", ratio=True), "inputs": item["confirmed"], "assumptions": item["assumptions"], "unknowns": item["unknowns"], "invalidations": item["invalidations"], "calculator": calc.formula, "formula": calc.formula, "units": calc.units, "policy_version": calc.policy_version, "total_equity_value_krw": calc.equity_value, "annualized_revenue_krw": calc.revenue, "annualized_operating_profit_krw": calc.operating, "annualized_net_income_krw": calc.net, "eps_krw": calc.net / shares, "per_share_value_range_krw": _range(center, POLICY_BANDS[label])}
        result["return_range_pct"] = {key: (value / price - 1) * 100 for key, value in result["per_share_value_range_krw"].items()}
        result["price_path"] = {horizon: dict(result["per_share_value_range_krw"]) for horizon in ("open", "close", "day_5", "day_20")}
        rendered.append(result); values[label] = calc.equity_value
    for key in ("total_equity_value_krw", "per_share_value_range_krw"):
        points = [values[label] if key.startswith("total") else next(x for x in rendered if x["label"] == label)[key]["low"] for label in LABELS]
        if not points[0] <= points[1] <= points[2]: fail("BAD BASE GOOD monotonicity")
    return rendered, values, known


def _lineage(db, data: dict[str, Any]) -> None:
    if not isinstance(data.get("card_id"), int) or not isinstance(data.get("evidence_id"), int): fail("card_id and evidence_id required")
    row = db.execute("""SELECT c.evidence_id, c.invalidated_at AS card_invalidated, e.symbol, e.status, e.invalidated_at AS evidence_invalidated
                     FROM decision_cards c JOIN material_evidence e ON e.id=c.evidence_id WHERE c.id=?""", (data["card_id"],)).fetchone()
    if not row or row["evidence_id"] != data["evidence_id"] or row["symbol"] != data["symbol"]: fail("card/evidence/symbol lineage mismatch")
    if row["card_invalidated"] or row["evidence_invalidated"] or row["status"] == "invalidated": fail("invalidated lineage")


def detail_by_id(db, ident):
    row = db.execute("SELECT * FROM event_scenario_sets WHERE id=?", (ident,)).fetchone()
    if not row: raise HTTPException(404, "scenario set not found")
    out=dict(row); out["scenario_set"]=json.loads(out.pop("scenario_json")); out["scenarios"]=json.loads(out.pop("scenarios_json")); out["observations"]= [dict(x) for x in db.execute("SELECT * FROM event_scenario_observations WHERE scenario_set_id=? ORDER BY known_at",(out["id"],))]; return out

def detail_by_event_identity(db, identity):
    row=db.execute("SELECT id FROM event_scenario_sets WHERE event_identity=? ORDER BY id DESC LIMIT 1",(identity,)).fetchone()
    if not row: raise HTTPException(404,"scenario set not found")
    return detail_by_id(db,row["id"])

def detail(db, ident): return detail_by_id(db,ident)

def create(db, data):
    rendered,values,_=validate(data); _lineage(db,data); frozen,expected=now(),sum(next(x["probability"] for x in rendered if x["label"]==label)*values[label] for label in LABELS)
    try: row=db.execute("INSERT INTO event_scenario_sets(event_identity,version,symbol,event_type,profile_id,profile_version,evidence_id,card_id,disclosed_at,known_at,frozen_at,scenario_json,scenarios_json,expected_value_krw) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?) RETURNING id",(data["event_identity"],data["version"],data["symbol"],data["event_type"],data["profile"]["id"],1,data["evidence_id"],data["card_id"],data["disclosed_at"],data["known_at"],frozen,canon({k:v for k,v in data.items() if k!="scenarios"}),canon(rendered),expected)).fetchone()
    except sqlite3.IntegrityError: raise HTTPException(409,"immutable scenario identity/version exists")
    db.commit(); return detail_by_id(db,row["id"])

OBSERVATION_KEYS = {
    "provider", "source", "source_receipt", "symbol", "idempotency_key", "known_at",
    "retrieved_at", "price_krw", "best_bid", "best_ask", "top_bid_qty",
    "top_ask_qty", "volume_ratio", "benchmark_excess_pct", "sector_excess_pct",
    "market_context_status",
}
TRUSTED_PROVENANCE = {
    ("KIS", "KIS"): "KIS:",
    ("TRUSTED_MARKET_CONTEXT", "TRUSTED_MARKET_CONTEXT"): "MARKET_CONTEXT:",
}


def observe(db, identity, data):
    if not isinstance(data, dict) or set(data) != OBSERVATION_KEYS:
        fail("invalid observation fields")
    scenario = detail_by_event_identity(db, identity)
    if scenario.get("scenario_kind") == "CONDITIONAL":
        raise HTTPException(409, "conditional scenario sets are not observable")
    frozen = ts(scenario["frozen_at"], "frozen_at")
    known = ts(data.get("known_at"), "observation known_at", now_kst())
    retrieved = ts(data.get("retrieved_at"), "retrieved_at", now_kst())
    if known < frozen or retrieved < known:
        fail("invalid observation chronology")
    provider, source, symbol, key, receipt = (
        text(data.get(field), field)
        for field in ("provider", "source", "symbol", "idempotency_key", "source_receipt")
    )
    receipt_prefix = TRUSTED_PROVENANCE.get((provider, source))
    if receipt_prefix is None or not receipt.startswith(receipt_prefix) or symbol != scenario["symbol"]:
        fail("untrusted observation provenance")
    status = data.get("market_context_status")
    if status not in {"UNAVAILABLE", "VERIFIED"}:
        fail("invalid market_context_status")
    if provider == "KIS" and status != "UNAVAILABLE":
        fail("KIS market context unavailable")
    price, bid, ask, bid_qty, ask_qty = (
        number(data.get(field), field, positive=True)
        for field in ("price_krw", "best_bid", "best_ask", "top_bid_qty", "top_ask_qty")
    )
    if bid > ask:
        fail("best_bid above best_ask")
    spread = (ask - bid) / price * 100
    imbalance = (bid_qty - ask_qty) / (bid_qty + ask_qty)
    volume, bench, sector = (
        number(data.get(field), field, positive=field == "volume_ratio")
        for field in ("volume_ratio", "benchmark_excess_pct", "sector_excess_pct")
    )
    material = canon({
        "provider": provider, "source": source, "source_receipt": receipt, "symbol": symbol,
        "known_at": known.isoformat(), "retrieved_at": retrieved.isoformat(), "price_krw": price,
        "best_bid": bid, "best_ask": ask, "top_bid_qty": bid_qty, "top_ask_qty": ask_qty,
        "spread_pct": spread, "imbalance": imbalance, "volume_ratio": volume,
        "benchmark_excess_pct": bench, "sector_excess_pct": sector,
        "market_context_status": status,
    })
    existing = db.execute(
        "SELECT match,action,active_scenario_label,material_hash FROM event_scenario_observations "
        "WHERE scenario_set_id=? AND idempotency_key=?", (scenario["id"], key)
    ).fetchone()
    if existing:
        if existing["material_hash"] != material:
            raise HTTPException(409, "idempotency key payload mismatch")
        return {"scenario_set_id": scenario["id"], **dict(existing), "idempotent": True}
    latest = db.execute(
        "SELECT known_at FROM event_scenario_observations WHERE scenario_set_id=? ORDER BY known_at DESC LIMIT 1",
        (scenario["id"],),
    ).fetchone()
    if latest and known <= ts(latest["known_at"], "latest known_at"):
        fail("observation must be strictly newer")
    invalidated = db.execute(
        "SELECT 1 FROM decision_cards c JOIN material_evidence e ON e.id=c.evidence_id "
        "WHERE c.id=? AND (c.invalidated_at IS NOT NULL OR e.invalidated_at IS NOT NULL OR e.status='invalidated')",
        (scenario["card_id"],),
    ).fetchone()
    if invalidated:
        match, action, active = "OUT_OF_RANGE", "EXIT_REVIEW", None
    elif status == "UNAVAILABLE":
        match, action, active = "OUT_OF_RANGE", "NO_ACTION", None
    else:
        ranges = {item["label"]: item["per_share_value_range_krw"] for item in scenario["scenarios"]}
        candidates = [label for label in LABELS if ranges[label]["low"] <= price <= ranges[label]["high"]]
        active = candidates[0] if candidates else None
        if active == "BAD":
            match, action = "BAD_MATCH", "REDUCE_REVIEW"
        elif active == "BASE":
            match, action = "BASE_MATCH", "WATCH"
        elif active == "GOOD":
            match, action = "GOOD_MATCH", "ADD_REVIEW" if volume >= 2 and min(bench, sector) >= 5 else "ENTRY_REVIEW"
        else:
            match, action = "OUT_OF_RANGE", "NO_ACTION"
    db.execute(
        """INSERT INTO event_scenario_observations(
            scenario_set_id,known_at,retrieved_at,provider,source,source_receipt,symbol,idempotency_key,price_krw,
            best_bid,best_ask,spread_pct,imbalance,volume_ratio,benchmark_excess_pct,
            sector_excess_pct,market_context_status,material_hash,match,action,active_scenario_label
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (scenario["id"], known.isoformat(), retrieved.isoformat(), provider, source, receipt, symbol, key,
         price, bid, ask, spread, imbalance, volume, bench, sector, status, material, match, action, active),
    )
    db.commit()
    return {"scenario_set_id": scenario["id"], "match": match, "action": action, "active_scenario_label": active, "idempotent": False}
