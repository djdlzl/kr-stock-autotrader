"""RED/GREEN contracts for the v2 short-term priced-in promotion gate."""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")
V2 = "giraffe-premarket-filter-v2-short-term-priced-in"


def bar(day, close):
    return {"stck_bsop_date": day.strftime("%Y%m%d"), "stck_clpr": str(close), "acml_vol": "10", "acml_tr_pbmn": "1100"}


def rows(*, jump=False, start=datetime(2026, 9, 1).date()):
    day, result = start, []
    while len(result) < 30:
        if day.weekday() < 5:
            close = 115 if jump and len(result) == 0 else 100
            result.append(bar(day, close))
        day -= timedelta(days=1)
    return result


def snapshot(rows_):
    from kr_stock_autotrader.kis_readonly import DailySnapshot
    return DailySnapshot(25, tuple(rows_), datetime(2026, 9, 2, 8, 0, 3, tzinfo=KST))


def configure(monkeypatch):
    for name, value in {
        "GIRAFFE_FILTER_CONFIG_VERSION": V2,
        "GIRAFFE_MIN_TRADING_VALUE_KRW": "1000",
        "GIRAFFE_MIN_MARKET_CAP_KRW": "2000000000",
        "GIRAFFE_MAX_MARKET_CAP_KRW": "3000000000",
        "GIRAFFE_MAX_RECENT_RISE_PCT": "30",
        "GIRAFFE_MAX_GAP_PCT": "10",
        "GIRAFFE_MAX_PRE_RETURN_PCT": "30",
        "GIRAFFE_SHORT_TERM_RISE_SESSIONS": "2",
        "GIRAFFE_MAX_SHORT_TERM_EXCESS_RISE_PCT": "10",
    }.items():
        monkeypatch.setenv(name, value)


def test_snapshot_has_two_completed_session_return_window_and_provenance(monkeypatch):
    from kr_stock_autotrader.market_data import build_premarket_snapshot, filter_inputs_from_snapshot
    configure(monkeypatch)
    as_of = datetime(2026, 9, 2, 8, tzinfo=KST)
    market = build_premarket_snapshot("005930", as_of, lambda symbol, *_: snapshot(rows(jump=symbol == "005930")), "2026-09-01T17:00:00+09:00")
    assert market["status"] == "ok"
    assert market["short_term_stock_return_pct"] == 15.0
    assert market["short_term_benchmark_return_pct"] == 0.0
    assert market["short_term_excess_return_pct"] == 15.0
    assert market["short_term_window"] == {"sessions": 2, "start": "2026-08-28", "end": "2026-09-01", "ended_known_at": "2026-09-01T15:30:00+09:00"}
    inputs = filter_inputs_from_snapshot(market)
    assert inputs["filter_config_version"] == V2
    assert inputs["short_term_provenance"] == {"source": "KIS", "daily_bars_known_at": "2026-09-01T15:30:00+09:00", "market_data_known_at": "2026-09-02T08:00:03+09:00"}


def test_short_term_excess_blocks_promotion_and_reports_units_window(monkeypatch):
    from kr_stock_autotrader.decision_cards import run_filter
    from kr_stock_autotrader.market_data import build_premarket_snapshot, filter_inputs_from_snapshot
    configure(monkeypatch)
    as_of = datetime(2026, 9, 2, 8, tzinfo=KST)
    market = build_premarket_snapshot("005930", as_of, lambda symbol, *_: snapshot(rows(jump=symbol == "005930")), "2026-09-01T17:00:00+09:00")
    result = run_filter(filter_inputs_from_snapshot(market) | {"source": "dart", "announcement_at": "2026-09-01T17:00:00+09:00", "economic_terms": "verified"}, market["market_data_known_at"], market["market_data_known_at"])
    assert result["verdict"] == "FAIL"
    assert "short-term excess rise too high" in result["reasons"]
    assert result["computed"]["short_term_stock_return_pct"] == 15.0
    assert result["computed"]["short_term_excess_return_pct"] == 15.0
    assert result["computed"]["short_term_window"]["sessions"] == 2
    assert result["units"]["short_term_excess_return_pct"].startswith("percent;")


def test_card_detail_ui_has_visible_approval_cta_and_separate_sections():
    from kr_stock_autotrader.ui import APP_HTML
    for text in ("재료 정보", "판단 카드", "매수 승인", "승인할 수 없는 이유", "short_term_excess_return_pct", "short_term_window"):
        assert text in APP_HTML
    assert "buy?draftForm(values,plan,id)" not in APP_HTML
    for text in ("material-section", "judgment-section", "#f59e0b", "#2563eb"):
        assert text in APP_HTML


def test_manager_card7_completed_close_denominator_order_and_boundaries(monkeypatch):
    """Card 7's supplied rows prove the v2 gate uses only completed close data."""
    from kr_stock_autotrader.market_data import build_premarket_snapshot
    configure(monkeypatch)
    as_of = datetime(2026, 9, 3, 8, tzinfo=KST)
    stock, benchmark = rows(start=datetime(2026, 9, 2).date()), rows(start=datetime(2026, 9, 2).date())
    # KIS may return reverse chronological order; the two-session denominator is
    # the 2026-08-31 completed close, never the 09-01 intraday high.
    for series, closes in ((stock, (1730, 1693, 1544)), (benchmark, (13620, 13950, 14370))):
        for item, close in zip(series[:3], closes): item["stck_clpr"] = str(close)
    def provider(symbol, *_):
        return __import__("kr_stock_autotrader.kis_readonly", fromlist=["DailySnapshot"]).DailySnapshot(
            25, tuple(reversed(stock if symbol == "013700" else benchmark)), datetime(2026, 9, 3, 8, 0, 3, tzinfo=KST))
    market = build_premarket_snapshot("013700", as_of, provider, "2026-09-02T18:00:00+09:00")
    assert market["status"] == "ok"
    assert market["short_term_window"] == {"sessions": 2, "start": "2026-08-31", "end": "2026-09-02", "ended_known_at": "2026-09-02T15:30:00+09:00"}
    assert (market["short_term_stock_return_pct"], market["short_term_benchmark_return_pct"], market["short_term_excess_return_pct"]) == (12.04663212, -5.21920668, 17.2658388)


def test_short_term_gate_fails_closed_for_duplicate_missing_or_future_boundary(monkeypatch):
    from kr_stock_autotrader.market_data import build_premarket_snapshot
    configure(monkeypatch)
    as_of = datetime(2026, 9, 2, 8, tzinfo=KST)
    base = rows()
    duplicate = [dict(x) for x in base]; duplicate[1]["stck_bsop_date"] = duplicate[0]["stck_bsop_date"]
    missing = [x for x in base if x["stck_bsop_date"] != "20260828"]
    for bad in (duplicate, missing):
        market = build_premarket_snapshot("005930", as_of, lambda *_: snapshot(bad), "2026-09-01T17:00:00+09:00")
        assert market["status"] == "unavailable"
    # A decision cannot use a session whose close is still after its known-at.
    market = build_premarket_snapshot("005930", as_of, lambda *_: snapshot(base), "2026-09-01T17:00:00+09:00")
    from kr_stock_autotrader.decision_cards import run_filter
    inputs = __import__("kr_stock_autotrader.market_data", fromlist=["filter_inputs_from_snapshot"]).filter_inputs_from_snapshot(market)
    result = run_filter(inputs | {"source":"dart", "announcement_at":"2026-09-01T17:00:00+09:00", "economic_terms":"verified", "short_term_window": inputs["short_term_window"] | {"ended_known_at":"2026-09-02T15:30:00+09:00"}}, market["market_data_known_at"], market["market_data_known_at"])
    assert result["verdict"] == "FAIL" and "short-term window known_at future" in result["reasons"]


def test_v2_contract_requires_exactly_two_sessions_in_config_snapshot_and_filter(monkeypatch):
    from kr_stock_autotrader.decision_cards import run_filter
    from kr_stock_autotrader.market_data import build_premarket_snapshot, filter_inputs_from_snapshot
    configure(monkeypatch)
    as_of = datetime(2026, 9, 2, 8, tzinfo=KST)
    market = build_premarket_snapshot("005930", as_of, lambda *_: snapshot(rows()), "2026-09-01T17:00:00+09:00")
    inputs = filter_inputs_from_snapshot(market) | {"source": "dart", "announcement_at": "2026-09-01T17:00:00+09:00", "economic_terms": "verified"}
    monkeypatch.setenv("GIRAFFE_SHORT_TERM_RISE_SESSIONS", "3")
    assert build_premarket_snapshot("005930", as_of, lambda *_: snapshot(rows()), "2026-09-01T17:00:00+09:00")["status"] == "unavailable"
    assert filter_inputs_from_snapshot(market) == {"market_data_status": "unavailable", "market_data_attempted_at": market["retrieved_at"]}
    result = run_filter(inputs | {"short_term_rise_sessions": 3, "short_term_window": inputs["short_term_window"] | {"sessions": 3}}, market["market_data_known_at"], market["market_data_known_at"])
    assert result["verdict"] == "FAIL"
    assert "short-term session window must be exactly two completed sessions" in result["reasons"]


def test_v2_filter_recomputes_excess_and_rejects_forged_value(monkeypatch):
    from kr_stock_autotrader.decision_cards import run_filter
    from kr_stock_autotrader.market_data import build_premarket_snapshot, filter_inputs_from_snapshot
    configure(monkeypatch)
    as_of = datetime(2026, 9, 2, 8, tzinfo=KST)
    market = build_premarket_snapshot("005930", as_of, lambda *_: snapshot(rows()), "2026-09-01T17:00:00+09:00")
    inputs = filter_inputs_from_snapshot(market) | {"source": "dart", "announcement_at": "2026-09-01T17:00:00+09:00", "economic_terms": "verified", "short_term_stock_return_pct": 20, "short_term_benchmark_return_pct": 0, "short_term_excess_return_pct": 0}
    result = run_filter(inputs, market["market_data_known_at"], market["market_data_known_at"])
    assert result["verdict"] == "FAIL"
    assert "short-term excess return mismatch" in result["reasons"]
    assert "short-term excess rise too high" in result["reasons"]
    assert result["computed"]["short_term_excess_return_pct"] == 20.0


def test_v2_excess_precision_boundary_uses_eight_decimal_producer_precision(monkeypatch):
    from kr_stock_autotrader.decision_cards import run_filter
    from kr_stock_autotrader.market_data import build_premarket_snapshot, filter_inputs_from_snapshot
    configure(monkeypatch)
    as_of = datetime(2026, 9, 2, 8, tzinfo=KST)
    market = build_premarket_snapshot("005930", as_of, lambda *_: snapshot(rows()), "2026-09-01T17:00:00+09:00")
    base = filter_inputs_from_snapshot(market) | {"source": "dart", "announcement_at": "2026-09-01T17:00:00+09:00", "economic_terms": "verified", "short_term_stock_return_pct": 10, "short_term_benchmark_return_pct": 0}
    accepted = run_filter(base | {"short_term_excess_return_pct": 10.000000004}, market["market_data_known_at"], market["market_data_known_at"])
    rejected = run_filter(base | {"short_term_excess_return_pct": 10.000000006}, market["market_data_known_at"], market["market_data_known_at"])
    assert accepted["verdict"] == "PASS"
    assert accepted["computed"]["short_term_excess_return_pct"] == 10.0
    assert rejected["verdict"] == "FAIL"
    assert "short-term excess return mismatch" in rejected["reasons"]
