"""Immutable, evidence-only conditional scenario sets."""
from __future__ import annotations
from datetime import datetime, timezone
from typing import Any
from fastapi import HTTPException
from .decision_cards import canon, now, _safe_source_url

KIND="CONDITIONAL"; SCHEMA_VERSION=1; LABELS=("BAD","BASE","GOOD")
_ALLOWED={"proof_point","next_check","unknowns","headline","false_positive","evidence_invalidation"}

def _fail(reason):
    return {"status":"SCENARIO_INPUTS_REQUIRED","missing":[reason]}

def _leaves(value: Any, path: str) -> list[dict[str,str]]:
    """Flatten only source fields, preserving literal leaf text and source path."""
    if isinstance(value,str): return [{"text":value.strip(),"path":path}] if value.strip() else []
    if isinstance(value,(list,tuple)):
        out=[]
        for i,item in enumerate(value): out += _leaves(item,f"{path}[{i}]")
        return out
    if isinstance(value,dict):
        out=[]
        for key,item in value.items():
            if not isinstance(key,str) or not key: raise ValueError("malformed source object")
            out += _leaves(item,f"{path}.{key}")
        return out
    if value is None: return []
    raise ValueError("unsupported source value")

def _condition(value,path):
    return [{"text":x["text"],"provenance":x["path"]} for x in _leaves(value,path)]

def _normal(items): return {" ".join(x["text"].split()).casefold() for x in items}
def _dedupe(items):
    seen=set(); return [x for x in items if not (x["text"] in seen or seen.add(x["text"]))]

def build_scenario_candidate(evidence: dict[str,Any], card: dict[str,Any]) -> dict[str,Any]:
    evidence=evidence if isinstance(evidence,dict) else {}; wrapped=card.get("card") if isinstance(card,dict) and isinstance(card.get("card"),dict) else card
    if not isinstance(wrapped,dict): return _fail("card")
    try:
        good=_dedupe(_condition(wrapped.get("proof_point"),"card.proof_point")+_condition(wrapped.get("next_check"),"card.next_check")+_condition(wrapped.get("unknowns"),"card.unknowns"))
        base=_dedupe(_condition(evidence.get("summary"),"evidence.summary")+_condition(evidence.get("title"),"evidence.title")+_condition(wrapped.get("headline"),"card.headline"))
        bad=_dedupe(_condition(wrapped.get("false_positive"),"card.false_positive")+_condition(wrapped.get("evidence_invalidation"),"card.evidence_invalidation"))
    except ValueError:
        return _fail("malformed_source_material")
    missing=[]
    if not good: missing.append("card.unknowns_or_next_proof_point")
    if not base: missing.append("evidence.confirmed_summary_or_title")
    if not bad: missing.append("card.false_positive_or_evidence_invalidation")
    if not _safe_source_url(evidence.get("source_url")): missing.append("evidence.source_url")
    if any(not evidence.get(k) for k in ("id","symbol","known_at","announcement_at")): missing.append("evidence.source_facts")
    if not isinstance(card,dict) or not card.get("id") or card.get("evidence_id") != evidence.get("id"): missing.append("card/evidence_exact_lineage")
    sets=[_normal(x) for x in (bad,base,good)]
    if all(sets) and len({frozenset(x) for x in sets}) != 3: missing.append("distinct_source_conditions")
    if missing: return {"status":"SCENARIO_INPUTS_REQUIRED","missing":sorted(set(missing))}
    return {"status":"COMPLETE","payload":{"kind":KIND,"schema_version":SCHEMA_VERSION,"event_identity":f"CONDITIONAL:{evidence['id']}:{card['id']}","version":1,"symbol":evidence["symbol"],"event_type":KIND,"evidence_id":evidence["id"],"card_id":card["id"],"disclosed_at":evidence["announcement_at"],"known_at":evidence["known_at"],"source_url":evidence["source_url"],"scenarios":[{"label":"BAD","conditions":bad},{"label":"BASE","conditions":base},{"label":"GOOD","conditions":good}]}}

def _time(value):
    if not isinstance(value,str) or not value: raise HTTPException(422,"invalid conditional timestamp")
    try: parsed=datetime.fromisoformat(value.replace("Z","+00:00"))
    except ValueError: raise HTTPException(422,"invalid conditional timestamp")
    if parsed.tzinfo is None or parsed > datetime.now(timezone.utc): raise HTTPException(422,"conditional timestamp must be offset-bearing and not future")
    return parsed

def _validate(data):
    keys={"kind","schema_version","event_identity","version","symbol","event_type","evidence_id","card_id","disclosed_at","known_at","source_url","scenarios"}
    if not isinstance(data,dict) or set(data)!=keys or data.get("kind")!=KIND or data.get("event_type")!=KIND or data.get("schema_version")!=1: raise HTTPException(422,"invalid conditional scenario set")
    if not isinstance(data["version"],int) or isinstance(data["version"],bool) or data["version"]<1: raise HTTPException(422,"invalid conditional scenario version")
    if not all(isinstance(data.get(x),str) and data[x].strip() for x in ("event_identity","symbol","disclosed_at","known_at")) or not _safe_source_url(data.get("source_url")): raise HTTPException(422,"invalid conditional scenario source fields")
    if any(not isinstance(data.get(x),int) or isinstance(data.get(x),bool) or data[x]<1 for x in ("evidence_id","card_id")): raise HTTPException(422,"conditional scenario lineage required")
    announced,known=_time(data["disclosed_at"]),_time(data["known_at"])
    if announced>known: raise HTTPException(422,"announcement_at must be at or before known_at")
    scenarios=data["scenarios"]
    if not isinstance(scenarios,list) or [x.get("label") if isinstance(x,dict) else None for x in scenarios]!=list(LABELS): raise HTTPException(422,"conditions must be BAD BASE GOOD")
    norms=[]
    for item in scenarios:
        if set(item)!={"label","conditions"} or not isinstance(item["conditions"],list) or not item["conditions"]: raise HTTPException(422,"invalid conditional scenario conditions")
        for condition in item["conditions"]:
            if not isinstance(condition,dict) or set(condition)!={"text","provenance"} or not all(isinstance(condition.get(k),str) and condition[k].strip() for k in condition): raise HTTPException(422,"invalid conditional source attribution")
        norms.append(frozenset(_normal(item["conditions"])))
    if len(set(norms)) != 3: raise HTTPException(422,"SCENARIO_INPUTS_REQUIRED: distinct_source_conditions")
    return data

def create(db,data,*,commit=True):
    """Append only the exact server-derived candidate for the locked active card.

    ``commit=True`` owns a new SQLite write reservation.  Nested callers must
    pass ``commit=False`` from an active owner transaction; this function
    acquires its no-op reservation in that owner before source reads.
    """
    d=_validate(data)
    owns_transaction=False
    try:
        if commit:
            if db.in_transaction:
                raise HTTPException(409,"conditional scenario requires a new owner transaction")
            try:
                db.execute("BEGIN IMMEDIATE")
            except Exception as exc:
                raise HTTPException(409,"conditional scenario write reservation unavailable") from exc
            owns_transaction=True
        elif not db.in_transaction:
            raise HTTPException(409,"conditional scenario requires an active owner transaction")
        else:
            try:
                # SQLite exposes no transaction-lock state.  A no-op UPDATE
                # upgrades the caller-owned transaction before any source read.
                db.execute("UPDATE event_conditional_scenario_sets SET frozen_at=frozen_at WHERE 0")
            except Exception as exc:
                raise HTTPException(409,"conditional scenario write reservation unavailable") from exc

        ev=db.execute("SELECT * FROM material_evidence WHERE id=?",(d["evidence_id"],)).fetchone(); card=db.execute("SELECT * FROM decision_cards WHERE id=?",(d["card_id"],)).fetchone()
        if not ev or not card or card["evidence_id"]!=ev["id"] or card["invalidated_at"] or ev["status"] not in {"card_generated","decision_pending"}: raise HTTPException(409,"conditional scenario requires active current lineage")
        if card["version"] != db.execute("SELECT MAX(version) FROM decision_cards WHERE lineage_key=? AND invalidated_at IS NULL",(card["lineage_key"],)).fetchone()[0]: raise HTTPException(409,"conditional card is not current head")
        collected=ev["collected_at"]
        if collected and _time(d["known_at"]) > _time(collected): raise HTTPException(422,"known_at must be at or before collected_at")
        # All authoritative reads, canonical rebuild, replay lookup, and insert
        # occur under the same write reservation.
        canonical=build_scenario_candidate(dict(ev), {**dict(card), "card": __import__("json").loads(card["card_json"])})
        if canonical.get("status") != "COMPLETE" or d != canonical["payload"]:
            raise HTTPException(409,"conditional payload must exactly equal current persisted candidate")
        body=canon({k:v for k,v in d.items() if k!="scenarios"}); scenarios=canon(d["scenarios"])
        exact=db.execute("SELECT * FROM event_conditional_scenario_sets WHERE card_id=? AND event_identity=? AND version=?",(d["card_id"],d["event_identity"],d["version"])).fetchone()
        if exact:
            if exact["scenario_json"]==body and exact["scenarios_json"]==scenarios:
                result=_detail(exact,True)
                if owns_transaction: db.commit()
                return result
            raise HTTPException(409,"immutable conditional scenario identity/version payload mismatch")
        head=db.execute("SELECT MAX(version) FROM event_conditional_scenario_sets WHERE card_id=? AND event_identity=?",(d["card_id"],d["event_identity"])).fetchone()[0]
        if d["version"] != (1 if head is None else head+1): raise HTTPException(409,"conditional scenario parent is not current head")
        try: row=db.execute("INSERT INTO event_conditional_scenario_sets(event_identity,version,symbol,evidence_id,card_id,disclosed_at,known_at,source_url,frozen_at,scenario_json,scenarios_json) VALUES(?,?,?,?,?,?,?,?,?,?,?) RETURNING *",(d["event_identity"],d["version"],d["symbol"],d["evidence_id"],d["card_id"],d["disclosed_at"],d["known_at"],d["source_url"],now(),body,scenarios)).fetchone()
        except Exception as exc: raise HTTPException(409,"conditional scenario persistence conflict") from exc
        if owns_transaction: db.commit()
        return _detail(row,False)
    except Exception:
        if owns_transaction and db.in_transaction: db.rollback()
        raise
def _detail(row,idempotent=False):
    return {"id":row["id"],"version":row["version"],"scenario_kind":KIND,"expected_value_krw":None,"scenarios":__import__("json").loads(row["scenarios_json"]),"idempotent":idempotent}
