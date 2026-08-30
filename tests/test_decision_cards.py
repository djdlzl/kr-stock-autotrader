import os, tempfile
from datetime import datetime
from zoneinfo import ZoneInfo
import pytest
from fastapi.testclient import TestClient
from kr_stock_autotrader import db as dbmod
from kr_stock_autotrader.decision_cards import run_filter, create_evidence, save_filter, save_card, user_decision, evaluate_order_plan, prompt_hash

KST=ZoneInfo('Asia/Seoul')
@pytest.fixture
def db(monkeypatch):
 p=tempfile.mktemp(suffix='.db'); monkeypatch.setattr(dbmod,'DATABASE_PATH',p); d=dbmod.connect(); yield d; d.close()
def evidence(d):
 return create_evidence(d,{'symbol':'005930','kind':'disclosure','title':'x','summary':'x','source':'dart','snapshot':{'x':1},'dedupe_key':'unique','known_at':'2026-08-31T08:00:00+09:00'})
def inputs(): return {'trading_status':'tradable','trading_value':1,'market_cap':1,'volume_ratio':1,'recent_rise_pct':1,'gap_pct':1,'pre_announcement_return_pct':1,'source':'dart','announcement_at':'x','economic_terms':'x'}
def card(e,f): return {'evidence_id':e,'filter_id':f,'model':'m','provider':'p','card':{'symbol':'005930','headline':'h','conclusion':'c','change':'c','source_urls':[],'business_value':'x','certainty':'x','priced_in':'x','filter_verdict':'PASS','price_cap':100,'window':{},'max_amount':1000,'max_qty':5,'stop_loss':1,'take_profit':[],'evidence_invalidation':{},'holding_until':'2026-09-01T10:00:00+09:00','review_at':'2026-09-01T09:00:00+09:00','false_positive':'x','unknowns':'x','verdict':'매수 검토 가능','confidence':.5}}
def test_filter_fail_closed_and_prompt_file_hash():
 assert run_filter({**inputs(),'volume_ratio':0},'2026-08-31T09:00:00+09:00','2026-08-31T08:00:00+09:00')['verdict']=='FAIL'
 assert run_filter(inputs(),'2026-08-31T09:00:00+09:00','2026-08-31T10:00:00+09:00')['verdict']=='FAIL'
 assert len(prompt_hash())==64
def test_evidence_dedupe_immutable_card_approval_and_buy_then_exit(db):
 e=evidence(db)
 with pytest.raises(Exception): evidence(db)
 f=save_filter(db,e['id'],inputs(),'2026-08-31T09:00:00+09:00','2026-08-31T08:00:00+09:00')
 c=save_card(db,card(e['id'],f['id']))
 db.execute("INSERT INTO users(email,password) VALUES('u','p')");db.commit()
 approved=user_decision(db,c['id'],1,'approve'); plan=db.execute('SELECT id FROM order_plans').fetchone()['id']
 assert approved['order_plan_hash']
 first=evaluate_order_plan(db,plan,{'tick_key':'a','known_at':'2026-08-31T10:00:00+09:00','price':90,'liquidity':1,'fill_qty':2})
 assert first['fills'][0]['side']=='buy'
 assert evaluate_order_plan(db,plan,{'tick_key':'a','known_at':'2026-08-31T10:00:00+09:00','price':90,'liquidity':1})['idempotent']
 second=evaluate_order_plan(db,plan,{'tick_key':'b','known_at':'2026-08-31T10:01:00+09:00','price':90,'liquidity':1,'exit':True,'fill_qty':1})
 assert second['fills'][0]['side']=='sell' and second['fills'][0]['qty']==1
def test_internal_writes_fail_closed(monkeypatch):
 monkeypatch.setenv('INTERNAL_API_KEY','secret'); from app import app
 c=TestClient(app); payload={'symbol':'005930','kind':'x','title':'x','summary':'x','source':'x','snapshot':{},'dedupe_key':'api','known_at':'2026-08-31T08:00:00+09:00'}
 assert c.post('/api/internal/evidence',json=payload).status_code==403
 assert c.post('/api/internal/evidence',json=payload,headers={'X-Internal-API-Key':'secret'}).status_code==200
