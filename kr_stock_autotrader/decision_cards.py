"""Immutable, fail-closed decision-card domain (paper only)."""
from __future__ import annotations
import hashlib, hmac, json, os, sqlite3
from datetime import timedelta
from pathlib import Path
from fastapi import Header, HTTPException
from .decision_card_schema import REQUIRED_CARD_FIELDS, VERDICTS
from .domain import now_kst, parse_kst, market_open

PROMPT_PATH = Path(__file__).parents[1] / 'prompts' / 'decision-card-v1.md'
def prompt_hash() -> str: return hashlib.sha256(PROMPT_PATH.read_bytes()).hexdigest()
def canon(x): return json.dumps(x, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
def now(): return now_kst().isoformat()
def audit(db, actor, action, typ, ident, detail=''):
    db.execute('INSERT INTO audit_logs(actor,action,entity_type,entity_id,detail,at) VALUES(?,?,?,?,?,?)',(actor,action,typ,str(ident),detail,now()))
def require_internal_api_key(x_internal_api_key: str|None=Header(None), x_internal_key: str|None=Header(None)):
    expected=os.getenv('INTERNAL_API_KEY','')
    supplied=x_internal_api_key or x_internal_key
    if not expected or not supplied or not hmac.compare_digest(supplied, expected): raise HTTPException(403,'internal authorization required')
def row(db, table, ident):
    r=db.execute(f'SELECT * FROM {table} WHERE id=?',(ident,)).fetchone()
    if not r: raise HTTPException(404, f'{table} not found')
    return r

def create_evidence(db, data):
    ts=now(); key=data['dedupe_key']
    try:
      r=db.execute('''INSERT INTO material_evidence(symbol,name,kind,title,summary,source,source_url,announcement_at,collected_at,known_at,snapshot,newness,dedupe_key,status,created_by,updated_at,audit_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,'active',?,?,?) RETURNING id''',
      (data['symbol'],data.get('name'),data['kind'],data['title'],data['summary'],data['source'],data.get('source_url'),data.get('announcement_at'),data.get('collected_at',ts),data['known_at'],canon(data['snapshot']),data.get('newness','new'),key,data.get('created_by','internal'),ts,'[]')).fetchone()
    except sqlite3.IntegrityError: raise HTTPException(409,'duplicate evidence dedupe_key')
    audit(db,data.get('created_by','internal'),'create','material_evidence',r['id']); db.commit(); return evidence_detail(db,r['id'])
def evidence_detail(db, ident):
    d=dict(row(db,'material_evidence',ident)); d['snapshot']=json.loads(d['snapshot']); return d
def list_evidence(db, symbol=None,status=None):
    q='SELECT id FROM material_evidence WHERE 1=1'; p=[]
    if symbol: q+=' AND symbol=?';p.append(symbol)
    if status: q+=' AND status=?';p.append(status)
    return [evidence_detail(db,r['id']) for r in db.execute(q+' ORDER BY id DESC',p)]
def mutate_evidence(db, ident, patch=None, invalidate=False):
    old=evidence_detail(db,ident); ts=now()
    if invalidate: db.execute("UPDATE material_evidence SET status='invalidated',invalidated_at=?,updated_at=? WHERE id=?",(ts,ts,ident))
    elif patch:
      # evidence is versioned by replacement fields; changing it invalidates dependent approvals.
      allowed={'title','summary','snapshot','newness','known_at','announcement_at'}
      for k,v in patch.items():
       if k in allowed: db.execute(f'UPDATE material_evidence SET {k}=?,updated_at=? WHERE id=?',(canon(v) if k=='snapshot' else v,ts,ident))
    invalidate_lineage(db, evidence_id=ident, reason='evidence_mutated')
    audit(db,'internal','invalidate' if invalidate else 'update','material_evidence',ident);db.commit();return evidence_detail(db,ident)

def run_filter(inputs, as_of, known_at):
    reasons=[]
    try: asdt=parse_kst(as_of); kdt=parse_kst(known_at)
    except ValueError: return {'verdict':'FAIL','reasons':['invalid timestamp'],'units':{}}
    if kdt>asdt or asdt-kdt>timedelta(days=1): reasons.append('known_at future or stale')
    def bad(name, test, why):
      if name not in inputs or test(inputs[name]): reasons.append(why)
    bad('trading_status',lambda x:x!='tradable','not tradable');bad('trading_value',lambda x:not isinstance(x,(int,float)) or x<=0,'missing/zero trading value')
    bad('market_cap',lambda x:not isinstance(x,(int,float)) or x<=0,'missing/zero market cap')
    bad('volume_ratio',lambda x:not isinstance(x,(int,float)) or x<=0,'missing/zero volume denominator')
    bad('recent_rise_pct',lambda x:not isinstance(x,(int,float)) or x>float(inputs.get('max_recent_rise_pct',30)),'recent rise too high')
    bad('gap_pct',lambda x:not isinstance(x,(int,float)) or x>float(inputs.get('max_gap_pct',15)),'gap too high')
    bad('pre_announcement_return_pct',lambda x:not isinstance(x,(int,float)) or x>float(inputs.get('max_pre_return_pct',30)),'pre-announcement return too high')
    for k in ('duplicate','recycled','low_certainty_terms','conflicting_bad_news'):
      if inputs.get(k) is True: reasons.append(k)
    if not inputs.get('source') or not inputs.get('announcement_at') or not inputs.get('economic_terms'): reasons.append('source/announcement/economic terms missing')
    return {'verdict':'FAIL' if reasons else 'PASS','reasons':reasons,'computed':{'relative_return_pct':float(inputs.get('benchmark_return_pct',0))-float(inputs.get('sector_return_pct',0))},'units':{'pct':'percent; positive means increase','volume_ratio':'current volume / baseline volume; baseline must be > 0','as_of':'KST ISO-8601'}}
def save_filter(db,evidence_id,inputs,as_of,known_at):
    out=run_filter(inputs,as_of,known_at)
    try:r=db.execute('INSERT INTO deterministic_filter_results(evidence_id,raw_inputs,computed_outputs,as_of,known_at,verdict,reasons,created_at) VALUES(?,?,?,?,?,?,?,?) RETURNING id',(evidence_id,canon(inputs),canon(out),as_of,known_at,out['verdict'],canon(out['reasons']),now())).fetchone()
    except sqlite3.IntegrityError: raise HTTPException(409,'duplicate filter key')
    audit(db,'internal','run','filter',r['id']);db.commit();return filter_detail(db,r['id'])
def filter_detail(db,ident):
 d=dict(row(db,'deterministic_filter_results',ident));d['raw_inputs']=json.loads(d['raw_inputs']);d['computed_outputs']=json.loads(d['computed_outputs']);d['reasons']=json.loads(d['reasons']);return d

def save_card(db,data):
  missing=REQUIRED_CARD_FIELDS-set(data['card'])
  if missing or data['card'].get('verdict') not in VERDICTS: raise HTTPException(422,'structured card required fields/verdict invalid')
  ev=row(db,'material_evidence',data['evidence_id']); fi=row(db,'deterministic_filter_results',data['filter_id'])
  lineage=data.get('lineage_key',f"{ev['symbol']}:{ev['id']}"); version=(db.execute('SELECT COALESCE(MAX(version),0)+1 n FROM decision_cards WHERE lineage_key=?',(lineage,)).fetchone()['n'])
  r=db.execute('INSERT INTO decision_cards(lineage_key,version,evidence_id,filter_id,prompt_version,prompt_hash,model,provider,card_json,verdict,confidence,generated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?) RETURNING id',(lineage,version,ev['id'],fi['id'],data.get('prompt_version','decision-card-v1'),prompt_hash(),data['model'],data['provider'],canon(data['card']),data['card']['verdict'],float(data['card']['confidence']),now())).fetchone()
  audit(db,'internal','save','decision_card',r['id']);db.commit();return card_detail(db,r['id'])
def card_detail(db,ident):
 d=dict(row(db,'decision_cards',ident));d['card']=json.loads(d.pop('card_json'));return d
def list_cards(db): return [card_detail(db,x['id']) for x in db.execute('SELECT id FROM decision_cards ORDER BY id DESC')]
def invalidate_lineage(db,evidence_id=None,card_id=None,reason='mutation'):
 q='SELECT id FROM decision_cards WHERE '+('evidence_id=?' if evidence_id else 'id=?'); p=(evidence_id or card_id,); ids=[x['id'] for x in db.execute(q,p)]
 for cid in ids:
  db.execute('UPDATE decision_cards SET invalidated_at=?,invalidation_reason=? WHERE id=?',(now(),reason,cid));db.execute("UPDATE order_plans SET status='invalidated' WHERE card_id=? AND status='approved'",(cid,))
def user_decision(db,card_id,user_id,decision,note=''):
 c=card_detail(db,card_id);db.execute('INSERT INTO user_decisions(card_id,user_id,decision,decided_at,note) VALUES(?,?,?,?,?) ON CONFLICT(card_id,user_id) DO UPDATE SET decision=excluded.decision,decided_at=excluded.decided_at,note=excluded.note',(card_id,user_id,decision,now(),note))
 if decision!='approve': db.commit();return {'decision':decision}
 if c['invalidated_at']: raise HTTPException(409,'card invalidated; regenerate and reapprove')
 x=c['card']; required=('price_cap','max_amount','max_qty','window')
 if any(k not in x for k in required): raise HTTPException(422,'card order plan fields missing')
 snapshot={k:x.get(k) for k in x}; vh=hashlib.sha256(canon({'card':c['id'],'user':user_id,'snapshot':snapshot}).encode()).hexdigest(); valid=x.get('valid_until') or x.get('holding_until')
 if not valid: raise HTTPException(422,'valid_until/holding_until required')
 db.execute('INSERT INTO order_plans(card_id,card_version,user_id,approved_at,valid_until,symbol,window_start,window_end,price_cap,max_amount,max_qty,split_json,order_type,stop_loss,take_profit_json,evidence_invalidation,holding_until,review_at,expires_at,status,version_hash) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',(c['id'],c['version'],user_id,now(),valid,c['card']['symbol'],x['window'].get('start'),x['window'].get('end'),x['price_cap'],x['max_amount'],x['max_qty'],canon(x.get('split',[])),x.get('order_type','limit'),x.get('stop_loss'),canon(x.get('take_profit',[])),canon(x.get('evidence_invalidation',{})),x.get('holding_until'),x.get('review_at'),valid,'approved',vh))
 audit(db,str(user_id),'approve','decision_card',card_id);db.commit();return {'decision':'approve','order_plan_hash':vh}
def evaluate_order_plan(db,plan_id,tick):
 p=dict(row(db,'order_plans',plan_id)); ts=parse_kst(tick['known_at']); reason=[]
 if p['status']!='approved' or ts>parse_kst(p['expires_at']): reason.append('unapproved_or_expired')
 if not market_open(ts): reason.append('market_closed')
 if tick.get('stale') or tick.get('duplicate') or tick['price']>p['price_cap'] or tick.get('gap_pct',0)>tick.get('max_gap_pct',15) or tick.get('liquidity',0)<=0: reason.append('precheck_failed')
 key=tick['tick_key']; existing=db.execute('SELECT 1 FROM order_evaluations WHERE order_plan_id=? AND tick_key=?',(plan_id,key)).fetchone()
 if existing:return {'idempotent':True,'fills':[]}
 if reason: db.execute('INSERT INTO order_evaluations(order_plan_id,tick_key,result,reasons,evaluated_at) VALUES(?,?,?,?,?)',(plan_id,key,'REJECT',canon(reason),now()));db.commit();return {'fills':[],'reasons':reason}
 # buy first; exits only on a later tick. partial quantity is explicit input, never exceeding caps.
 qty=min(int(tick.get('fill_qty',p['max_qty'])),p['max_qty']-p['bought_qty'])
 side='buy' if p['bought_qty']==0 else ('sell' if tick.get('exit') else None)
 if side=='sell': qty=min(int(tick.get('fill_qty',p['bought_qty']-p['sold_qty'])),p['bought_qty']-p['sold_qty'])
 if not side or qty<=0: db.execute('INSERT INTO order_evaluations(order_plan_id,tick_key,result,reasons,evaluated_at) VALUES(?,?,?,?,?)',(plan_id,key,'NOOP','[]',now()));db.commit();return {'fills':[]}
 fk=f'{plan_id}:{key}:{side}';db.execute('INSERT INTO order_fills(order_plan_id,event_key,side,qty,price,filled_at) VALUES(?,?,?,?,?,?)',(plan_id,fk,side,qty,tick['price'],now()))
 if side=='buy': db.execute('UPDATE order_plans SET bought_qty=bought_qty+?,last_tick_key=? WHERE id=?',(qty,key,plan_id));db.execute('INSERT INTO positions(order_plan_id,symbol,qty,avg_price,status) VALUES(?,?,?,?,?) ON CONFLICT(order_plan_id) DO UPDATE SET qty=qty+excluded.qty,status="open"',(plan_id,p['symbol'],qty,tick['price'],'open'))
 else: db.execute('UPDATE order_plans SET sold_qty=sold_qty+?,last_tick_key=? WHERE id=?',(qty,key,plan_id));db.execute('UPDATE positions SET qty=qty-?,status=CASE WHEN qty-?=0 THEN "closed" ELSE "open" END WHERE order_plan_id=?',(qty,qty,plan_id))
 db.execute('INSERT INTO order_evaluations(order_plan_id,tick_key,result,reasons,evaluated_at) VALUES(?,?,?,?,?)',(plan_id,key,'FILLED',canon([side]),now()));db.commit();return {'fills':[{'side':side,'qty':qty,'price':tick['price']}]}
