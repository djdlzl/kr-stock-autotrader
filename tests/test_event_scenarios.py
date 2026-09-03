"""Synthetic fixtures only; no performance/OOS claim is made."""
from copy import deepcopy
from datetime import timedelta
from fastapi.testclient import TestClient
from kr_stock_autotrader import db as dbmod
from kr_stock_autotrader.domain import parse_kst


def payload():
    scenarios=[]
    for label, probability, contract, price in (("BAD", .2, 1000, 80), ("BASE", .5, 2000, 100), ("GOOD", .3, 3000, 120)):
        scenarios.append({
            "label":label,"probability":probability,
            "confirmed":{"contract_amount_krw":contract,"margin_pct":.2,"tax_rate":.2,"execution_probability":.8,"period_years":1,"discount_rate":.1,"working_capital_krw":10,"financing_cost_krw":5},
            "option":{"value_krw":0,"reason":"후속 수주 미확인"},
            "revenue_krw":contract,"operating_profit_krw":contract*.2,"net_income_krw":(contract*.2-15)*.8*.8,
            "eps_krw":((contract*.2-15)*.8*.8)/100,"per_share_value_krw":price,
            "return_range_pct":{"low":-5,"high":20},"price_path":{"open":price,"close":price,"day_5":price,"day_20":price},
            "gates":{"entry":price-2,"add":price,"reduce":price+5,"withdraw":price-5},
            "assumptions":["synthetic"],"unknowns":["volume"],"invalidations":["contract_cancelled"]
        })
    return {"event_identity":"DART:2026-08-31:synthetic","version":1,"symbol":"123456","event_type":"SUPPLY_CONTRACT","profile":{"id":"SUPPLY_CONTRACT","version":1},"disclosed_at":"2026-08-30T09:00:00+09:00","known_at":"2026-08-31T09:00:00+09:00","source_url":"https://example.test/disclosure","source_receipt":"synthetic-receipt","baseline":{"price_krw":90,"price_known_at":"2026-08-31T08:59:00+09:00","shares_outstanding":100,"shares_known_at":"2026-08-31T08:59:00+09:00","market_cap_krw":9000,"market_cap_known_at":"2026-08-31T08:59:00+09:00","financial_as_of":"2026-06-30","denominator":"shares_outstanding"},"scenarios":scenarios}


def test_scenario_api_is_immutable_recalculates_and_has_no_order_side_effects(monkeypatch, tmp_path):
    monkeypatch.setattr(dbmod, "DATABASE_PATH", str(tmp_path / "scenario.db"))
    monkeypatch.setenv("INTERNAL_API_KEY", "scenario-key")
    from app import app
    client=TestClient(app); headers={"X-Internal-API-Key":"scenario-key"}
    data=payload(); data["scenarios"][0]["derived"]={"expected_value_krw":999999}
    created=client.post("/api/internal/scenario-sets",json=data,headers=headers)
    assert created.status_code == 200, created.text
    out=created.json(); assert out["expected_value_krw"] != 999999 and [x["label"] for x in out["scenarios"]]==["BAD","BASE","GOOD"]
    assert client.get("/api/internal/scenario-sets/DART:2026-08-31:synthetic",headers=headers).status_code == 200
    assert client.post("/api/internal/scenario-sets",json=data,headers=headers).status_code == 409
    bad=deepcopy(data); bad["event_identity"]="bad-prob"; bad["scenarios"][2]["probability"]=.4
    assert client.post("/api/internal/scenario-sets",json=bad,headers=headers).status_code == 422
    bad=deepcopy(data); bad["event_identity"]="bad-order"; bad["scenarios"][0]["per_share_value_krw"]=130
    assert client.post("/api/internal/scenario-sets",json=bad,headers=headers).status_code == 422
    before=client.get("/api/internal/scenario-sets/DART:2026-08-31:synthetic",headers=headers).json()
    future=deepcopy(data); future["event_identity"]="future"; future["known_at"]="2099-01-01T00:00:00+09:00"
    assert client.post("/api/internal/scenario-sets",json=future,headers=headers).status_code == 422
    before_freeze=client.post(f"/api/internal/scenario-sets/{out['id']}/observations",json={"known_at":"2026-08-31T10:00:00+09:00","price_krw":90,"benchmark_excess_pct":0,"sector_excess_pct":0,"volume_ratio":1},headers=headers)
    assert before_freeze.status_code == 422
    derived_mismatch=deepcopy(data); derived_mismatch["event_identity"]="derived-mismatch"; derived_mismatch["scenarios"][0]["net_income_krw"]=999
    assert client.post("/api/internal/scenario-sets",json=derived_mismatch,headers=headers).status_code == 422
    observed_at=out["frozen_at"]
    observation={"known_at":observed_at,"price_krw":120,"benchmark_excess_pct":3,"sector_excess_pct":3,"volume_ratio":2}
    observed=client.post(f"/api/internal/scenario-sets/{out['id']}/observations",json=observation,headers=headers)
    assert observed.status_code == 200, observed.text
    assert observed.json()["match"] == "GOOD_MATCH"
    db=dbmod.connect()
    assert db.execute("SELECT count(*) FROM order_plans").fetchone()[0] == 0
    assert db.execute("SELECT count(*) FROM order_fills").fetchone()[0] == 0
    assert db.execute("SELECT count(*) FROM positions").fetchone()[0] == 0
    db.close()
