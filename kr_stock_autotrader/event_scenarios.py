"""Immutable, server-calculated event scenario sets; never creates trading rows."""
from __future__ import annotations
import json, math, sqlite3
from fastapi import HTTPException
from .decision_cards import canon, now
from .domain import now_kst, parse_kst

PROFILE_REGISTRY={name:{"version":1,"required":required} for name,required in {
 "SUPPLY_CONTRACT":("contract_amount_krw",), "LICENSE":("contract_amount_krw",),
 "APPROVAL":("contract_amount_krw",), "CAPACITY_EXPANSION":("contract_amount_krw",),
 "SHAREHOLDER_RETURN":("contract_amount_krw",), "PREFERRED_BIDDER":("contract_amount_krw",),
}.items()}
LABELS=("BAD","BASE","GOOD")
ACTIONS={"BAD_MATCH":"REDUCE_REVIEW","BASE_MATCH":"WATCH","GOOD_MATCH":"ENTRY_REVIEW","OUT_OF_RANGE":"NO_ACTION"}
def fail(message): raise HTTPException(422,message)
def number(value, name, positive=False, ratio=False):
    if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value) or (positive and value<=0): fail(f"invalid {name}")
    if ratio and not 0<=value<=1: fail(f"invalid {name}")
    return float(value)
def timestamp(value,name, ceiling=None):
    try: parsed=parse_kst(value)
    except (TypeError,ValueError): fail(f"invalid {name}")
    if ceiling and parsed>ceiling: fail(f"future {name}")
    return parsed
def scenario_value(item):
    confirmed=item.get("confirmed")
    option=item.get("option")
    if not isinstance(confirmed,dict) or not isinstance(option,dict): fail("missing valuation inputs")
    amount=number(confirmed.get("contract_amount_krw"),"contract_amount_krw",True)
    margin=number(confirmed.get("margin_pct"),"margin_pct",ratio=True); tax=number(confirmed.get("tax_rate"),"tax_rate",ratio=True)
    execution=number(confirmed.get("execution_probability"),"execution_probability",ratio=True); years=number(confirmed.get("period_years"),"period_years",True)
    discount=number(confirmed.get("discount_rate"),"discount_rate",ratio=True); wc=number(confirmed.get("working_capital_krw"),"working_capital_krw"); financing=number(confirmed.get("financing_cost_krw"),"financing_cost_krw")
    optional=number(option.get("value_krw"),"option.value_krw")
    if optional == 0 and not isinstance(option.get("reason"),str): fail("option zero requires reason")
    operating=amount/years*margin; net=(operating-wc-financing)*(1-tax)*execution
    supplied={"revenue_krw":amount/years,"operating_profit_krw":operating,"net_income_krw":net}
    for key, expected in supplied.items():
        actual=number(item.get(key),key)
        if not math.isclose(actual,expected,rel_tol=1e-9,abs_tol=1e-6): fail(f"derived mismatch {key}")
    return net/(1+discount)**years+optional
def validate(data):
    ceiling=now_kst(); disclosed=timestamp(data.get("disclosed_at"),"disclosed_at",ceiling); known=timestamp(data.get("known_at"),"known_at",ceiling)
    if known<disclosed: fail("known_at before disclosed_at")
    profile=data.get("profile") or {}; registered=PROFILE_REGISTRY.get(profile.get("id"))
    if not registered or profile.get("version")!=registered["version"] or data.get("event_type")!=profile.get("id"): fail("unknown profile version")
    if not all(isinstance(data.get(k),str) and data[k] for k in ("event_identity","symbol","source_url","source_receipt")): fail("missing identity/source")
    baseline=data.get("baseline") or {}
    for key in ("price_krw","shares_outstanding","market_cap_krw"):
        number(baseline.get(key),key,True)
    if baseline.get("denominator")!="shares_outstanding" or not isinstance(baseline.get("financial_as_of"),str): fail("invalid denominator")
    for key in ("price_known_at","shares_known_at","market_cap_known_at"):
        if timestamp(baseline.get(key),key,ceiling)>known: fail("baseline known after scenario")
    items=data.get("scenarios")
    if not isinstance(items,list) or len(items)!=3 or {x.get("label") for x in items if isinstance(x,dict)}!=set(LABELS): fail("exactly GOOD BASE BAD required")
    by={x["label"]:x for x in items}; probability=sum(number(by[label].get("probability"),"probability",ratio=True) for label in LABELS)
    if not math.isclose(probability,1.0,abs_tol=1e-9): fail("probabilities must sum to 1")
    values={label:scenario_value(by[label]) for label in LABELS}
    prices={label:number(by[label].get("per_share_value_krw"),"per_share_value_krw",True) for label in LABELS}
    if not (values["BAD"]<=values["BASE"]<=values["GOOD"] and prices["BAD"]<=prices["BASE"]<=prices["GOOD"]): fail("BAD BASE GOOD monotonicity")
    for item in items:
        if not isinstance(item.get("price_path"),dict) or not isinstance(item.get("gates"),dict): fail("missing price path/gates")
        for key in ("open","close","day_5","day_20"): number(item["price_path"].get(key),"price_path",True)
        for key in ("entry","add","reduce","withdraw"): number(item["gates"].get(key),"gate",True)
        if not all(isinstance(item.get(k),list) for k in ("assumptions","unknowns","invalidations")): fail("missing narrative conditions")
    return by,values,known
def detail(db, ident):
    row=db.execute("SELECT * FROM event_scenario_sets WHERE id=? OR event_identity=? ORDER BY id DESC LIMIT 1",(ident,ident)).fetchone()
    if not row: raise HTTPException(404,"scenario set not found")
    out=dict(row); out["scenario_set"]=json.loads(out.pop("scenario_json")); out["scenarios"]=json.loads(out.pop("scenarios_json")); out["observations"]=[dict(x) for x in db.execute("SELECT known_at,price_krw,benchmark_excess_pct,sector_excess_pct,volume_ratio,match,action FROM event_scenario_observations WHERE scenario_set_id=? ORDER BY id",(out["id"],))]; return out
def create(db,data):
    by,values,_=validate(data); frozen=now(); expected=sum(by[label]["probability"]*values[label] for label in LABELS)
    rendered=[]
    for label in LABELS:
        item={k:v for k,v in by[label].items() if k!="derived"}; item["server_value_krw"]=values[label]; rendered.append(item)
    try:
        row=db.execute("INSERT INTO event_scenario_sets(event_identity,version,symbol,event_type,profile_id,profile_version,evidence_id,card_id,disclosed_at,known_at,frozen_at,scenario_json,scenarios_json,expected_value_krw) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?) RETURNING id",(data["event_identity"],data["version"],data["symbol"],data["event_type"],data["profile"]["id"],data["profile"]["version"],data.get("evidence_id"),data.get("card_id"),data["disclosed_at"],data["known_at"],frozen,canon({k:v for k,v in data.items() if k!="scenarios"}),canon(rendered),expected)).fetchone()
    except sqlite3.IntegrityError: raise HTTPException(409,"immutable scenario identity/version exists")
    db.commit(); return detail(db,row["id"])
def observe(db, ident, data):
    scenario=detail(db,ident); known=timestamp(data.get("known_at"),"observation known_at",now_kst())
    if known<timestamp(scenario["frozen_at"],"frozen_at"): fail("observation before freeze")
    price=number(data.get("price_krw"),"price_krw",True); bench=number(data.get("benchmark_excess_pct"),"benchmark_excess_pct"); sector=number(data.get("sector_excess_pct"),"sector_excess_pct"); volume=number(data.get("volume_ratio"),"volume_ratio",True)
    p={x["label"]:x["per_share_value_krw"] for x in scenario["scenarios"]}
    match="OUT_OF_RANGE" if volume<1 or min(bench,sector)<-10 else ("BAD_MATCH" if price<=p["BAD"] else "GOOD_MATCH" if price>=p["GOOD"] else "BASE_MATCH")
    action=ACTIONS[match]
    db.execute("INSERT INTO event_scenario_observations(scenario_set_id,known_at,price_krw,benchmark_excess_pct,sector_excess_pct,volume_ratio,match,action) VALUES(?,?,?,?,?,?,?,?)",(scenario["id"],known.isoformat(),price,bench,sector,volume,match,action)); db.commit(); return {"scenario_set_id":scenario["id"],"match":match,"action":action}
