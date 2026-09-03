"""Immutable, fail-closed decision-card domain (paper only)."""
from __future__ import annotations
import hashlib, hmac, json, math, os, sqlite3
from datetime import timedelta
from pathlib import Path
from fastapi import Header, HTTPException
from .decision_card_schema import SCHEMA_VERSION, validate_card
from .domain import fresh_quote, market_open, now_kst, parse_kst

PROMPT_PATH = Path(__file__).parents[1] / "prompts" / "decision-card-v1.md"
SHORT_TERM_RISE_SESSIONS = 2
SHORT_TERM_EXCESS_PRECISION = 8
SHORT_TERM_EXCESS_TOLERANCE = 0.5 * 10 ** -SHORT_TERM_EXCESS_PRECISION
def prompt_hash(): return hashlib.sha256(PROMPT_PATH.read_bytes()).hexdigest()
def canon(x): return json.dumps(x, ensure_ascii=False, sort_keys=True, separators=(",",":"))
def now(): return now_kst().isoformat()
def audit(db, actor, action, typ, ident, detail=""):
    db.execute("INSERT INTO audit_logs(actor,action,entity_type,entity_id,detail,at) VALUES(?,?,?,?,?,?)", (actor,action,typ,str(ident),detail,now()))
def require_internal_api_key(x_internal_api_key: str|None=Header(None), x_internal_key: str|None=Header(None)):
    expected, supplied = os.getenv("INTERNAL_API_KEY", ""), x_internal_api_key or x_internal_key
    if not expected or not supplied or not hmac.compare_digest(supplied, expected): raise HTTPException(403, "internal authorization required")
def row(db, table, ident):
    found=db.execute(f"SELECT * FROM {table} WHERE id=?", (ident,)).fetchone()
    if not found: raise HTTPException(404, f"{table} not found")
    return found

def create_evidence(db, data):
    ts=now()
    try:
        r=db.execute("""INSERT INTO material_evidence(symbol,name,kind,title,summary,source,source_url,announcement_at,collected_at,known_at,snapshot,newness,dedupe_key,status,created_by,updated_at,audit_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,'new',?,?,?) RETURNING id""", (data["symbol"],data.get("name"),data["kind"],data["title"],data["summary"],data["source"],data.get("source_url"),data.get("announcement_at"),data.get("collected_at",ts),data["known_at"],canon(data["snapshot"]),data.get("newness","new"),data["dedupe_key"],data.get("created_by","internal"),ts,"[]")).fetchone()
    except sqlite3.IntegrityError: raise HTTPException(409,"duplicate evidence dedupe_key")
    audit(db,data.get("created_by","internal"),"create","material_evidence",r["id"]); db.commit(); return evidence_detail(db,r["id"])
def evidence_detail(db, ident):
    d=dict(row(db,"material_evidence",ident)); d["snapshot"]=json.loads(d["snapshot"]); return d
def missing_evidence_view(db, ident):
    """Return only actionable display fields; raw collection data stays internal."""
    evidence=evidence_detail(db, ident)
    safe={k:evidence.get(k) for k in ("id","symbol","name","kind","title","summary","source","source_url","announcement_at","collected_at","known_at","status","version")}
    safe["snapshot_available"]=bool(evidence.get("snapshot"))
    return safe
def list_evidence(db, symbol=None,status=None, date=None):
    q="SELECT id FROM material_evidence WHERE 1=1"; p=[]
    if symbol: q+=" AND symbol=?"; p.append(symbol)
    if status: q+=" AND status=?"; p.append(status)
    if date: q+=" AND substr(known_at,1,10)=?"; p.append(date)
    return [evidence_detail(db,x["id"]) for x in db.execute(q+" ORDER BY id DESC",p)]
def invalidate_lineage(db,evidence_id=None,card_id=None,reason="mutation"):
    """Atomically revoke every entry-capable plan but retain open positions for exit."""
    ids=[x["id"] for x in db.execute("SELECT id FROM decision_cards WHERE "+("evidence_id=?" if evidence_id else "id=?"),(evidence_id or card_id,))]
    for cid in ids:
        db.execute("UPDATE decision_cards SET invalidated_at=?,invalidation_reason=? WHERE id=?",(now(),reason,cid))
        db.execute("""UPDATE order_plans SET status=CASE WHEN EXISTS(
            SELECT 1 FROM positions p WHERE p.order_plan_id=order_plans.id AND p.qty>0
          ) THEN 'review_required' ELSE 'entry_invalidated' END
          WHERE card_id=? AND status IN ('approved','execution_wait','partial_or_filled','exit_wait')""",(cid,))
def mutate_evidence(db, ident, patch=None, invalidate=False):
    ts=now()
    status = "invalidated" if invalidate else (patch or {}).get("status")
    if status is not None and status not in {"new", "card_generated", "decision_pending", "error", "invalidated"}:
        raise HTTPException(422,"invalid evidence status")
    changed = False
    if status == "invalidated":
        db.execute("UPDATE material_evidence SET status='invalidated',invalidated_at=?,updated_at=?,version=version+1 WHERE id=?",(ts,ts,ident))
    elif status is not None:
        db.execute("UPDATE material_evidence SET status=?,updated_at=? WHERE id=?", (status,ts,ident))
    for k,v in (patch or {}).items():
        if k in {"title","summary","snapshot","newness","known_at","announcement_at"}:
            db.execute(f"UPDATE material_evidence SET {k}=?,updated_at=?,version=version+1 WHERE id=?", (canon(v) if k=="snapshot" else v,ts,ident)); changed=True
        elif k != "status":
            raise HTTPException(422,"no mutable evidence fields")
    if not invalidate and status is None and not changed: raise HTTPException(422,"no mutable evidence fields")
    revokes_entry = status == "invalidated" or changed
    if revokes_entry: invalidate_lineage(db,evidence_id=ident,reason="evidence_invalidated" if status == "invalidated" else "evidence_mutated")
    audit(db,"internal","invalidate" if revokes_entry else "status","material_evidence",ident,status or "update")
    db.commit();return evidence_detail(db,ident)

def _num(inputs, key, reasons, positive=False):
    value=inputs.get(key)
    if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value) or (positive and value<=0): reasons.append(f"missing/invalid {key}"); return None
    return float(value)
def run_filter(inputs, as_of, known_at):
    reasons=[]
    try: asdt,kdt=parse_kst(as_of),parse_kst(known_at)
    except (ValueError,TypeError): return {"verdict":"FAIL","reasons":["invalid timestamp"],"computed":{},"units":{}}
    if kdt>asdt or asdt-kdt>timedelta(days=1): reasons.append("known_at future or stale")
    # An unavailable snapshot has only a retrieval-attempt provenance.  It is
    # intentionally not a market observation and therefore has no
    # market_data_known_at.  Keep this narrow so ordinary incomplete inputs
    # remain fail-closed under the normal market-data contract.
    if inputs.get("market_data_status") == "unavailable":
        try:
            attempted_at=parse_kst(inputs.get("market_data_attempted_at"))
            if attempted_at > kdt: reasons.append("market data attempted_at future of filter known_at")
            if attempted_at > asdt: reasons.append("market data attempted_at future of as_of")
        except (ValueError, TypeError):
            reasons.append("missing/invalid market_data_attempted_at")
        reasons.append("market data unavailable")
        return {"verdict":"FAIL","reasons":reasons,"computed":{},"units":{"as_of":"KST ISO-8601; unavailable snapshots use market_data_attempted_at <= filter.known_at <= filter.as_of and do not claim market_data_known_at"}}
    try:
        market_known_at=parse_kst(inputs.get("market_data_known_at"))
        if market_known_at>asdt: reasons.append("market data known_at future of as_of")
        if market_known_at>kdt: reasons.append("market data known_at future of filter known_at")
    except (ValueError, TypeError):
        reasons.append("missing/invalid market_data_known_at")
    observable=(inputs.get("observability") if isinstance(inputs.get("observability"),dict) else {})
    optional_premarket={key for key in ("current_volume","baseline_volume","gap_pct") if observable.get(key)=="not_yet_observable"}
    optional_sector=observable.get("sector_return_pct")=="unknown"
    optional_pre_return=observable.get("pre_announcement_return_pct")=="unknown"
    numbers={k:_num(inputs,k,reasons,positive=k in {"current_volume","baseline_volume","trading_value","market_cap"}) for k in ("stock_return_pct","benchmark_return_pct","sector_return_pct","current_volume","baseline_volume","trading_value","market_cap","recent_rise_pct","gap_pct","pre_announcement_return_pct") if k not in optional_premarket and not (k=="sector_return_pct" and optional_sector) and not (k=="pre_announcement_return_pct" and optional_pre_return)}
    for key in optional_premarket | ({"sector_return_pct"} if optional_sector else set()) | ({"pre_announcement_return_pct"} if optional_pre_return else set()): numbers[key]=None
    for k in ("min_trading_value","min_market_cap","max_market_cap","max_recent_rise_pct","max_gap_pct","max_pre_return_pct"): _num(inputs,k,reasons,positive=k in {"min_trading_value","min_market_cap","max_market_cap"})
    # KIS daily history has no authoritatively verified pre-market halt field.
    # Only this exact explicit observability contract bypasses the tradability gate.
    if not (inputs.get("trading_status")=="premarket_unverified" and observable.get("trading_status")=="premarket_unverified") and inputs.get("trading_status")!="tradable": reasons.append("not tradable")
    if numbers["trading_value"] is not None and numbers["trading_value"] < inputs.get("min_trading_value",float("inf")): reasons.append("trading value below minimum")
    if numbers["market_cap"] is not None and not (inputs.get("min_market_cap",float("inf")) <= numbers["market_cap"] <= inputs.get("max_market_cap",float("-inf"))): reasons.append("market cap outside range")
    for actual, limit, label in ((numbers["recent_rise_pct"],inputs.get("max_recent_rise_pct"),"recent rise"),(numbers["gap_pct"],inputs.get("max_gap_pct"),"gap"),(numbers["pre_announcement_return_pct"],inputs.get("max_pre_return_pct"),"pre-return")):
        if actual is not None and isinstance(limit,(int,float)) and actual>limit: reasons.append(label+" too high")
    is_v2 = inputs.get("filter_config_version") == "giraffe-premarket-filter-v2-short-term-priced-in"
    expected_short_term_excess = None
    if is_v2:
        max_short_term_excess = _num(inputs, "max_short_term_excess_rise_pct", reasons, positive=True)
        short_stock_return = _num(inputs, "short_term_stock_return_pct", reasons)
        short_benchmark_return = _num(inputs, "short_term_benchmark_return_pct", reasons)
        supplied_short_term_excess = _num(inputs, "short_term_excess_return_pct", reasons)
        window, provenance = inputs.get("short_term_window"), inputs.get("short_term_provenance")
        if not isinstance(window, dict) or window.get("sessions") != inputs.get("short_term_rise_sessions") or not isinstance(window.get("start"), str) or not isinstance(window.get("end"), str):
            reasons.append("missing/invalid short-term window")
        if inputs.get("short_term_rise_sessions") != SHORT_TERM_RISE_SESSIONS:
            reasons.append("short-term session window must be exactly two completed sessions")
        try:
            ended_known = parse_kst(window.get("ended_known_at"))
            if ended_known > kdt or ended_known > asdt: reasons.append("short-term window known_at future")
        except (AttributeError, ValueError, TypeError):
            reasons.append("missing/invalid short-term window known_at")
        try:
            daily_known = parse_kst(provenance.get("daily_bars_known_at"))
            if provenance.get("source") != "KIS" or daily_known > kdt or daily_known > asdt: reasons.append("missing/invalid short-term provenance")
        except (AttributeError, ValueError, TypeError):
            reasons.append("missing/invalid short-term provenance")
        if short_stock_return is not None and short_benchmark_return is not None:
            expected_short_term_excess = round(short_stock_return - short_benchmark_return, SHORT_TERM_EXCESS_PRECISION)
            if supplied_short_term_excess is not None and abs(supplied_short_term_excess - expected_short_term_excess) > SHORT_TERM_EXCESS_TOLERANCE:
                reasons.append("short-term excess return mismatch")
        if expected_short_term_excess is not None and max_short_term_excess is not None and expected_short_term_excess > max_short_term_excess:
            reasons.append("short-term excess rise too high")
    elif inputs.get("filter_config_version") not in (None, "giraffe-premarket-filter-v1-frozen"):
        reasons.append("unknown filter config version")
    low=str(inputs.get("low_certainty_terms","")).lower()
    if any(x in low for x in ("mou","검토","신청")): reasons.append("low certainty term")
    if any(inputs.get(x) is True for x in ("duplicate","recycled","conflicting_bad_news","conflicting_financing","cb","유상증자")): reasons.append("conflicting or recycled disclosure")
    if not all(inputs.get(k) for k in ("source","announcement_at","economic_terms")): reasons.append("source/announcement/economic terms missing")
    volume_ratio=None if numbers["current_volume"] is None or numbers["baseline_volume"] is None else numbers["current_volume"]/numbers["baseline_volume"]
    computed={"stock_vs_benchmark_pct":None if numbers["stock_return_pct"] is None or numbers["benchmark_return_pct"] is None else numbers["stock_return_pct"]-numbers["benchmark_return_pct"],"stock_vs_sector_pct":None if numbers["stock_return_pct"] is None or numbers["sector_return_pct"] is None else numbers["stock_return_pct"]-numbers["sector_return_pct"],"volume_ratio":volume_ratio}
    if is_v2:
        computed.update({key: inputs.get(key) for key in ("filter_config_version", "short_term_stock_return_pct", "short_term_benchmark_return_pct", "short_term_window", "short_term_provenance", "max_short_term_excess_rise_pct")})
        computed["short_term_excess_return_pct"] = expected_short_term_excess
    return {"verdict":"FAIL" if reasons else "PASS","reasons":reasons,"computed":computed,"units":{"stock_vs_benchmark_pct":"percent; stock_return_pct - benchmark_return_pct; positive means stock outperformed benchmark","stock_vs_sector_pct":"percent; stock_return_pct - sector_return_pct; positive means stock outperformed sector","volume_ratio":"ratio; current_volume / baseline_volume; baseline_volume > 0","short_term_stock_return_pct":"percent; close-to-close stock return over short_term_window","short_term_benchmark_return_pct":"percent; close-to-close benchmark return over short_term_window","short_term_excess_return_pct":"percent; short_term_stock_return_pct - short_term_benchmark_return_pct; positive means stock outperformed benchmark","short_term_window":"KST ISO date window, completed aligned sessions only; ended_known_at must not be after decision known_at","short_term_provenance":"KIS completed daily bars and known-at timestamps","as_of":"KST ISO-8601; evidence.known_at and market_data_known_at must each be at or before filter.known_at <= filter.as_of"}}
def save_filter(db,evidence_id,inputs,as_of,known_at):
    evidence=row(db,"material_evidence",evidence_id)
    try:
        evidence_known, filter_known, filter_as_of = parse_kst(evidence["known_at"]), parse_kst(known_at), parse_kst(as_of)
    except (ValueError, TypeError):
        raise HTTPException(422, "evidence/filter timestamps must be KST ISO-8601")

    if filter_known > filter_as_of:
        raise HTTPException(422, "filter.known_at must be at or before as_of")
    if evidence_known > filter_known:
        raise HTTPException(422, "evidence.known_at must be at or before filter.known_at")
    if evidence_known > filter_as_of:
        raise HTTPException(422, "evidence.known_at must be at or before as_of")
    unavailable = inputs.get("market_data_status") == "unavailable"
    if unavailable:
        if "market_data_known_at" in inputs:
            raise HTTPException(422, "unavailable market data must not claim market_data_known_at")
        try:
            attempted_at = parse_kst(inputs.get("market_data_attempted_at"))
        except (ValueError, TypeError):
            raise HTTPException(422, "market_data_attempted_at must be KST ISO-8601")
        if attempted_at > filter_known:
            raise HTTPException(422, "market_data_attempted_at must be at or before filter.known_at")
        if attempted_at > filter_as_of:
            raise HTTPException(422, "market_data_attempted_at must be at or before as_of")
    else:
        try:
            market_known = parse_kst(inputs.get("market_data_known_at"))
        except (ValueError, TypeError):
            raise HTTPException(422, "market_data_known_at must be KST ISO-8601")
        if market_known > filter_known:
            raise HTTPException(422, "market_data_known_at must be at or before filter.known_at")
        if market_known > filter_as_of:
            raise HTTPException(422, "market_data_known_at must be at or before as_of")

    out=run_filter(inputs,as_of,known_at)
    if out["verdict"] == "FAIL" and "market data known_at future of filter known_at" in out["reasons"]:
        raise HTTPException(422, "market_data_known_at must be at or before filter.known_at")
    if out["verdict"] == "FAIL" and "market data known_at future of as_of" in out["reasons"]:
        raise HTTPException(422, "market_data_known_at must be at or before as_of")
    try:r=db.execute("INSERT INTO deterministic_filter_results(evidence_id,raw_inputs,computed_outputs,as_of,known_at,evidence_version,verdict,reasons,created_at) VALUES(?,?,?,?,?,?,?,?,?) RETURNING id",(evidence_id,canon(inputs),canon(out),as_of,known_at,evidence["version"],out["verdict"],canon(out["reasons"]),now())).fetchone()
    except sqlite3.IntegrityError: raise HTTPException(409,"duplicate filter key")
    audit(db,"internal","run","filter",r["id"]);db.commit();return filter_detail(db,r["id"])
def filter_detail(db,ident):
    d=dict(row(db,"deterministic_filter_results",ident)); d["raw_inputs"]=json.loads(d["raw_inputs"]);d["computed_outputs"]=json.loads(d["computed_outputs"]);d["reasons"]=json.loads(d["reasons"]);return d

def save_card(db,data):
    try: card=validate_card(data["card"])
    except (KeyError, ValueError) as exc: raise HTTPException(422, f"structured card invalid: {exc}")
    ev=row(db,"material_evidence",data["evidence_id"]); fi=filter_detail(db,data["filter_id"])
    if fi["evidence_id"]!=ev["id"] or ev["status"]=="invalidated" or fi["evidence_version"] != ev["version"]: raise HTTPException(409,"card requires current active evidence filter")
    if card["filter_verdict"] != fi["verdict"]: raise HTTPException(422,"card source evidence/filter mismatch")
    # A deterministic PASS is evidence quality, not an automatic trading recommendation.
    # Any allowed final verdict may follow PASS; only 매수 검토 가능 can later be approved.
    if fi["verdict"] == "FAIL" and card["verdict"] == "매수 검토 가능": raise HTTPException(422,"FAIL filter cannot be buy-review")
    lineage=data.get("lineage_key",f"{ev['symbol']}:{ev['id']}"); version=db.execute("SELECT COALESCE(MAX(version),0)+1 n FROM decision_cards WHERE lineage_key=?",(lineage,)).fetchone()["n"]
    if version>1:
        old_ids=[item["id"] for item in db.execute("SELECT id FROM decision_cards WHERE lineage_key=?", (lineage,))]
        for old_id in old_ids:
            invalidate_lineage(db, card_id=old_id, reason="new_card")
    r=db.execute("INSERT INTO decision_cards(lineage_key,version,evidence_id,filter_id,prompt_version,prompt_hash,model,provider,card_json,schema_version,verdict,confidence,generated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) RETURNING id",(lineage,version,ev["id"],fi["id"],data.get("prompt_version","decision-card-v1"),prompt_hash(),data["model"],data["provider"],canon(card),card["schema_version"],card["verdict"],float(card["confidence"]),now())).fetchone()
    db.execute("UPDATE material_evidence SET status='card_generated',updated_at=? WHERE id=?",(now(),ev["id"]))
    audit(db,"internal","save","decision_card",r["id"]);db.commit();return card_detail(db,r["id"])
def card_detail(db,ident):
    d=dict(row(db,"decision_cards",ident));d["card"]=json.loads(d.pop("card_json"));return d
def user_fill_summary(db, card_id, user_id):
    """Safe, card-wide paper-fill display state for one authenticated user.

    A card can have several approval generations.  Aggregate their actual
    fills instead of trusting plan status or the newest generation, and scope
    the join through the owning user's plans before exposing any result.
    """
    fills = db.execute("""SELECT f.id, f.side, f.qty, f.filled_at
      FROM order_plans p JOIN order_fills f ON f.order_plan_id=p.id
      WHERE p.card_id=? AND p.user_id=?
      ORDER BY f.filled_at, f.id""", (card_id, user_id))
    first_buy_at = None
    last_full_sell_at = None
    remaining = 0
    for fill in fills:
        qty = max(0, fill["qty"])
        if fill["side"] == "buy":
            first_buy_at = first_buy_at or fill["filled_at"]
            remaining += qty
            # A new buy reopens the card-level position after any earlier exit.
            if qty:
                last_full_sell_at = None
        elif fill["side"] == "sell" and remaining > 0:
            before = remaining
            remaining = max(0, remaining - qty)
            if before > 0 and remaining == 0:
                last_full_sell_at = fill["filled_at"]
    if first_buy_at is None:
        return {"first_buy_at": None, "last_full_sell_at": None, "fill_state": "unfilled"}
    if remaining > 0:
        return {"first_buy_at": first_buy_at, "last_full_sell_at": None, "fill_state": "bought"}
    return {"first_buy_at": first_buy_at, "last_full_sell_at": last_full_sell_at, "fill_state": "sold_complete"}
def user_card_view(db, ident, user_id):
    """Safe read model for the owner-facing UI; never expose internal raw input."""
    result=card_detail(db,ident)
    evidence=evidence_detail(db,result["evidence_id"])
    result["evidence"]={k:evidence.get(k) for k in ("id","symbol","name","kind","title","summary","source","source_url","announcement_at","collected_at","known_at","status")}
    result["evidence"]["snapshot_available"]=bool(evidence.get("snapshot"))
    filter_result=filter_detail(db,result["filter_id"])
    result["filter"]={k:filter_result.get(k) for k in ("id","verdict","reasons","as_of","known_at")}
    result["filter"]["computed"]=filter_result.get("computed_outputs",{}).get("computed",{})
    result["fill_summary"] = user_fill_summary(db, ident, user_id)
    decision=db.execute("SELECT decision,decided_at,note FROM user_decisions WHERE card_id=? AND user_id=?",(ident,user_id)).fetchone()
    plan=db.execute("SELECT * FROM order_plans WHERE card_id=? AND user_id=? ORDER BY id DESC LIMIT 1",(ident,user_id)).fetchone()
    draft=db.execute("SELECT snapshot_json,status,updated_at,supersedes_plan_id FROM order_plan_drafts WHERE card_id=? AND user_id=? AND status='draft'",(ident,user_id)).fetchone()
    plan_view=None
    if plan:
        plan_view=dict(plan); plan_view["take_profit"]=json.loads(plan_view.pop("take_profit_json")); plan_view["split"]=json.loads(plan_view.pop("split_json")); plan_view["evidence_invalidation"]=json.loads(plan_view["evidence_invalidation"])
        plan_view["snapshot"]={"symbol":plan_view["symbol"],"window":{"start":plan_view["window_start"],"end":plan_view["window_end"]},"price_cap":plan_view["price_cap"],"max_amount":plan_view["max_amount"],"max_qty":plan_view["max_qty"],"split":plan_view["split"],"order_type":plan_view["order_type"],"stop_loss":plan_view["stop_loss"],"take_profit":plan_view["take_profit"],"evidence_invalidation":plan_view["evidence_invalidation"],"holding_until":plan_view["holding_until"],"review_at":plan_view["review_at"],"valid_until":plan_view["valid_until"],"expires":plan_view["expires_at"]}
        plan_view["fills"]=[dict(x) for x in db.execute("SELECT side,qty,price,filled_at FROM order_fills WHERE order_plan_id=? ORDER BY id",(plan["id"],))]
        plan_view["events"]=[dict(x) for x in db.execute("SELECT event,reason,at FROM order_events WHERE order_plan_id=? ORDER BY id",(plan["id"],))]
        plan_view["position"]=dict(db.execute("SELECT symbol,qty,avg_price,status FROM positions WHERE order_plan_id=?",(plan["id"],)).fetchone() or {})
        plan_view["exit_lineage"]=[dict(x) for x in db.execute("SELECT rule,quote_known_at,created_at FROM exit_lineage WHERE order_plan_id=? ORDER BY id",(plan["id"],))]
    result["event_scenarios"]=[]
    for scenario in db.execute("SELECT id,expected_value_krw,scenarios_json FROM event_scenario_sets WHERE card_id=? ORDER BY id DESC",(ident,)):
        observations=[dict(x) for x in db.execute("SELECT match,action FROM event_scenario_observations WHERE scenario_set_id=? ORDER BY id DESC LIMIT 1",(scenario["id"],))]
        result["event_scenarios"].append({"expected_value_krw":scenario["expected_value_krw"],"scenarios":json.loads(scenario["scenarios_json"]),"current":observations[0] if observations else {"match":"UNOBSERVED","action":"NO_ACTION"}})
    result["user_state"]={"decision":dict(decision) if decision else None,"order_plan":plan_view,"draft":({**dict(draft),"snapshot":json.loads(draft["snapshot_json"])} if draft else None),"default_paper_amount":_default_paper_amount(db, user_id)}
    return result

def list_cards(db, missing=False, date=None, current_only=False):
    """Date-scoped requests retain history; current-only lists show active heads."""
    params = []
    date_clause = ""
    if date:
        date_clause = " AND substr(known_at,1,10)=?"
        params.append(date)
    if missing:
        query = """SELECT id FROM material_evidence
          WHERE status != 'invalidated' AND NOT EXISTS (
            SELECT 1 FROM decision_cards c WHERE c.evidence_id=material_evidence.id
          )""" + date_clause + " ORDER BY id DESC"
        return [missing_evidence_view(db, item["id"]) for item in db.execute(query, params)]
    query = """SELECT c.id FROM decision_cards c
      JOIN material_evidence e ON e.id=c.evidence_id WHERE 1=1"""
    if date:
        query += " AND substr(e.known_at,1,10)=?"
    if current_only:
        query += """ AND c.invalidated_at IS NULL
          AND NOT EXISTS (SELECT 1 FROM decision_cards newer
            WHERE newer.lineage_key=c.lineage_key AND newer.version>c.version
              AND newer.invalidated_at IS NULL)"""
    return [card_detail(db, item["id"]) for item in db.execute(query + " ORDER BY c.id DESC", params)]
def _positive(value): return isinstance(value,(int,float)) and not isinstance(value,bool) and math.isfinite(value) and value>0
def _valid_plan(card):
    x=card["card"]; window=x.get("window") or {}
    if not all(_positive(x.get(k)) for k in ("price_cap","max_amount")) or not isinstance(x.get("max_qty"),int) or isinstance(x.get("max_qty"),bool) or x["max_qty"]<=0 or not _positive(x.get("stop_loss")) or not x.get("evidence_invalidation"): raise HTTPException(422,"positive frozen order and exit rules required")
    if x.get("order_type") not in {"limit","market"}: raise HTTPException(422,"invalid order_type")
    if not isinstance(x.get("take_profit"),list) or not x["take_profit"] or any(not isinstance(rule,dict) or not _positive(rule.get("price")) or not isinstance(rule.get("qty"),int) or isinstance(rule.get("qty"),bool) or rule["qty"]<=0 for rule in x["take_profit"]): raise HTTPException(422,"positive take-profit rules required")
    try:
        start, end = parse_kst(window["start"]), parse_kst(window["end"])
        holding_until, review_at = parse_kst(x["holding_until"]), parse_kst(x["review_at"])
        valid_until, expires = parse_kst(x["valid_until"]), parse_kst(x["expires"])
    except (KeyError, ValueError, TypeError): raise HTTPException(422,"nonempty KST window and exit times required")
    if start>=end: raise HTTPException(422,"window start must precede end")
    return x,window,valid_until
def _plan_snapshot(plan):
    """Rebuild an immutable plan's user-editable snapshot without raw DB fields."""
    max_amount = plan["max_amount"]
    # SQLite REAL round-trips an exact KRW amount as 500000.0; preserve canonical hash identity.
    if isinstance(max_amount, float) and max_amount.is_integer():
        max_amount = int(max_amount)
    return {"symbol":plan["symbol"], "window":{"start":plan["window_start"],"end":plan["window_end"]}, "price_cap":plan["price_cap"], "max_amount":max_amount, "max_qty":plan["max_qty"], "split":json.loads(plan["split_json"]), "order_type":plan["order_type"], "stop_loss":plan["stop_loss"], "take_profit":json.loads(plan["take_profit_json"]), "evidence_invalidation":json.loads(plan["evidence_invalidation"]), "holding_until":plan["holding_until"], "review_at":plan["review_at"], "valid_until":plan["valid_until"], "expires":plan["expires_at"]}

def _default_paper_amount(db, user_id):
    """Legacy users have no row and therefore safely retain the exact fallback."""
    setting = db.execute("SELECT default_paper_amount FROM user_settings WHERE user_id=?", (user_id,)).fetchone()
    return int(setting["default_paper_amount"]) if setting else 500000

def _snapshot(card, override=None, default_amount=None):
    x = dict(card["card"])
    if default_amount is not None:
        x["max_amount"] = default_amount
    x.update(override or {})
    frozen = dict(card); frozen["card"] = x
    _valid_plan(frozen)
    return x

EDITABLE_DRAFT_FIELDS=frozenset({"window","price_cap","max_amount","max_qty","split","order_type","stop_loss","take_profit","evidence_invalidation","holding_until","review_at","valid_until","expires"})
def _validate_override(override):
    if not isinstance(override,dict) or set(override)-EDITABLE_DRAFT_FIELDS: raise HTTPException(422,"invalid draft override fields")

def edit_draft(db, card_id, user_id, override):
    c=card_detail(db,card_id)
    if c["invalidated_at"]: raise HTTPException(409,"card invalidated")
    if c["card"]["filter_verdict"] != "PASS" or c["verdict"] != "매수 검토 가능": raise HTTPException(409,"non-PASS card cannot create draft")
    _validate_override(override)
    draft = db.execute("SELECT * FROM order_plan_drafts WHERE card_id=? AND user_id=? AND status='draft'", (card_id, user_id)).fetchone()
    existing = db.execute("SELECT * FROM order_plans WHERE card_id=? AND user_id=? ORDER BY id DESC LIMIT 1", (card_id, user_id)).fetchone()
    # Omitted fields always inherit the immutable active draft first, then plan lineage, then today's default.
    base = json.loads(draft["snapshot_json"]) if draft else (_plan_snapshot(existing) if existing else _snapshot(c, default_amount=_default_paper_amount(db, user_id)))
    x = dict(base)
    x.update(override)
    _snapshot(c, x)
    db.execute("INSERT INTO order_plan_drafts(card_id,user_id,snapshot_json,created_at,updated_at) VALUES(?,?,?,?,?) ON CONFLICT(card_id,user_id,status) DO UPDATE SET snapshot_json=excluded.snapshot_json,updated_at=excluded.updated_at",(card_id,user_id,canon(x),now(),now()))
    db.commit(); return {"card_id":card_id,"status":"draft","draft":x}

def edit_order_plan(db, plan_id, user_id, override):
    """Store a validated user draft; post-approval edits revoke entries, not exits."""
    p = row(db, "order_plans", plan_id)
    if p["user_id"] != user_id: raise HTTPException(404, "order plan not found")
    _validate_override(override)
    c = card_detail(db, p["card_id"])
    draft = db.execute("SELECT * FROM order_plan_drafts WHERE card_id=? AND user_id=? AND status='draft'", (p["card_id"], user_id)).fetchone()
    if draft and draft["supersedes_plan_id"] != plan_id:
        raise HTTPException(409, "draft belongs to a different order plan")
    base = json.loads(draft["snapshot_json"]) if draft else _plan_snapshot(p)
    base = dict(base)
    base.update(override); _snapshot(c, base)
    db.execute("INSERT INTO order_plan_drafts(card_id,user_id,snapshot_json,supersedes_plan_id,created_at,updated_at) VALUES(?,?,?,?,?,?) ON CONFLICT(card_id,user_id,status) DO UPDATE SET snapshot_json=excluded.snapshot_json,supersedes_plan_id=excluded.supersedes_plan_id,updated_at=excluded.updated_at", (c["id"],user_id,canon(base),plan_id,now(),now()))
    position = db.execute("SELECT qty FROM positions WHERE order_plan_id=?", (plan_id,)).fetchone()
    status = "review_required" if position and position["qty"] > 0 else "entry_invalidated"
    db.execute("UPDATE order_plans SET status=? WHERE id=?", (status,plan_id))
    audit(db,str(user_id),"edit_invalidates_entry","order_plan",plan_id); db.commit()
    return {"order_plan_id":plan_id,"status":status,"reapproval_required":True,"draft":base}

def user_decision(db,card_id,user_id,decision,note=""):
    c=card_detail(db,card_id)
    if decision!="approve":
        db.execute("INSERT INTO user_decisions(card_id,user_id,decision,decided_at,note) VALUES(?,?,?,?,?) ON CONFLICT(card_id,user_id) DO UPDATE SET decision=excluded.decision,decided_at=excluded.decided_at,note=excluded.note",(card_id,user_id,decision,now(),note))
        for p in db.execute("SELECT id FROM order_plans WHERE card_id=? AND user_id=? AND status IN ('approved','execution_wait','partial_or_filled','exit_wait')",(card_id,user_id)):
            pos=db.execute("SELECT qty FROM positions WHERE order_plan_id=?",(p["id"],)).fetchone(); db.execute("UPDATE order_plans SET status=? WHERE id=?",("review_required" if pos and pos["qty"] else "entry_invalidated",p["id"]))
        db.commit(); return {"decision":decision}
    if c["invalidated_at"]: raise HTTPException(409,"card invalidated; regenerate and reapprove")
    if c["card"]["filter_verdict"] != "PASS" or c["verdict"] != "매수 검토 가능": raise HTTPException(409,"only PASS buy-review card is approvable")
    draft=db.execute("SELECT * FROM order_plan_drafts WHERE card_id=? AND user_id=? AND status='draft'",(card_id,user_id)).fetchone()
    latest_plan = db.execute("SELECT * FROM order_plans WHERE card_id=? AND user_id=? ORDER BY id DESC LIMIT 1", (card_id, user_id)).fetchone()
    if draft:
        x = _snapshot(c, json.loads(draft["snapshot_json"]))
    elif latest_plan:
        # Reapprovals without a draft retain this card's last frozen plan, not a later global setting.
        x = _snapshot(c, _plan_snapshot(latest_plan))
    else:
        # Approval is server-side: never depend on a browser prefill to freeze the user's budget.
        x = _snapshot(c, default_amount=_default_paper_amount(db, user_id))
    window=x["window"]
    valid_until, expires = parse_kst(x["valid_until"]), parse_kst(x["expires"])
    snapshot=canon(x); base_hash=hashlib.sha256(canon({"card":c["id"],"user":user_id,"snapshot":snapshot}).encode()).hexdigest()
    existing=db.execute("SELECT id,version_hash FROM order_plans WHERE card_id=? AND user_id=? AND version_hash LIKE ? AND status IN ('approved','execution_wait','partial_or_filled') ORDER BY id DESC LIMIT 1",(c["id"],user_id,base_hash+":%")).fetchone()
    if existing: return {"decision":"approve","order_plan_hash":existing["version_hash"],"idempotent":True,"order_plan_id":existing["id"]}
    generation=db.execute("SELECT COALESCE(MAX(approval_generation),0)+1 n FROM order_plans WHERE card_id=? AND user_id=?",(c["id"],user_id)).fetchone()["n"]
    vh=f"{base_hash}:{generation}"
    db.execute("INSERT INTO user_decisions(card_id,user_id,decision,decided_at,note) VALUES(?,?,?,?,?) ON CONFLICT(card_id,user_id) DO UPDATE SET decision=excluded.decision,decided_at=excluded.decided_at,note=excluded.note",(card_id,user_id,"approve",now(),note))
    p=db.execute("INSERT INTO order_plans(card_id,card_version,user_id,approved_at,valid_until,symbol,window_start,window_end,price_cap,max_amount,max_qty,split_json,order_type,stop_loss,take_profit_json,evidence_invalidation,holding_until,review_at,expires_at,status,version_hash,approval_generation) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) RETURNING id",(c["id"],c["version"],user_id,now(),valid_until.isoformat(),x["symbol"],window["start"],window["end"],x["price_cap"],x["max_amount"],x["max_qty"],canon(x.get("split",[])),x.get("order_type","limit"),x["stop_loss"],canon(x["take_profit"]),canon(x["evidence_invalidation"]),x.get("holding_until"),x.get("review_at"),expires.isoformat(),"approved",vh,generation)).fetchone()
    if draft:
        # The partial unique key permits one draft and one approved snapshot per card/user.
        # Preserve prior approved drafts as lineage before freezing this reapproval snapshot.
        db.execute("UPDATE order_plan_drafts SET status='superseded' WHERE card_id=? AND user_id=? AND status='approved'", (card_id, user_id))
        db.execute("UPDATE order_plan_drafts SET status='approved' WHERE id=?",(draft["id"],))
    audit(db,str(user_id),"approve","decision_card",card_id);db.commit();return {"decision":"approve","order_plan_hash":vh,"order_plan_id":p["id"],"idempotent":False}

def evaluate_order_plan(db,plan_id,tick,server_now=None):
    p=dict(row(db,"order_plans",plan_id)); key=tick.get("tick_key")
    if not key: raise HTTPException(422,"tick_key required")
    if db.execute("SELECT 1 FROM order_evaluations WHERE order_plan_id=? AND tick_key=?",(plan_id,key)).fetchone(): return {"idempotent":True,"fills":[]}
    reasons=[]
    try: known=parse_kst(tick["known_at"]); current=parse_kst(server_now or tick.get("server_now") or now())
    except (KeyError,ValueError,TypeError): known=current=None; reasons.append("invalid_known_at")
    if current is None or not fresh_quote(type("Q",(),{"known_at":known})(),current): reasons.append("stale_or_future_quote")
    remaining=p["bought_qty"]-p["sold_qty"]
    # Exit gates are deliberately independent from entry validity and prechecks.
    rule=None; tranche_qty=None
    if not reasons and not market_open(current): reasons.append("market_closed")
    if not reasons and remaining>0:
        if tick.get("manual_exit"): rule="manual"
        elif tick.get("evidence_invalidated"): rule="evidence_invalidation"
        elif _positive(tick.get("price")) and tick["price"]<=p["stop_loss"]: rule="stop_loss"
        else:
            for i,r in enumerate(json.loads(p["take_profit_json"])):
                stage=f"take_profit:{i}"
                used=db.execute("SELECT 1 FROM exit_lineage WHERE order_plan_id=? AND rule=?",(plan_id,stage)).fetchone()
                if not used and _positive(r.get("price")) and tick.get("price",0)>=r["price"]:
                    rule=stage; tranche_qty=int(r.get("qty",0)); break
            if not rule and p["holding_until"] and current>=parse_kst(p["holding_until"]): rule="holding_until"
            if not rule and p["review_at"] and current>=parse_kst(p["review_at"]): rule="review_at"
    if rule:
        qty=remaining if rule in {"manual","evidence_invalidation","stop_loss","holding_until","review_at"} else min(remaining,tranche_qty)
        side="sell"
    else:
        entry_reasons=list(reasons)
        if p["status"] not in {"approved","execution_wait","partial_or_filled"} or current>parse_kst(p["expires_at"]): entry_reasons.append("unapproved_or_expired")
        if not market_open(current) or current<parse_kst(p["window_start"]) or current>parse_kst(p["window_end"]): entry_reasons.append("market_or_order_window_closed")
        if not _positive(tick.get("price")) or tick["price"]>p["price_cap"] or tick.get("gap_pct",0)>10 or not _positive(tick.get("liquidity")) or not _positive(tick.get("trading_value")) or tick.get("conflicting_disclosure"): entry_reasons.append("precheck_failed")
        if entry_reasons:
            db.execute("INSERT INTO order_evaluations(order_plan_id,tick_key,result,reasons,evaluated_at) VALUES(?,?,?,?,?)",(plan_id,key,"REJECT",canon(entry_reasons),now())); db.execute("INSERT INTO order_events(order_plan_id,event_key,event,reason,at) VALUES(?,?,?,?,?)",(plan_id,f"{plan_id}:{key}:entry_reject","entry_rejected",canon(entry_reasons),now())); db.commit(); return {"fills":[],"reasons":entry_reasons}
        requested=max(0,int(tick.get("fill_qty",p["max_qty"]-p["bought_qty"])))
        qty=min(requested,p["max_qty"]-p["bought_qty"],math.floor((p["max_amount"]-p["bought_amount"])/tick["price"])); side="buy"
    if qty<=0:
        db.execute("INSERT INTO order_evaluations(order_plan_id,tick_key,result,reasons,evaluated_at) VALUES(?,?,?,?,?)",(plan_id,key,"NOOP",canon(reasons),now())); db.commit(); return {"fills":[]}
    event_key=f"{plan_id}:{key}:{side}"; fill=db.execute("INSERT INTO order_fills(order_plan_id,event_key,side,qty,price,filled_at) VALUES(?,?,?,?,?,?) RETURNING id",(plan_id,event_key,side,qty,tick["price"],now())).fetchone()
    db.execute("INSERT INTO order_events(order_plan_id,event_key,event,reason,at) VALUES(?,?,?,?,?)",(plan_id,event_key,"exit_filled" if side=="sell" else "buy_filled",rule or "approved_entry",now()))
    if side=="buy":
        new_amount=p["bought_amount"]+qty*tick["price"]; new_qty=p["bought_qty"]+qty
        db.execute("UPDATE order_plans SET bought_qty=?,bought_amount=?,last_tick_key=?,status=? WHERE id=?",(new_qty,new_amount,key,"partial_or_filled",plan_id))
        db.execute("INSERT INTO positions(order_plan_id,symbol,qty,avg_price,status) VALUES(?,?,?,?,?) ON CONFLICT(order_plan_id) DO UPDATE SET avg_price=(positions.avg_price*positions.qty+excluded.avg_price*excluded.qty)/(positions.qty+excluded.qty),qty=positions.qty+excluded.qty,status='open'",(plan_id,p["symbol"],qty,tick["price"],"open"))
    else:
        after=remaining-qty; db.execute("UPDATE order_plans SET sold_qty=sold_qty+?,last_tick_key=?,status=? WHERE id=?",(qty,key,"closed" if after==0 else "exit_wait",plan_id)); db.execute("UPDATE positions SET qty=qty-?,status=CASE WHEN qty-?=0 THEN 'closed' ELSE 'open' END WHERE order_plan_id=?",(qty,qty,plan_id)); db.execute("INSERT INTO exit_lineage(order_plan_id,fill_id,rule,quote_known_at,created_at) VALUES(?,?,?,?,?)",(plan_id,fill["id"],rule,known.isoformat(),now()))
    db.execute("INSERT INTO order_evaluations(order_plan_id,tick_key,result,reasons,evaluated_at) VALUES(?,?,?,?,?)",(plan_id,key,"FILLED",canon([side,rule] if rule else [side]),now())); db.commit(); return {"fills":[{"side":side,"qty":qty,"price":tick["price"]}]}
