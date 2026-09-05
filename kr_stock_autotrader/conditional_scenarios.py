"""Immutable, evidence-only conditional scenario sets."""
from __future__ import annotations
from datetime import datetime, timezone
import math
from typing import Any
from fastapi import HTTPException
from .decision_cards import canon, now, _safe_source_url

KIND="CONDITIONAL"; SCHEMA_VERSION=2; LABELS=("BAD","BASE","GOOD")
_POLICY={
    "BAD":"투자 논지가 약화되거나 무효화되었습니다.",
    "BASE":"확인된 사실 범위에서 현재 판단을 유지합니다.",
    "GOOD":"긍정적 증거가 확인될 때에만 투자 논지가 강화됩니다.",
}

def _fail(reason): return {"status":"SCENARIO_INPUTS_REQUIRED","missing":[reason]}
def _leaves(value: Any, path: str) -> list[dict[str,str]]:
    if isinstance(value,str): return [{"text":value.strip(),"path":path}] if value.strip() else []
    if isinstance(value,(list,tuple)): return [leaf for i,item in enumerate(value) for leaf in _leaves(item,f"{path}[{i}]")]
    if isinstance(value,dict):
        out=[]
        for key,item in value.items():
            if not isinstance(key,str) or not key: raise ValueError("malformed source object")
            out += _leaves(item,f"{path}.{key}")
        return out
    if value is None: return []
    raise ValueError("unsupported source value")
def _condition(value,path): return [{"text":x["text"],"provenance":x["path"]} for x in _leaves(value,path)]
def _normal(items): return {" ".join(x["text"].split()).casefold() for x in items}
def _dedupe(items):
    seen=set(); return [x for x in items if not (x["text"] in seen or seen.add(x["text"]))]
def _one(items, fallback): return items[0] if items else {"text":fallback,"provenance":"derived.fail_closed"}
def _positive(value): return isinstance(value,(int,float)) and not isinstance(value,bool) and math.isfinite(value) and value>0

def _price(card: dict[str,Any], label: str) -> dict[str,Any]:
    mapping={"BAD":("stop_loss","손절 기준","card.stop_loss"),"BASE":("price_cap","진입 상한","card.price_cap")}
    if label == "GOOD":
        rules=card.get("take_profit")
        if isinstance(rules,list) and rules and isinstance(rules[0],dict) and _positive(rules[0].get("price")):
            return {"value_krw":float(rules[0]["price"]),"basis":"익절 기준","provenance":"card.take_profit[0].price"}
    else:
        key,basis,path=mapping[label]
        if _positive(card.get(key)): return {"value_krw":float(card[key]),"basis":basis,"provenance":path}
    return {"value_krw":None,"basis":"가격 미설정","provenance":None}

def _actions(label: str, price: dict[str,Any], has_invalidation: bool) -> dict[str,str]:
    value=price["value_krw"]
    amount=f"{value:g}원" if value is not None else "가격 미설정"
    if label == "BAD":
        return {"no_position":"신규 진입 안 함", "holding":f"{amount} 이하 축소·매도 검토" if value is not None else ("무효화 조건 확인 시 축소·매도 검토 · 가격 미설정" if has_invalidation else "보유 지속 여부 확인 · 가격 미설정")}
    if label == "BASE":
        return {"no_position":f"{amount} 이하에서만 신규 진입 검토" if value is not None else "신규 진입 안 함 · 가격 미설정", "holding":"확인된 사실 범위에서 보유 유지·다음 확인"}
    return {"no_position":"긍정 증거 확인 전 신규 진입 안 함", "holding":f"{amount} 이상 분할매도 검토" if value is not None else "긍정 증거와 보유 기준 확인 · 가격 미설정"}

def build_scenario_candidate(evidence: dict[str,Any], card: dict[str,Any]) -> dict[str,Any]:
    evidence=evidence if isinstance(evidence,dict) else {}; wrapped=card.get("card") if isinstance(card,dict) and isinstance(card.get("card"),dict) else card
    if not isinstance(wrapped,dict): return _fail("card")
    try:
        proof=_dedupe(_condition(wrapped.get("proof_point"),"card.proof_point"))
        base=_dedupe(_condition(evidence.get("summary"),"evidence.summary")+_condition(evidence.get("title"),"evidence.title")+_condition(wrapped.get("headline"),"card.headline"))
        bad=_dedupe(_condition(wrapped.get("false_positive"),"card.false_positive")+_condition(wrapped.get("evidence_invalidation"),"card.evidence_invalidation"))
        checks=_dedupe(_condition(wrapped.get("next_check"),"card.next_check")+_condition(wrapped.get("unknowns"),"card.unknowns"))
    except ValueError: return _fail("malformed_source_material")
    missing=[]
    if not proof: missing.append("card.proof_point")
    if not checks or not any(x["provenance"].startswith("card.next_check") for x in checks): missing.append("card.next_check")
    if not base: missing.append("evidence.confirmed_summary_or_title")
    if not bad: missing.append("card.false_positive_or_evidence_invalidation")
    if not _safe_source_url(evidence.get("source_url")): missing.append("evidence.source_url")
    if any(not evidence.get(k) for k in ("id","symbol","known_at","announcement_at")): missing.append("evidence.source_facts")
    if not isinstance(card,dict) or not card.get("id") or card.get("evidence_id") != evidence.get("id"): missing.append("card/evidence_exact_lineage")
    sets=[_normal(x) for x in (bad,base,proof)]
    if all(sets) and len({frozenset(x) for x in sets}) != 3: missing.append("distinct_source_conditions")
    if missing: return {"status":"SCENARIO_INPUTS_REQUIRED","missing":sorted(set(missing))}
    scenario_sources={"BAD":bad,"BASE":base,"GOOD":proof}; scenarios=[]
    has_invalidation=any(x["provenance"].startswith("card.evidence_invalidation") for x in bad)
    for label in LABELS:
        situation=_one(scenario_sources[label],"상황 확인 필요")
        price=_price(wrapped,label)
        scenario_checks=_dedupe((bad if label=="BAD" else [])+checks)
        scenarios.append({"label":label,"situation":situation,"judgment":{"text":_POLICY[label],"provenance":"derived.policy_v2"},"actions":_actions(label,price,has_invalidation),"price_criterion":price,"checks":scenario_checks})
    return {"status":"COMPLETE","payload":{"kind":KIND,"schema_version":SCHEMA_VERSION,"event_identity":f"CONDITIONAL:{evidence['id']}:{card['id']}","version":1,"symbol":evidence["symbol"],"event_type":KIND,"evidence_id":evidence["id"],"card_id":card["id"],"disclosed_at":evidence["announcement_at"],"known_at":evidence["known_at"],"source_url":evidence["source_url"],"scenarios":scenarios}}

def _time(value):
    if not isinstance(value,str) or not value: raise HTTPException(422,"invalid conditional timestamp")
    try: parsed=datetime.fromisoformat(value.replace("Z","+00:00"))
    except ValueError: raise HTTPException(422,"invalid conditional timestamp")
    if parsed.tzinfo is None or parsed > datetime.now(timezone.utc): raise HTTPException(422,"conditional timestamp must be offset-bearing and not future")
    return parsed

def _source(value): return isinstance(value,dict) and set(value)=={"text","provenance"} and all(isinstance(value.get(k),str) and value[k].strip() for k in value)
def _validate_v2_item(item):
    if not isinstance(item,dict) or list(item)!=["label","situation","judgment","actions","price_criterion","checks"] or item.get("label") not in LABELS or not _source(item["situation"]) or not _source(item["judgment"]): raise HTTPException(422,"invalid actionable conditional scenario")
    if item["judgment"] != {"text":_POLICY[item["label"]],"provenance":"derived.policy_v2"}: raise HTTPException(422,"invalid actionable conditional judgment")
    if not isinstance(item["actions"],dict) or set(item["actions"])!={"no_position","holding"} or not all(isinstance(x,str) and x.strip() for x in item["actions"].values()): raise HTTPException(422,"invalid actionable conditional actions")
    price=item["price_criterion"]
    allowed={"BAD":"card.stop_loss","BASE":"card.price_cap","GOOD":"card.take_profit[0].price"}[item["label"]]
    if not isinstance(price,dict) or set(price)!={"value_krw","basis","provenance"} or not isinstance(price.get("basis"),str) or not price["basis"].strip(): raise HTTPException(422,"invalid actionable conditional price")
    value=price.get("value_krw")
    if value is None:
        if price.get("basis")!="가격 미설정" or price.get("provenance") is not None: raise HTTPException(422,"invalid unavailable conditional price")
    elif not _positive(value) or price.get("provenance") != allowed: raise HTTPException(422,"invalid grounded conditional price")
    if not isinstance(item["checks"],list) or any(not _source(x) for x in item["checks"]): raise HTTPException(422,"invalid actionable conditional checks")

def _validate(data):
    keys={"kind","schema_version","event_identity","version","symbol","event_type","evidence_id","card_id","disclosed_at","known_at","source_url","scenarios"}
    if not isinstance(data,dict) or set(data)!=keys or data.get("kind")!=KIND or data.get("event_type")!=KIND or data.get("schema_version") not in {1,2}: raise HTTPException(422,"invalid conditional scenario set")
    if not isinstance(data["version"],int) or isinstance(data["version"],bool) or data["version"]<1: raise HTTPException(422,"invalid conditional scenario version")
    if not all(isinstance(data.get(x),str) and data[x].strip() for x in ("event_identity","symbol","disclosed_at","known_at")) or not _safe_source_url(data.get("source_url")): raise HTTPException(422,"invalid conditional scenario source fields")
    if any(not isinstance(data.get(x),int) or isinstance(data.get(x),bool) or data[x]<1 for x in ("evidence_id","card_id")): raise HTTPException(422,"conditional scenario lineage required")
    announced,known=_time(data["disclosed_at"]),_time(data["known_at"])
    if announced>known: raise HTTPException(422,"announcement_at must be at or before known_at")
    scenarios=data["scenarios"]
    if not isinstance(scenarios,list) or [x.get("label") if isinstance(x,dict) else None for x in scenarios]!=list(LABELS): raise HTTPException(422,"conditions must be BAD BASE GOOD")
    if data["schema_version"]==1:
        for item in scenarios:
            if set(item)!={"label","conditions"} or not isinstance(item["conditions"],list) or not item["conditions"] or any(not _source(x) for x in item["conditions"]): raise HTTPException(422,"invalid conditional scenario conditions")
    else:
        for item in scenarios: _validate_v2_item(item)
    return data

def create(db,data,*,commit=True):
    d=_validate(data); owns_transaction=False
    try:
        if commit:
            if db.in_transaction: raise HTTPException(409,"conditional scenario requires a new owner transaction")
            try: db.execute("BEGIN IMMEDIATE")
            except Exception as exc: raise HTTPException(409,"conditional scenario write reservation unavailable") from exc
            owns_transaction=True
        elif not db.in_transaction: raise HTTPException(409,"conditional scenario requires an active owner transaction")
        else:
            try: db.execute("UPDATE event_conditional_scenario_sets SET frozen_at=frozen_at WHERE 0")
            except Exception as exc: raise HTTPException(409,"conditional scenario write reservation unavailable") from exc
        ev=db.execute("SELECT * FROM material_evidence WHERE id=?",(d["evidence_id"],)).fetchone(); card=db.execute("SELECT * FROM decision_cards WHERE id=?",(d["card_id"],)).fetchone()
        if not ev or not card or card["evidence_id"]!=ev["id"] or card["invalidated_at"] or ev["status"] not in {"card_generated","decision_pending"}: raise HTTPException(409,"conditional scenario requires active current lineage")
        if card["version"] != db.execute("SELECT MAX(version) FROM decision_cards WHERE lineage_key=? AND invalidated_at IS NULL",(card["lineage_key"],)).fetchone()[0]: raise HTTPException(409,"conditional card is not current head")
        if ev["collected_at"] and _time(d["known_at"]) > _time(ev["collected_at"]): raise HTTPException(422,"known_at must be at or before collected_at")
        canonical=build_scenario_candidate(dict(ev), {**dict(card), "card": __import__("json").loads(card["card_json"])})
        if canonical.get("status") != "COMPLETE" or d != canonical["payload"]: raise HTTPException(409,"conditional payload must exactly equal current persisted candidate")
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
    scenario_set=__import__("json").loads(row["scenario_json"])
    return {"id":row["id"],"version":row["version"],"scenario_kind":KIND,"schema_version":scenario_set.get("schema_version",1),"expected_value_krw":None,"scenarios":__import__("json").loads(row["scenarios_json"]),"idempotent":idempotent}
