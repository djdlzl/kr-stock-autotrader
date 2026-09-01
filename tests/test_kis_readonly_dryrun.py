import os
import tempfile
from concurrent.futures import ThreadPoolExecutor
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
    x={'status':'ok','symbol':'005930','price':70000,'volume':1234,'quote_known_at':NOW.isoformat(),'retrieved_at':NOW.isoformat(),'timestamp_source':'network_retrieved_at'};x.update(overrides);return x

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

@pytest.mark.parametrize("timestamp_overrides", [
    {"quote_known_at": "2099-01-01T10:00:00+09:00", "retrieved_at": "2099-01-01T10:00:00+09:00"},
    {"quote_known_at": (NOW + timedelta(days=1)).isoformat(), "retrieved_at": (NOW + timedelta(days=1)).isoformat()},
    {"quote_known_at": (NOW - timedelta(minutes=5, seconds=1)).isoformat(), "retrieved_at": (NOW - timedelta(minutes=5, seconds=1)).isoformat()},
    {"quote_known_at": NOW.isoformat(), "retrieved_at": (NOW - timedelta(seconds=1)).isoformat()},
])
def test_timestamp_invalid_success_is_unavailable_and_never_cached(monkeypatch, timestamp_overrides):
    from kr_stock_autotrader import api

    calls = []
    def provider(symbol):
        calls.append(symbol)
        return quote(symbol=symbol, **timestamp_overrides)

    monkeypatch.setattr(api, "now_kst", lambda: NOW)
    monkeypatch.setattr(api.app.state, "kis_quote_provider", provider, raising=False)
    api._quote_cache.clear()
    assert api._cached_kis_quote("005930")["status"] == "unavailable"
    assert api._quote_cache == {}
    assert api._cached_kis_quote("005930")["status"] == "unavailable"
    assert calls == ["005930", "005930"]


def test_current_coherent_quote_is_cached(monkeypatch):
    from kr_stock_autotrader import api

    monkeypatch.setattr(api, "now_kst", lambda: NOW)
    monkeypatch.setattr(api.app.state, "kis_quote_provider", lambda symbol: quote(symbol=symbol), raising=False)
    api._quote_cache.clear()
    assert api._cached_kis_quote("005930")["status"] == "ok"
    assert len(api._quote_cache) == 1


def test_successful_quote_cache_is_bounded_and_safe(monkeypatch):
    from kr_stock_autotrader import api
    monkeypatch.setattr(api, "now_kst", lambda: NOW)
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


@pytest.mark.parametrize("field,bad_value", [
    ("symbol", {"nested": "KIS-TYPED-PROJECTION-SECRET"}),
    ("price", {"nested": "KIS-TYPED-PROJECTION-SECRET"}),
    ("volume", {"nested": "KIS-TYPED-PROJECTION-SECRET"}),
    ("quote_known_at", {"nested": "KIS-TYPED-PROJECTION-SECRET"}),
    ("retrieved_at", {"nested": "KIS-TYPED-PROJECTION-SECRET"}),
    ("timestamp_source", {"nested": "KIS-TYPED-PROJECTION-SECRET"}),
    ("market_status", {"nested": "KIS-TYPED-PROJECTION-SECRET"}),
    ("halt_status", {"nested": "KIS-TYPED-PROJECTION-SECRET"}),
    ("management_status", {"nested": "KIS-TYPED-PROJECTION-SECRET"}),
    ("symbol", "000001"), ("price", True), ("volume", False),
    ("price", float("nan")), ("price", float("inf")), ("volume", float("-inf")),
    ("price", -1), ("volume", -1), ("quote_known_at", "2026-08-28T10:00:00"),
    ("retrieved_at", "2026-08-28T10:00:00"), ("timestamp_source", "exchange_trade_time"),
    ("quote_known_at", "2026-08-28T10:00:00+09:00\x00KIS-TYPED-PROJECTION-SECRET"),
])
def test_malformed_success_is_closed_typed_unavailable_not_cached_or_leaked(monkeypatch, caplog, field, bad_value):
    from fastapi.testclient import TestClient
    from kr_stock_autotrader import api, db as dbmod
    from kr_stock_autotrader.auth import issue_session
    from tests.test_decision_card_invariants import make_plan

    secret = "KIS-TYPED-PROJECTION-SECRET"
    path = tempfile.mktemp(suffix=".db")
    monkeypatch.setattr(dbmod, "DATABASE_PATH", path)
    db = dbmod.connect()
    _, plan_id = make_plan(db)
    db.execute("UPDATE order_plans SET valid_until=?, expires_at=? WHERE id=?", ("2099-12-31T23:59:00+09:00", "2099-12-31T23:59:00+09:00", plan_id))
    db.commit(); db.close()
    calls = []

    def provider(symbol):
        calls.append(symbol)
        return {**quote(symbol=symbol), field: bad_value, "source": secret, "environment": secret}

    monkeypatch.setattr(api.app.state, "kis_quote_provider", provider, raising=False)
    api._quote_cache.clear()
    client = TestClient(api.app); client.cookies.set("session", issue_session(1))
    quote_response = client.get("/api/kis/quote/005930")
    dry_response = client.post(f"/api/order-plans/{plan_id}/live-dry-run", json={"dry_run_key": "typed-projection-key"})
    assert quote_response.json()["status"] == "unavailable"
    assert dry_response.json()["result"] == "WOULD_WAIT"
    assert api._quote_cache == {} and calls == ["005930", "005930"]
    check = dbmod.connect()
    try:
        serialized_db = " ".join(str(value) for row in check.execute("SELECT * FROM live_dry_run_receipts") for value in row)
    finally:
        check.close()
    assert secret not in str(quote_response.json()) + str(dry_response.json()) + serialized_db + caplog.text


def test_success_projection_normalizes_numbers_omits_statuses_and_revalidates_cache(monkeypatch):
    from kr_stock_autotrader import api
    monkeypatch.setattr(api, "now_kst", lambda: NOW)
    api._quote_cache.clear()
    calls = []

    def provider(symbol):
        calls.append(symbol)
        return {**quote(symbol=symbol, price=70000, volume=1234), "market_status": "normal", "halt_status": "none", "management_status": "none"}

    monkeypatch.setattr(api.app.state, "kis_quote_provider", provider, raising=False)
    result = api._cached_kis_quote("005930")
    assert result["status"] == "ok" and result["price"] == 70000.0 and result["volume"] == 1234.0
    assert not {"market_status", "halt_status", "management_status"} & set(result)
    key = next(iter(api._quote_cache))
    api._quote_cache[key] = (api.monotonic_time.monotonic(), {**result, "price": {"nested": "KIS-TYPED-PROJECTION-SECRET"}})
    assert api._cached_kis_quote("005930")["status"] == "unavailable"
    assert calls == ["005930"]


def test_quote_single_flight_and_cache_and_lock_registries_are_bounded(monkeypatch):
    from kr_stock_autotrader import api
    monkeypatch.setattr(api, "now_kst", lambda: NOW)
    calls = []

    def provider(symbol):
        calls.append(symbol)
        return {**quote(symbol=symbol), 'raw_secret': 'must-not-cache'}

    monkeypatch.setattr(api.app.state, 'kis_quote_provider', provider, raising=False)
    api._quote_cache.clear()
    api._quote_locks.clear()
    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(api._cached_kis_quote, ['005930'] * 3))
    assert calls == ['005930']
    assert results == [results[0]] * 3
    assert 'raw_secret' not in results[0]
    assert api._quote_locks == {}

    for number in range(api.QUOTE_CACHE_MAX_ENTRIES + 100):
        api._cached_kis_quote(f'{number:06d}')
    assert len(api._quote_cache) <= api.QUOTE_CACHE_MAX_ENTRIES
    assert api._quote_locks == {}

    api._dry_run_locks.clear()
    def use_unique_lock(number):
        with api._dry_run_lock(number, 1, f'key-{number:03d}'):
            pass
    with ThreadPoolExecutor(max_workers=24) as pool:
        list(pool.map(use_unique_lock, range(400)))
    assert api._dry_run_locks == {}


def test_readiness_requires_structurally_valid_presence_only_aliases(monkeypatch):
    for name in ('KIS_ACCOUNT_NO', 'R_ACCOUNT_NUMBER', 'KIS_ACCOUNT_PRODUCT_CODE', 'LS_ACCOUNT'):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv('LS_ACCOUNT', '12345678')
    monkeypatch.setenv('KIS_ACCOUNT_PRODUCT_CODE', '01')
    assert KISReadOnlyClient.readiness()['account_readiness'] == 'blocked_missing_account_env'
    for account in ('not-an-account', '1234567', '123456789'):
        monkeypatch.setenv('KIS_ACCOUNT_NO', account)
        assert KISReadOnlyClient.readiness()['account_readiness'] == 'blocked_missing_account_env'
    monkeypatch.setenv('KIS_ACCOUNT_NO', '12345678')
    for product in ('x', '001', ' 1'):
        monkeypatch.setenv('KIS_ACCOUNT_PRODUCT_CODE', product)
        assert KISReadOnlyClient.readiness()['account_readiness'] == 'blocked_missing_account_env'
    monkeypatch.setenv('KIS_ACCOUNT_PRODUCT_CODE', '01')
    assert KISReadOnlyClient.readiness()['account_readiness'] == 'ready'


def test_concurrent_dry_run_has_one_provider_call_and_one_receipt(monkeypatch):
    from fastapi.testclient import TestClient
    from kr_stock_autotrader import api, db as dbmod
    from kr_stock_autotrader.auth import issue_session
    from tests.test_decision_card_invariants import make_plan

    path = tempfile.mktemp(suffix='.db')
    monkeypatch.setattr(dbmod, 'DATABASE_PATH', path)
    db = dbmod.connect()
    _, plan_id = make_plan(db)
    db.close()
    calls = []

    def provider(symbol):
        calls.append(symbol)
        return {**quote(symbol=symbol), 'raw_secret': 'must-not-persist'}

    monkeypatch.setattr(api.app.state, 'kis_quote_provider', provider, raising=False)
    api._quote_cache.clear()
    api._dry_run_locks.clear()
    key = 'same-key-123'

    def request_once(_):
        client = TestClient(api.app)
        client.cookies.set('session', issue_session(1))
        return client.post(f'/api/order-plans/{plan_id}/live-dry-run', json={'dry_run_key': key})

    with ThreadPoolExecutor(max_workers=3) as pool:
        responses = list(pool.map(request_once, range(3)))
    assert [response.status_code for response in responses] == [200, 200, 200]
    assert calls == ['005930']
    check = dbmod.connect()
    try:
        assert check.execute('SELECT count(*) AS total FROM live_dry_run_receipts').fetchone()['total'] == 1
    finally:
        check.close()
    assert api._dry_run_locks == {}


@pytest.mark.parametrize("outcome", [
    lambda secret: {"status": "unavailable", "retrieved_at": NOW.isoformat(), "timestamp_source": "network_retrieved_at", "app_secret": secret, "authorization": secret, "cookie": secret, "token": secret, "raw": {"nested": secret}},
    lambda secret: {"status": "not-a-status", "retrieved_at": "not-a-timestamp", "timestamp_source": "unapproved", "app_secret": secret, "authorization": secret, "cookie": secret, "token": secret, "raw": {"nested": secret}},
    lambda secret: [secret, {"raw": secret}],
    lambda secret: RuntimeError(f"provider failure {secret}"),
])
def test_failure_provider_outcomes_are_sanitized_at_quote_and_dry_run_boundaries(monkeypatch, caplog, outcome):
    from fastapi.testclient import TestClient
    from kr_stock_autotrader import api, db as dbmod
    from kr_stock_autotrader.auth import issue_session
    from tests.test_decision_card_invariants import make_plan

    secret = "KIS-LEAK-MARKER-DO-NOT-RETURN"
    path = tempfile.mktemp(suffix=".db")
    monkeypatch.setattr(dbmod, "DATABASE_PATH", path)
    db = dbmod.connect()
    _, plan_id = make_plan(db)
    db.execute("UPDATE order_plans SET valid_until=?, expires_at=? WHERE id=?", ("2099-12-31T23:59:00+09:00", "2099-12-31T23:59:00+09:00", plan_id))
    db.commit()
    db.close()
    calls = []

    def provider(symbol):
        calls.append(symbol)
        result = outcome(secret)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(api.app.state, "kis_quote_provider", provider, raising=False)
    api._quote_cache.clear()
    client = TestClient(api.app)
    client.cookies.set("session", issue_session(1))
    quote_response = client.get("/api/kis/quote/005930")
    dry_response = client.post(f"/api/order-plans/{plan_id}/live-dry-run", json={"dry_run_key": "failure-safe-key"})
    assert quote_response.status_code == dry_response.status_code == 200
    safe_quote = quote_response.json()
    assert safe_quote["symbol"] == "005930"
    assert safe_quote["status"] == "unavailable"
    assert safe_quote["source"] == "KIS" and safe_quote["environment"] == "production"
    assert set(safe_quote) <= {"symbol", "status", "retrieved_at", "timestamp_source", "source", "environment"}
    assert dry_response.json()["result"] == "WOULD_WAIT"
    assert dry_response.json()["network_order_calls"] == 0
    assert calls == ["005930", "005930"]  # failures are sanitized but never cached
    check = dbmod.connect()
    try:
        serialized_db = " ".join(str(value) for row in check.execute("SELECT * FROM live_dry_run_receipts") for value in row)
    finally:
        check.close()
    assert secret not in str(quote_response.json()) + str(dry_response.json()) + serialized_db + caplog.text
