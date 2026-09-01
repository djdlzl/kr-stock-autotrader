import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import pytest
from kr_stock_autotrader.kis_readonly import KISReadOnlyClient, OAUTH_PATH, QUOTE_PATH, QUOTE_TR_ID, PRODUCTION_BASE_URL
from kr_stock_autotrader.live_dry_run import evaluate_live_dry_run

KST=ZoneInfo('Asia/Seoul')
NOW=datetime(2026,8,28,10,0,tzinfo=KST)

class Response:
    def __init__(self,payload,status_code=200): self.payload,self.status_code=payload,status_code
    def json(self): return self.payload
class FakeTransport:
    def __init__(self, responses=None): self.calls=[]; self.responses=list(responses or [])
    def request(self, method, url, **kwargs):
        self.calls.append((method,url,kwargs))
        if self.responses: return self.responses.pop(0)
        if url.endswith(OAUTH_PATH): return Response({'access_token':'secret-token','expires_in':86400,'access_token_token_expired':'2026-08-29T10:00:00+09:00'})
        return Response({'rt_cd':'0','output':{'stck_prpr':'70000','acml_vol':'1234','unwanted':'raw'}})

def test_kis_allowlist_pinned_host_and_safe_actual_projection(monkeypatch):
    t=FakeTransport(); k=KISReadOnlyClient('app-secret-key','app-secret-value',base_url=PRODUCTION_BASE_URL+'/',transport=t)
    q=k.current_price('005930')
    assert q['status']=='ok' and q['symbol']=='005930' and q['price']==70000
    assert q['timestamp_source']=='network_retrieved_at' and q['quote_known_at']==q['retrieved_at']
    assert len(t.calls)==2 and t.calls[0][0:2]==('POST',PRODUCTION_BASE_URL+OAUTH_PATH)
    assert t.calls[1][2]['headers']['tr_id']==QUOTE_TR_ID
    assert 'secret' not in str(q).lower() and 'unwanted' not in q
    with pytest.raises(ValueError): KISReadOnlyClient(base_url='https://fake.test',transport=t)
    monkeypatch.setenv('KIS_BASE_URL','https://169.254.169.254')
    with pytest.raises(ValueError): KISReadOnlyClient(transport=t)
    with pytest.raises(ValueError): k._request('POST','/uapi/domestic-stock/v1/trading/order-cash')
    with pytest.raises(ValueError): k.current_price('00593')

def test_oauth_http_status_expiry_refresh_and_one_auth_retry():
    # expired token forces a fresh OAuth; one 401 invokes exactly one additional OAuth+quote.
    t=FakeTransport([
        Response({'access_token':'old','expires_in':1}), Response({'msg_cd':'EGW00123'},401),
        Response({'access_token':'retry','expires_in':86400}), Response({'rt_cd':'0','output':{'stck_prpr':'70001','acml_vol':'2'}}),
    ])
    q=KISReadOnlyClient('key','value',transport=t).current_price('005930')
    assert q['status']=='ok' and q['price']==70001
    assert [x[0] for x in t.calls] == ['POST','GET','POST','GET']
    failed=FakeTransport([Response({'access_token':'x','expires_in':86400},500)])
    assert KISReadOnlyClient('key','value',transport=failed).current_price('005930')['status']=='unavailable'
    assert len(failed.calls)==1

def plan(**overrides):
    x={'id':1,'card_id':2,'card_version':1,'version_hash':'frozen','status':'approved','symbol':'005930','valid_until':'2026-08-28T12:00:00+09:00','expires_at':'2026-08-28T12:00:00+09:00','window_start':'2026-08-28T09:00:00+09:00','window_end':'2026-08-28T15:00:00+09:00','price_cap':71000,'max_qty':2,'max_amount':140000,'bought_qty':0,'bought_amount':0}
    x.update(overrides); return x
def quote(**overrides):
    x={'status':'ok','symbol':'005930','price':70000,'quote_known_at':NOW.isoformat(),'retrieved_at':NOW.isoformat(),'timestamp_source':'network_retrieved_at'};x.update(overrides);return x

def test_dry_run_uses_network_observation_not_exchange_trade_time():
    assert evaluate_live_dry_run(plan(),False,quote(),server_now=NOW)['result']=='WOULD_SUBMIT'
    assert evaluate_live_dry_run(plan(),False,quote(),server_now=NOW.replace(hour=8))['result']=='WOULD_WAIT'
    assert evaluate_live_dry_run(plan(),False,quote(price=72000),server_now=NOW)['result']=='WOULD_REJECT'
    assert evaluate_live_dry_run(plan(),False,quote(quote_known_at=(NOW-timedelta(minutes=6)).isoformat()),server_now=NOW)['result']=='WOULD_WAIT'
    assert evaluate_live_dry_run(plan(),False,quote(timestamp_source='exchange_trade_time'),server_now=NOW)['result']=='WOULD_WAIT'
    assert evaluate_live_dry_run(plan(),False,quote(halt_status='halt'),server_now=NOW)['result']=='WOULD_WAIT'
    assert evaluate_live_dry_run(plan(status='entry_invalidated'),False,quote(),server_now=NOW)['result']=='WOULD_REJECT'
    r=evaluate_live_dry_run(plan(),True,quote(),server_now=NOW)
    assert r['network_order_calls']==0 and r['broker_mode']=='read_only_dry_run' and r['result']=='WOULD_REJECT'

def test_readiness_legacy_account_alias_is_presence_only(monkeypatch):
    monkeypatch.delenv('KIS_ACCOUNT_NO',raising=False);monkeypatch.delenv('KIS_ACCOUNT_PRODUCT_CODE',raising=False);monkeypatch.delenv('R_ACCOUNT_NUMBER',raising=False)
    assert KISReadOnlyClient.readiness()['account_readiness']=='blocked_missing_account_env'
    monkeypatch.setenv('R_ACCOUNT_NUMBER','12345678');monkeypatch.setenv('KIS_ACCOUNT_PRODUCT_CODE','01')
    assert KISReadOnlyClient.readiness()['account_readiness']=='ready'

def test_successful_quote_cache_is_bounded_and_safe(monkeypatch):
    from kr_stock_autotrader import api
    calls=[]
    def provider(symbol):
        calls.append(symbol)
        return {**quote(), 'raw_secret': 'must-not-cache'}
    monkeypatch.setattr(api.app.state, 'kis_quote_provider', provider, raising=False)
    api._quote_cache.clear()
    assert [api._cached_kis_quote('005930') for _ in range(3)]
    assert calls == ['005930']
    assert 'raw_secret' not in api._cached_kis_quote('005930')
    assert api.LiveDryRunIn(dry_run_key='safe_key-1').dry_run_key == 'safe_key-1'
    with pytest.raises(Exception): api.LiveDryRunIn(dry_run_key='bad key')
