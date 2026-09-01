import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import pytest
from kr_stock_autotrader.kis_readonly import KISReadOnlyClient, OAUTH_PATH, QUOTE_PATH, QUOTE_TR_ID
from kr_stock_autotrader.live_dry_run import evaluate_live_dry_run

KST=ZoneInfo('Asia/Seoul')
NOW=datetime(2026,8,28,10,0,tzinfo=KST)

class Response:
    def __init__(self,payload): self.payload=payload
    def json(self): return self.payload
class FakeTransport:
    def __init__(self): self.calls=[]
    def request(self, method, url, **kwargs):
        self.calls.append((method,url,kwargs))
        if url.endswith(OAUTH_PATH): return Response({'access_token':'secret-token'})
        return Response({'rt_cd':'0','output':{'stck_prpr':'70000','acml_vol':'1234','stck_cntg_hour':'100000','unwanted':'raw'}})

def test_kis_allowlist_and_safe_projection():
    t=FakeTransport(); k=KISReadOnlyClient('app-secret-key','app-secret-value',base_url='https://fake.test',transport=t)
    q=k.current_price('005930')
    assert q['status']=='ok' and q['symbol']=='005930' and q['price']==70000
    assert len(t.calls)==2 and t.calls[0][0:2]==('POST','https://fake.test'+OAUTH_PATH)
    assert t.calls[1][2]['headers']['tr_id']==QUOTE_TR_ID
    assert 'secret' not in str(q).lower() and 'unwanted' not in q
    with pytest.raises(ValueError): k._request('POST','/uapi/domestic-stock/v1/trading/order-cash')
    with pytest.raises(ValueError): k.current_price('00593')

def plan(**overrides):
    x={'id':1,'card_id':2,'card_version':1,'version_hash':'frozen','status':'approved','symbol':'005930','valid_until':'2026-08-28T12:00:00+09:00','expires_at':'2026-08-28T12:00:00+09:00','window_start':'2026-08-28T09:00:00+09:00','window_end':'2026-08-28T15:00:00+09:00','price_cap':71000,'max_qty':2,'max_amount':140000,'bought_qty':0,'bought_amount':0}
    x.update(overrides); return x
def quote(**overrides):
    x={'status':'ok','symbol':'005930','price':70000,'quote_known_at':NOW.isoformat(),'retrieved_at':NOW.isoformat()};x.update(overrides);return x

def test_dry_run_outcomes_are_pure_and_fail_closed():
    assert evaluate_live_dry_run(plan(),False,quote(),server_now=NOW)['result']=='WOULD_SUBMIT'
    assert evaluate_live_dry_run(plan(),False,quote(),server_now=NOW.replace(hour=8))['result']=='WOULD_WAIT'
    assert evaluate_live_dry_run(plan(),False,quote(price=72000),server_now=NOW)['result']=='WOULD_REJECT'
    assert evaluate_live_dry_run(plan(),False,quote(quote_known_at=(NOW-timedelta(minutes=6)).isoformat()),server_now=NOW)['result']=='WOULD_WAIT'
    assert evaluate_live_dry_run(plan(status='entry_invalidated'),False,quote(),server_now=NOW)['result']=='WOULD_REJECT'
    r=evaluate_live_dry_run(plan(),True,quote(),server_now=NOW)
    assert r['network_order_calls']==0 and r['broker_mode']=='read_only_dry_run' and r['result']=='WOULD_REJECT'

def test_readiness_legacy_account_alias_is_presence_only(monkeypatch):
    monkeypatch.delenv('KIS_ACCOUNT_NO',raising=False);monkeypatch.delenv('KIS_ACCOUNT_PRODUCT_CODE',raising=False);monkeypatch.delenv('R_ACCOUNT_NUMBER',raising=False)
    assert KISReadOnlyClient.readiness()['account_readiness']=='blocked_missing_account_env'
    monkeypatch.setenv('R_ACCOUNT_NUMBER','12345678');monkeypatch.setenv('KIS_ACCOUNT_PRODUCT_CODE','01')
    assert KISReadOnlyClient.readiness()['account_readiness']=='ready'
