import os
import tempfile
from datetime import datetime
from zoneinfo import ZoneInfo

os.environ["DATABASE_PATH"] = tempfile.mktemp(suffix=".db")
os.environ["SESSION_SECRET"] = "test-session-secret-that-is-at-least-thirty-two-bytes-long"
from fastapi.testclient import TestClient
from app import app
import kr_stock_autotrader.service as service

KST = ZoneInfo("Asia/Seoul")
NOW = datetime(2026, 8, 28, 10, 0, tzinfo=KST)
c = TestClient(app)


def test_auth_separation_and_ui_smoke():
    assert c.get('/').status_code == 200
    assert '모의투자 전용' in c.get('/').text
    assert c.get('/api/plans').status_code == 401
    assert c.post('/api/signup',json={'email':'a@test.com','password':'long-password'}).status_code == 200
    assert c.post('/api/plans',json={'symbol':'005930','name':'삼성전자','scheduled_at':'2026-08-31T10:00','qty':2,'conditions':[]}).status_code == 200
    other=TestClient(app); other.post('/api/signup',json={'email':'b@test.com','password':'long-password'})
    assert other.get('/api/plans').json()==[]


def test_legacy_plan_is_readable_but_never_paper_filled(monkeypatch):
    monkeypatch.setattr(service, "now_kst", lambda: NOW)
    x=TestClient(app); x.post('/api/signup',json={'email':'flow@test.com','password':'long-password'})
    response=x.post('/api/plans',json={'symbol':'005930','name':'삼성전자','scheduled_at':'2020-01-01T10:00','qty':3,'combine_mode':'OR','conditions':[{'kind':'absolute_price','operator':'>=','value':70000}]})
    assert response.json()['executable'] is False
    plan=response.json()['id']
    tick={'symbol':'005930','price':70000,'volume':100,'baseline_volume':100,'known_at':NOW.isoformat(),'idempotency_key':'once'}
    assert x.post('/api/ticks',json=tick).status_code==200
    row=x.get('/api/plans').json()[0]
    assert row['id']==plan and row['status']=='manual_only' and row['sold_qty']==0 and row['fills']==[]
    x.post('/api/ticks',json={**tick, 'idempotency_key':'next'})
    assert x.get('/api/plans').json()[0]['fills']==[]
