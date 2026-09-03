"""TDD contract tests for server-owned scenario calculations and observations."""
from copy import deepcopy
from datetime import timedelta

from fastapi.testclient import TestClient

from kr_stock_autotrader import db as dbmod
from kr_stock_autotrader.domain import parse_kst


def _lineage(db, symbol="123456"):
    db.execute("""INSERT INTO material_evidence(symbol,kind,title,summary,source,collected_at,known_at,snapshot,newness,dedupe_key,created_by,updated_at,audit_json)
                  VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""", (symbol, "disclosure", "test", "test", "test", "2026-08-31T08:00:00+09:00", "2026-08-31T08:00:00+09:00", "{}", "new", f"test-{symbol}", "test", "2026-08-31T08:00:00+09:00", "[]"))
    evidence_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
    db.execute("INSERT INTO deterministic_filter_results(evidence_id,raw_inputs,computed_outputs,as_of,known_at,verdict,reasons,created_at) VALUES(?,?,?,?,?,?,?,?)", (evidence_id, "{}", "{}", "2026-08-31", "2026-08-31T08:00:00+09:00", "PASS", "[]", "2026-08-31T08:00:00+09:00"))
    filter_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
    db.execute("""INSERT INTO decision_cards(lineage_key,version,evidence_id,filter_id,prompt_version,prompt_hash,model,provider,card_json,verdict,confidence,generated_at)
                  VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""", (f"lineage-{symbol}", 1, evidence_id, filter_id, "v1", "hash", "test", "test", "{}", "hold", .5, "2026-08-31T08:00:00+09:00"))
    card_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
    db.commit()
    return evidence_id, card_id


def payload(evidence_id=None, card_id=None):
    scenarios = []
    for label, probability, amount in (("BAD", .2, 1000), ("BASE", .5, 2000), ("GOOD", .3, 3000)):
        scenarios.append({"label": label, "probability": probability,
            "confirmed": {"amount_krw": amount, "duration_years": 1, "margin_pct": .2, "tax_rate": .2, "execution_probability": .8, "discount_rate": .1, "working_capital_krw": 10, "financing_cost_krw": 5, "follow_on": {"amount_krw": 0, "probability": 0, "margin_pct": 0, "years": 1}},
            "assumptions": ["synthetic"], "unknowns": ["volume"], "invalidations": ["contract_cancelled"]})
    return {"event_identity": "DART:2026-08-31:synthetic", "version": 1, "symbol": "123456", "event_type": "SUPPLY_CONTRACT", "profile": {"id": "SUPPLY_CONTRACT", "version": 1}, "disclosed_at": "2026-08-30T09:00:00+09:00", "known_at": "2026-08-31T09:00:00+09:00", "source_url": "https://example.test/disclosure", "source_receipt": "synthetic-receipt", "evidence_id": evidence_id, "card_id": card_id, "baseline": {"price_krw": 90, "price_known_at": "2026-08-31T08:59:00+09:00", "shares_outstanding": 100, "shares_known_at": "2026-08-31T08:59:00+09:00", "market_cap_krw": 9000, "market_cap_known_at": "2026-08-31T08:59:00+09:00", "financial_as_of": "2026-06-30", "denominator": "shares_outstanding"}, "scenarios": scenarios}


def _observation(created, key, price, *, volume=1.5, bench=0, sector=0, invalidation=False, seconds=1):
    known = (parse_kst(created["frozen_at"]) + timedelta(seconds=seconds)).isoformat()
    return {"provider": "TRUSTED_MARKET_CONTEXT", "source": "TRUSTED_MARKET_CONTEXT", "source_receipt": "MARKET_CONTEXT:test-receipt", "symbol": "123456", "idempotency_key": key, "known_at": known, "retrieved_at": known, "price_krw": price, "best_bid": price - .1, "best_ask": price + .1, "top_bid_qty": 18, "top_ask_qty": 12, "volume_ratio": volume, "benchmark_excess_pct": bench, "sector_excess_pct": sector, "market_context_status":"VERIFIED"}


def test_scenario_api_recalculates_lineage_and_has_no_order_side_effects(monkeypatch, tmp_path):
    monkeypatch.setattr(dbmod, "DATABASE_PATH", str(tmp_path / "scenario.db")); monkeypatch.setenv("INTERNAL_API_KEY", "scenario-key")
    from app import app
    client, headers = TestClient(app), {"X-Internal-API-Key": "scenario-key"}
    db = dbmod.connect(); evidence_id, card_id = _lineage(db); db.close()
    data = payload(evidence_id, card_id)
    created = client.post("/api/internal/scenario-sets", json=data, headers=headers)
    assert created.status_code == 200, created.text
    out = created.json()
    assert [x["label"] for x in out["scenarios"]] == ["BAD", "BASE", "GOOD"]
    assert out["scenarios"][0]["calculator"].startswith("SUPPLY_CONTRACT_")
    assert client.post("/api/internal/scenario-sets", json=data, headers=headers).status_code == 409
    successor = deepcopy(data); successor["version"] = 2
    assert client.post("/api/internal/scenario-sets", json=successor, headers=headers).status_code == 200
    db = dbmod.connect(); columns = {row["name"]: row for row in db.execute("PRAGMA table_info(event_scenario_sets)")}; db.close()
    assert columns["card_id"]["notnull"] == columns["evidence_id"]["notnull"] == 1
    forged = deepcopy(data); forged["event_identity"] = "forged"; forged["scenarios"][0]["eps_krw"] = 9
    assert client.post("/api/internal/scenario-sets", json=forged, headers=headers).status_code == 422
    forged = deepcopy(data); forged["event_identity"] = "mcap"; forged["baseline"]["market_cap_krw"] = 1
    assert client.post("/api/internal/scenario-sets", json=forged, headers=headers).status_code == 422
    forged = deepcopy(data); forged["event_identity"] = "version"; forged["version"] = "1"
    assert client.post("/api/internal/scenario-sets", json=forged, headers=headers).status_code == 422
    forged = deepcopy(data); forged["event_identity"] = "cost"; forged["scenarios"][0]["confirmed"]["working_capital_krw"] = -1
    assert client.post("/api/internal/scenario-sets", json=forged, headers=headers).status_code == 422
    forged = deepcopy(data); forged["event_identity"] = "unknown"; forged["scenarios"][0]["confirmed"]["attacker_primitive"] = {"saved": True}
    assert client.post("/api/internal/scenario-sets", json=forged, headers=headers).status_code == 422
    forged = deepcopy(data); forged["event_identity"] = "fractional"; forged["event_type"] = forged["profile"]["id"] = "CAPACITY_EXPANSION"
    for item in forged["scenarios"]:
        item["confirmed"] = {"units": 1, "utilization_pct": .5, "unit_price_krw": 100, "margin_pct": .5, "years": 1.5, "capex_krw": 0, "financing_cost_krw": 0, "tax_rate": .2, "discount_rate": .1}
    assert client.post("/api/internal/scenario-sets", json=forged, headers=headers).status_code == 422
    db = dbmod.connect()
    assert db.execute("SELECT count(*) FROM order_plans").fetchone()[0] == 0
    assert db.execute("SELECT count(*) FROM order_fills").fetchone()[0] == 0
    assert db.execute("SELECT count(*) FROM positions").fetchone()[0] == 0
    db.close()


def test_lineage_and_observation_invariants_and_all_actions(monkeypatch, tmp_path):
    monkeypatch.setattr(dbmod, "DATABASE_PATH", str(tmp_path / "observe.db")); monkeypatch.setenv("INTERNAL_API_KEY", "scenario-key")
    from app import app
    client, headers = TestClient(app), {"X-Internal-API-Key": "scenario-key"}
    db = dbmod.connect(); evidence_id, card_id = _lineage(db); other_evidence, other_card = _lineage(db, "654321"); db.close()
    assert client.post("/api/internal/scenario-sets", json=payload(), headers=headers).status_code == 422
    cross = payload(evidence_id, other_card); cross["event_identity"] = "cross"
    assert client.post("/api/internal/scenario-sets", json=cross, headers=headers).status_code == 422
    created = client.post("/api/internal/scenario-sets", json=payload(evidence_id, card_id), headers=headers).json()
    import kr_stock_autotrader.event_scenarios as scenarios_module
    monkeypatch.setattr(scenarios_module, "now_kst", lambda: parse_kst(created["frozen_at"]) + timedelta(minutes=1))
    ranges = {x["label"]: x["per_share_value_range_krw"] for x in created["scenarios"]}
    endpoint = f"/api/internal/scenario-sets/{created['event_identity']}/observations"
    cases = [("none", ranges["GOOD"]["high"] + 100, 0.5, 0, 0, False, "NO_ACTION"), ("bad", sum(ranges["BAD"].values()) / 2, 1.5, 0, 0, False, "REDUCE_REVIEW"), ("base", sum(ranges["BASE"].values()) / 2, 1.5, 0, 0, False, "WATCH"), ("entry", sum(ranges["GOOD"].values()) / 2, 1.5, 0, 0, False, "ENTRY_REVIEW"), ("add", sum(ranges["GOOD"].values()) / 2, 2.5, 6, 6, False, "ADD_REVIEW"), ("exit", 90, 1.5, 0, 0, True, "EXIT_REVIEW")]
    add_observation = None
    for sequence, (key, price, volume, bench, sector, invalidation, expected) in enumerate(cases, 1):
        if invalidation:
            db=dbmod.connect(); db.execute("UPDATE decision_cards SET invalidated_at=? WHERE id=?", (created["frozen_at"], card_id)); db.commit(); db.close()
        body = _observation(created, key, price, volume=volume, bench=bench, sector=sector, invalidation=invalidation, seconds=sequence)
        if key == "add": add_observation = body
        response = client.post(endpoint, json=body, headers=headers)
        assert response.status_code == 200, response.text
        assert response.json()["action"] == expected
    assert add_observation is not None
    assert client.post(endpoint, json=add_observation, headers=headers).json()["idempotent"] is True
    collision = deepcopy(add_observation); collision["price_krw"] += 1
    assert client.post(endpoint, json=collision, headers=headers).status_code == 409
    old = _observation(created, "old", 90, seconds=1)
    assert client.post(endpoint, json=old, headers=headers).status_code == 422
    bad_book = _observation(created, "book", 90, seconds=21); bad_book["best_bid"] = 101; bad_book["best_ask"] = 99
    assert client.post(endpoint, json=bad_book, headers=headers).status_code == 422
    evil = _observation(created, "evil", 90, seconds=22); evil["provider"] = "evil"
    assert client.post(endpoint, json=evil, headers=headers).status_code == 422
    supplied_spread = _observation(created, "spread", 90, seconds=23); supplied_spread["spread_pct"] = 0
    assert client.post(endpoint, json=supplied_spread, headers=headers).status_code == 422
