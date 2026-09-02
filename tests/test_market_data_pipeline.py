from datetime import datetime
from zoneinfo import ZoneInfo
import pytest

KST = ZoneInfo("Asia/Seoul")

def bar(day, close, volume, value, cap):
    return {"stck_bsop_date": day, "stck_clpr": str(close), "acml_vol": str(volume), "acml_tr_pbmn": str(value), "hts_avls": str(cap)}

def test_snapshot_uses_completed_unique_daily_bars_and_benchmark():
    from kr_stock_autotrader.market_data import build_premarket_snapshot
    as_of = datetime(2026, 9, 2, 8, 0, tzinfo=KST)
    stock = [bar("20260902",999,1,1,1),bar("20260901",110,10,1100,25),bar("20260829",100,9,900,24)]
    benchmark = [bar("20260901",220,1,1,1),bar("20260829",200,1,1,1)]
    result = build_premarket_snapshot("005930",as_of,lambda symbol,*_: stock if symbol=="005930" else benchmark)
    assert result["status"] == "ok" and result["trading_day"] == "2026-09-01"
    assert result["stock_return_pct"] == result["benchmark_return_pct"] == 10.0
    assert result["trading_value_krw"] == 1100.0 and result["market_cap_krw"] == 2_500_000_000.0
    assert result["gap_pct"] is None and result["trading_status"] == "unknown" and result["observability"]["gap_pct"] == "not_yet_observable" and "999" not in str(result)

@pytest.mark.parametrize("bars", [[bar("20260901",110,10,1100,25),bar("20260901",100,9,900,24)],[bar("20260901",110,10,1100,25)]])
def test_snapshot_fails_closed_for_duplicate_or_missing_prior_bar(bars):
    from kr_stock_autotrader.market_data import build_premarket_snapshot
    result=build_premarket_snapshot("005930",datetime(2026,9,2,8,tzinfo=KST),lambda *_:bars)
    assert result["status"] == "unavailable" and set(result) <= {"symbol","status","source","environment","retrieved_at","reason"}

def test_premarket_filter_inputs_use_config_and_unknowns_do_not_fail(monkeypatch):
    from kr_stock_autotrader.market_data import filter_inputs_from_snapshot
    for name,value in {"GIRAFFE_MIN_TRADING_VALUE_KRW":"1000","GIRAFFE_MIN_MARKET_CAP_KRW":"2000000000","GIRAFFE_MAX_MARKET_CAP_KRW":"3000000000","GIRAFFE_MAX_RECENT_RISE_PCT":"20","GIRAFFE_MAX_GAP_PCT":"10","GIRAFFE_MAX_PRE_RETURN_PCT":"20"}.items(): monkeypatch.setenv(name,value)
    snapshot={"status":"ok","symbol":"005930","known_at":"2026-09-02T08:00:00+09:00","stock_return_pct":10.,"benchmark_return_pct":10.,"trading_value_krw":1100.,"market_cap_krw":2_500_000_000.,"recent_rise_pct":10.,"pre_announcement_return_pct":0.,"trading_status":"tradable","gap_pct":None,"current_volume":None,"baseline_volume":None,"sector_return_pct":None,"observability":{"gap_pct":"not_yet_observable","current_volume":"not_yet_observable","sector_return_pct":"unknown"}}
    inputs=filter_inputs_from_snapshot(snapshot)
    assert inputs["min_trading_value"]==1000. and inputs["sector_return_pct"] is None and inputs["gap_pct"] is None

def test_invalid_threshold_config_fails_closed(monkeypatch):
    from kr_stock_autotrader.market_data import filter_inputs_from_snapshot
    monkeypatch.setenv("GIRAFFE_MIN_TRADING_VALUE_KRW","bogus")
    assert filter_inputs_from_snapshot({"status":"ok"})["market_data_status"] == "unavailable"

def test_daily_chart_is_allowlisted_with_official_tr_id():
    from kr_stock_autotrader.kis_readonly import KISReadOnlyClient,DAILY_CHART_PATH,DAILY_CHART_TR_ID,PRODUCTION_BASE_URL
    class Response:
        status_code=200
        def json(self): return {"rt_cd":"0","output2":[]}
    class Transport:
        def __init__(self): self.calls=[]
        def request(self,method,url,**kwargs):
            self.calls.append((method,url,kwargs))
            return Response() if url.endswith(DAILY_CHART_PATH) else type("OAuth",(),{"status_code":200,"json":lambda _:{"access_token":"x","expires_in":60}})()
    transport=Transport()
    assert KISReadOnlyClient("key","secret",transport=transport).daily_bars("005930",datetime(2026,9,2,8,tzinfo=KST))==[]
    assert transport.calls[-1][0:2]==("GET",PRODUCTION_BASE_URL+DAILY_CHART_PATH) and transport.calls[-1][2]["headers"]["tr_id"]==DAILY_CHART_TR_ID

def test_internal_snapshot_api_has_safe_filter_ready_projection(monkeypatch):
    from fastapi.testclient import TestClient
    from kr_stock_autotrader import api
    monkeypatch.setenv("INTERNAL_API_KEY","test-key")
    for name,value in {"GIRAFFE_MIN_TRADING_VALUE_KRW":"1000","GIRAFFE_MIN_MARKET_CAP_KRW":"2000000000","GIRAFFE_MAX_MARKET_CAP_KRW":"3000000000","GIRAFFE_MAX_RECENT_RISE_PCT":"20","GIRAFFE_MAX_GAP_PCT":"10","GIRAFFE_MAX_PRE_RETURN_PCT":"20"}.items(): monkeypatch.setenv(name,value)
    rows=[bar("20260901",110,10,1100,25),bar("20260829",100,9,900,24)]
    monkeypatch.setattr(api.app.state,"kis_daily_bars_provider",lambda *_:rows,raising=False)
    response=TestClient(api.app).post("/api/internal/market-snapshots/005930",headers={"X-Internal-API-Key":"test-key"},json={"as_of":"2026-09-02T08:00:00+09:00"})
    assert response.status_code==200
    payload=response.json()
    assert payload["snapshot"]["status"]=="ok" and payload["filter_inputs"]["observability"]["gap_pct"]=="not_yet_observable" and "output2" not in response.text
