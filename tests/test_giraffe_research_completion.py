import hashlib
import json
import os
import tempfile
from datetime import datetime
from pathlib import Path

os.environ.setdefault("DATABASE_PATH", tempfile.mktemp(suffix=".db"))
os.environ.setdefault("INTERNAL_API_KEY", "test-key")
os.environ.setdefault("RESEARCH_CONTROL_KEY", "test-control-key")
os.environ.setdefault("SESSION_SECRET", "test-session-secret-that-is-at-least-thirty-two-bytes-long")
from fastapi.testclient import TestClient
from app import app
from kr_stock_autotrader import api as api_module

HEADERS = {"X-Internal-API-Key": os.environ["INTERNAL_API_KEY"]}
CONTROL_HEADERS = {"X-Research-Control-Key": os.environ["RESEARCH_CONTROL_KEY"]}


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def commitment(run_key, receipts, source_root=None, source_control_dates=None):
    from kr_stock_autotrader.krx_calendar import admitted_backlog_dates

    run_date = datetime.strptime(run_key.removeprefix("research-").split("-0700-kst", 1)[0], "%Y-%m-%d")
    dates = admitted_backlog_dates(run_date.date())
    source_root = source_root or Path.home() / ".hermes" / "runs" / "giraffe-7923" / "dart-source-packets"
    source_control_dates = source_control_dates or {}
    sources = [{
        "rcp_no": rcp_no,
        "date": source_control_dates.get(rcp_no, rcp_no[:8]),
        "receipt_source_date": rcp_no[:8],
        "packet_path": str(source_root / source_control_dates.get(rcp_no, rcp_no[:8]) / f"{rcp_no}.json"),
        "packet_sha256": "a" * 64,
    } for rcp_no in sorted(receipts)]
    contract = {
        "schema_version": "giraffe-research-control-v1",
        "run_key": run_key,
        "dates": dates,
        "source_valid": True,
        "expected_rcp_nos": sorted(receipts),
        "control_count": len(receipts),
        "sources": sources,
    }
    return contract, hashlib.sha256(canonical(contract)).hexdigest()


def register(client, key, receipts=(), headers=CONTROL_HEADERS, **commitment_kwargs):
    contract, digest = commitment(key, receipts, **commitment_kwargs)
    response = client.post(f"/api/internal/research-runs/{key}/register", json={"control_contract": contract}, headers=headers)
    assert response.status_code == 200
    return contract, digest


def start(client, key, receipts=(), **commitment_kwargs):
    contract, digest = register(client, key, receipts, **commitment_kwargs)
    response = client.post(f"/api/internal/scheduler-runs/{key}/start", json={"kind": "research"}, headers=HEADERS)
    assert response.status_code == 200 and response.json()["idempotent"] is True
    return contract, digest


def coverage_lane(name, *, outcome="not_material", source_valid=True, published_at="2026-09-15T06:00:00+09:00", retrieved_at="2026-09-16T07:00:00+09:00"):
    return {
        "executed": True,
        "query_count": 1,
        "checked_url_count": 1,
        "source_valid_count": int(source_valid),
        "candidate_count": int(outcome == "candidate"),
        "coverage_error_count": 0,
        "queries": [f"{name} material disclosure 2026-09-15"],
        "checked_sources": [{
            "url": f"https://example.com/{name}",
            "source_valid": source_valid,
            "published_at": published_at if source_valid else None,
            "retrieved_at": retrieved_at,
            "outcome": outcome,
            "economic_disposition": None,
            "economic_reason": None,
            "evidence_id": None,
        }],
    }


def receipt(run_key, digest, receipts, **extra):
    control = len(receipts)
    value = {
        "schema_version": "giraffe-research-completion-v1", "run_key": run_key,
        "control_contract_sha256": digest, "control_count": control,
        "source_valid": control, "reviewed_unique": control,
        "reviewed_rcp_nos": sorted(receipts), "source_error": 0, "store_error": 0,
        "coverage_error": 0, "rejected_after_evidence": control, "saved": 0,
        "existing": 0, "correction_stored": 0,
        "coverage_lanes": {name: coverage_lane(name, published_at=f"{run_key[9:19]}T06:00:00+09:00", retrieved_at=f"{run_key[9:19]}T07:00:00+09:00") for name in ("kind_krx", "issuer_ir_newsroom", "reputable_media")},
    }
    value.update(extra)
    return value


def test_versioned_research_run_registers_starts_and_finishes_but_arbitrary_suffix_rejects():
    client = TestClient(app); key = "research-2026-09-16-0700-kst-r1"
    contract, digest = start(client, key, ["20260916000001"])
    assert contract["run_key"] == key
    done = {"status": "done", "count": 0, "detail": {"completion_receipt": receipt(key, digest, ["20260916000001"])}}
    assert client.post(f"/api/internal/scheduler-runs/{key}/finish", json=done, headers=HEADERS).status_code == 200
    bad = "research-2026-09-16-0700-kst-correction"
    contract, _ = commitment(bad, [])
    assert client.post(f"/api/internal/research-runs/{bad}/register", json={"control_contract": contract}, headers=CONTROL_HEADERS).status_code == 422


def test_research_done_requires_exact_coverage_lanes():
    client = TestClient(app); key = "research-2026-09-16-0700-kst"
    _, digest = start(client, key, [])
    cases = [
        {},
        {"kind_krx": {}},
        {"kind_krx": {"executed": False, "query_count": 1, "checked_url_count": 0, "source_valid_count": 0, "candidate_count": 0, "coverage_error_count": 0},
         "issuer_ir_newsroom": {"executed": True, "query_count": 1, "checked_url_count": 0, "source_valid_count": 0, "candidate_count": 0, "coverage_error_count": 0},
         "reputable_media": {"executed": True, "query_count": 1, "checked_url_count": 0, "source_valid_count": 0, "candidate_count": 0, "coverage_error_count": 0}},
    ]
    valid = receipt(key, digest, [])
    for lanes in cases:
        invalid = receipt(key, digest, [], coverage_lanes=lanes)
        payload = {"status": "done", "count": 0, "detail": {"completion_receipt": invalid}}
        assert client.post(f"/api/internal/scheduler-runs/{key}/finish", json=payload, headers=HEADERS).status_code == 422
    for field, value in (("query_count", 0), ("coverage_error_count", 1)):
        invalid = receipt(key, digest, [])
        invalid["coverage_lanes"]["kind_krx"][field] = value
        payload = {"status": "done", "count": 0, "detail": {"completion_receipt": invalid}}
        assert client.post(f"/api/internal/scheduler-runs/{key}/finish", json=payload, headers=HEADERS).status_code == 422
    # A query alone is not coverage: every independent lane must check and
    # validate an original source before a no-candidate DONE is safe.
    for lane_name in ("kind_krx", "issuer_ir_newsroom", "reputable_media"):
        for field, value in (("checked_url_count", 0), ("source_valid_count", 0)):
            invalid = receipt(key, digest, [])
            invalid["coverage_lanes"][lane_name][field] = value
            payload = {"status": "done", "count": 0, "detail": {"completion_receipt": invalid}}
            assert client.post(f"/api/internal/scheduler-runs/{key}/finish", json=payload, headers=HEADERS).status_code == 422
    invalid = receipt(key, digest, [])
    invalid["coverage_lanes"]["issuer_ir_newsroom"].update({"checked_url_count": 1, "source_valid_count": 2})
    payload = {"status": "done", "count": 0, "detail": {"completion_receipt": invalid}}
    assert client.post(f"/api/internal/scheduler-runs/{key}/finish", json=payload, headers=HEADERS).status_code == 422
    payload = {"status": "done", "count": 0, "detail": {"completion_receipt": valid}}
    assert client.post(f"/api/internal/scheduler-runs/{key}/finish", json=payload, headers=HEADERS).status_code == 200


def test_research_done_requires_durable_inline_query_source_audit_and_readback():
    client = TestClient(app); key = "research-2026-09-18-0700-kst"
    _, digest = start(client, key, [])

    counts_only = receipt(key, digest, [])
    for lane in counts_only["coverage_lanes"].values():
        lane.pop("queries"); lane.pop("checked_sources")
    assert client.post(f"/api/internal/scheduler-runs/{key}/finish", json={"status": "done", "count": 0, "detail": {"completion_receipt": counts_only}}, headers=HEADERS).status_code == 422

    variants = []
    def invalid(mutator):
        value = receipt(key, digest, [])
        mutator(value["coverage_lanes"]["kind_krx"])
        variants.append(value)

    invalid(lambda lane: lane.update({"unexpected": True}))
    invalid(lambda lane: lane.pop("queries"))
    invalid(lambda lane: lane.update({"query_count": 2}))
    invalid(lambda lane: lane.update({"queries": [""]}))
    invalid(lambda lane: lane.update({"queries": ["x" * 501]}))
    invalid(lambda lane: lane.update({"queries": [f"query {n}" for n in range(101)], "query_count": 101}))
    invalid(lambda lane: lane.update({"queries": [lane for lane in ["same", "same"]], "query_count": 2}))
    invalid(lambda lane: lane["checked_sources"][0].update({"url": "http://example.com/no"}))
    invalid(lambda lane: lane["checked_sources"][0].update({"url": "/relative"}))
    invalid(lambda lane: lane["checked_sources"][0].update({"url": "https://user:pass@example.com/no"}))
    invalid(lambda lane: lane["checked_sources"][0].update({"url": "https://example.com/no#fragment"}))
    invalid(lambda lane: lane.update({"checked_sources": [lane["checked_sources"][0], dict(lane["checked_sources"][0])], "checked_url_count": 2, "source_valid_count": 2}))
    invalid(lambda lane: lane.update({"checked_sources": [{**lane["checked_sources"][0], "url": f"https://example.com/{n}"} for n in range(101)], "checked_url_count": 101, "source_valid_count": 101}))
    invalid(lambda lane: lane["checked_sources"][0].update({"source_valid": "true"}))
    invalid(lambda lane: lane["checked_sources"][0].update({"published_at": "2026-09-15T06:00:00"}))
    invalid(lambda lane: lane["checked_sources"][0].update({"retrieved_at": "not-a-timestamp"}))
    invalid(lambda lane: lane["checked_sources"][0].update({"retrieved_at": "2026-09-16T07:00:00"}))
    invalid(lambda lane: lane["checked_sources"][0].update({"outcome": "made_up"}))
    invalid(lambda lane: lane["checked_sources"][0].update({"outcome": "invalid_source"}))
    invalid(lambda lane: lane.update({"candidate_count": 1}))
    invalid(lambda lane: lane["checked_sources"][0].update({"evidence_id": 1}))
    invalid(lambda lane: lane["checked_sources"][0].update({"extra": "no"}))

    for value in variants:
        payload = {"status": "done", "count": 0, "detail": {"completion_receipt": value}}
        assert client.post(f"/api/internal/scheduler-runs/{key}/finish", json=payload, headers=HEADERS).status_code == 422

    valid = receipt(key, digest, [])
    response = client.post(f"/api/internal/scheduler-runs/{key}/finish", json={"status": "done", "count": 0, "detail": {"completion_receipt": valid}}, headers=HEADERS)
    assert response.status_code == 200
    latest = client.get("/api/internal/scheduler-runs/latest?kind=research&date=2026-09-18", headers=HEADERS)
    assert latest.status_code == 200
    assert latest.json()["detail"]["detail"]["completion_receipt"]["coverage_lanes"] == valid["coverage_lanes"]


def test_coverage_cutoff_canonical_urls_and_candidate_economics_are_enforced():
    client = TestClient(app); key = "research-2026-09-16-0700-kst"
    _, digest = start(client, key, [])

    def finish(value):
        return client.post(f"/api/internal/scheduler-runs/{key}/finish", json={"status": "done", "count": 0, "detail": {"completion_receipt": value}}, headers=HEADERS)

    # Candidate-side evidence is admitted at the 07:00 KST instant, including
    # its UTC equivalent, but timing-ineligible must be strictly later.
    exact = receipt(key, digest, [])
    source = exact["coverage_lanes"]["kind_krx"]["checked_sources"][0]
    source.update({"outcome": "candidate", "published_at": "2026-09-15T22:00:00Z", "economic_disposition": "eligible", "economic_reason": "published before this run cutoff and satisfies the stated economics"})
    exact["coverage_lanes"]["kind_krx"]["candidate_count"] = 1
    assert finish(exact).status_code == 422
    no_economics = receipt(key, digest, [])
    no_economics["coverage_lanes"]["kind_krx"]["checked_sources"][0]["outcome"] = "candidate"
    no_economics["coverage_lanes"]["kind_krx"]["candidate_count"] = 1
    assert finish(no_economics).status_code == 422
    retrieved_before_publication = receipt(key, digest, [])
    retrieved_before_publication["coverage_lanes"]["kind_krx"]["checked_sources"][0].update({"published_at": "2026-09-16T06:30:00+09:00", "retrieved_at": "2026-09-16T06:00:00+09:00"})
    assert finish(retrieved_before_publication).status_code == 422

    for outcome, published_at in (("candidate", "2026-09-16T07:01:00+09:00"), ("negative_evidence", "2026-09-16T07:01:00+09:00"), ("not_material", "2026-09-16T07:01:00+09:00"), ("timing_ineligible", "2026-09-16T06:59:00+09:00")):
        value = receipt(key, digest, [])
        source = value["coverage_lanes"]["kind_krx"]["checked_sources"][0]
        source.update({"outcome": outcome, "published_at": published_at})
        if outcome == "candidate":
            source.update({"economic_disposition": "hold", "economic_reason": "needs evidence"}); value["coverage_lanes"]["kind_krx"]["candidate_count"] = 1
        assert finish(value).status_code == 422

    for duplicate in ("https://EXAMPLE.com:443/kind_krx", "https://example.com./kind_krx", "https://example.com/%6b%69nd_krx"):
        value = receipt(key, digest, [])
        lane = value["coverage_lanes"]["kind_krx"]
        lane["checked_sources"].append({**lane["checked_sources"][0], "url": duplicate})
        lane.update({"checked_url_count": 2, "source_valid_count": 2})
        assert finish(value).status_code == 422
    value = receipt(key, digest, [])
    lane = value["coverage_lanes"]["kind_krx"]
    lane["checked_sources"][0]["url"] = "https://example.com/"
    lane["checked_sources"].append({**lane["checked_sources"][0], "url": "https://example.com"})
    lane.update({"checked_url_count": 2, "source_valid_count": 2})
    assert finish(value).status_code == 422

    for invalid_url in ("https://example .com/no", "https://example.com/a\tb", "https://example.com/a\nb", "https://example.com/a\x01b", "https://example.com/%zz"):
        value = receipt(key, digest, [])
        value["coverage_lanes"]["kind_krx"]["checked_sources"][0]["url"] = invalid_url
        assert finish(value).status_code == 422

    distinct = receipt(key, digest, [])
    lane = distinct["coverage_lanes"]["kind_krx"]
    lane["checked_sources"].append({**lane["checked_sources"][0], "url": "https://example.com/kind_krx?version=2"})
    lane.update({"checked_url_count": 2, "source_valid_count": 2})
    assert finish(distinct).status_code == 200


def test_coverage_candidate_terminal_evidence_binding_and_cross_lane_dedupe():
    client = TestClient(app); key = "research-2026-09-16-0700-kst-r1"
    _, digest = start(client, key, ["20260916000001"])

    def finish(value):
        return client.post(f"/api/internal/scheduler-runs/{key}/finish", json={"status": "done", "count": value["saved"] + value["correction_stored"], "detail": {"completion_receipt": value}}, headers=HEADERS)

    def candidate(value, disposition, evidence_id=None):
        source = value["coverage_lanes"]["kind_krx"]["checked_sources"][0]
        source.update({"outcome": "candidate", "economic_disposition": disposition, "economic_reason": "terminal economic review", "evidence_id": evidence_id})
        value["coverage_lanes"]["kind_krx"]["candidate_count"] = 1
        return source

    # Canonical uniqueness is global across independent lanes, not merely local.
    duplicate = receipt(key, digest, ["20260916000001"])
    duplicate["coverage_lanes"]["issuer_ir_newsroom"]["checked_sources"][0]["url"] = "https://EXAMPLE.com:443/kind_krx"
    assert finish(duplicate).status_code == 422
    independent = receipt(key, digest, ["20260916000001"])
    independent["coverage_lanes"]["issuer_ir_newsroom"]["checked_sources"][0]["url"] = "https://example.com/other-original"

    prior_eligible = receipt(key, digest, ["20260916000001"])
    candidate(prior_eligible, "eligible")
    assert finish(prior_eligible).status_code == 422
    zero_saved = receipt(key, digest, ["20260916000001"])
    candidate(zero_saved, "saved", 1)
    assert finish(zero_saved).status_code == 422
    fake = receipt(key, digest, ["20260916000001"], rejected_after_evidence=0, saved=1)
    candidate(fake, "saved", 999999)
    assert finish(fake).status_code == 422

    evidence = client.post("/api/internal/evidence", headers=HEADERS, json={
        "symbol": "005930", "kind": "news", "title": "candidate evidence", "summary": "material",
        "source": "issuer", "source_url": "https://example.com/kind_krx",
        "announcement_at": "2026-09-16T06:00:00+09:00", "collected_at": "2026-09-16T07:00:00+09:00",
        "known_at": "2026-09-16T06:00:00+09:00", "research_mode": "scheduled_as_of", "research_run_key": key,
        "snapshot": {}, "dedupe_key": "candidate-terminal-binding",
    })
    assert evidence.status_code == 200
    saved = receipt(key, digest, ["20260916000001"], rejected_after_evidence=0, saved=1)
    candidate(saved, "saved", evidence.json()["id"])
    response = finish(saved)
    assert response.status_code == 200
    assert client.get(f"/api/internal/scheduler-runs/{key}", headers=CONTROL_HEADERS).json()["detail"]["detail"]["completion_receipt"] == saved
    assert finish(independent).status_code == 200

    for disposition, field in (("existing", "existing"), ("correction_stored", "correction_stored")):
        mismatch = receipt(key, digest, ["20260916000001"], rejected_after_evidence=0, **{field: 1})
        candidate(mismatch, disposition, evidence.json()["id"])
        mismatch[field] = 0
        mismatch["rejected_after_evidence"] = 1
        assert finish(mismatch).status_code == 422

    conflicting = receipt(key, digest, ["20260916000001"], rejected_after_evidence=0, saved=1, existing=1)
    candidate(conflicting, "saved", evidence.json()["id"])
    other = conflicting["coverage_lanes"]["issuer_ir_newsroom"]["checked_sources"][0]
    other.update({"outcome": "candidate", "economic_disposition": "existing", "economic_reason": "terminal economic review", "evidence_id": evidence.json()["id"]})
    conflicting["coverage_lanes"]["issuer_ir_newsroom"]["candidate_count"] = 1
    assert finish(conflicting).status_code == 422

    for disposition in ("rejected", "hold"):
        no_evidence = receipt(key, digest, ["20260916000001"])
        candidate(no_evidence, disposition)
        assert finish(no_evidence).status_code == 200
    rejected_with_evidence = receipt(key, digest, ["20260916000001"])
    candidate(rejected_with_evidence, "rejected", evidence.json()["id"])
    assert finish(rejected_with_evidence).status_code == 422


def test_candidate_evidence_known_at_is_cutoff_bound_and_chronological():
    """A stored candidate cannot use knowledge unavailable at the 07:00 run."""
    client = TestClient(app)
    evidence = client.post("/api/internal/evidence", headers=HEADERS, json={
        "symbol": "005930", "kind": "news", "title": "known-at cutoff evidence", "summary": "material",
        "source": "issuer", "source_url": "https://example.com/known-at-cutoff",
        "announcement_at": "2026-09-16T06:00:00+09:00", "collected_at": "2026-09-16T07:00:00+09:00",
        "known_at": "2026-09-16T06:00:00+09:00", "research_mode": "scheduled_as_of", "research_run_key": "research-2026-09-16-0700-kst-r2", "snapshot": {}, "dedupe_key": "candidate-known-at-cutoff",
    })
    assert evidence.status_code == 200

    def finish(version, known_at):
        key = f"research-2026-09-16-0700-kst-r{version}"
        _, digest = start(client, key, [f"202609160000{version:02d}"])
        db = api_module.connect()
        try:
            db.execute("UPDATE material_evidence SET known_at=? WHERE id=?", (known_at, evidence.json()["id"]))
            db.commit()
        finally:
            db.close()
        value = receipt(key, digest, [f"202609160000{version:02d}"], rejected_after_evidence=0, saved=1)
        source = value["coverage_lanes"]["kind_krx"]["checked_sources"][0]
        source.update({"url": "https://example.com/known-at-cutoff", "outcome": "candidate", "economic_disposition": "saved", "economic_reason": "durable candidate evidence", "evidence_id": evidence.json()["id"]})
        value["coverage_lanes"]["kind_krx"]["candidate_count"] = 1
        return client.post(f"/api/internal/scheduler-runs/{key}/finish", json={"status": "done", "count": 1, "detail": {"completion_receipt": value}}, headers=HEADERS)

    assert finish(2, "2026-09-16T07:00:00+09:00").status_code == 200
    # An evidence row is sealed to its exact rerun, not merely the calendar day.
    for version, known_at in ((3, "2026-09-15T22:00:00Z"), (4, "2026-09-16T07:00:01+09:00"), (5, "2026-09-16T07:00:00"), (6, "not-a-timestamp"), (7, "2026-09-16T05:59:59+09:00")):
        assert finish(version, known_at).status_code == 422


def test_research_run_exact_readback_is_versioned_and_control_key_gated():
    client = TestClient(app)
    keys = ("research-2026-09-16-0700-kst", "research-2026-09-16-0700-kst-r1", "research-2026-09-16-0700-kst-r12")
    for key in keys:
        _, digest = start(client, key, [])
        assert client.post(f"/api/internal/scheduler-runs/{key}/finish", json={"status": "done", "count": 0, "detail": {"completion_receipt": receipt(key, digest, [])}}, headers=HEADERS).status_code == 200
        response = client.get(f"/api/internal/scheduler-runs/{key}", headers=CONTROL_HEADERS)
        assert response.status_code == 200 and response.json()["run_key"] == key
    assert client.get("/api/internal/scheduler-runs/research-2026-09-16-0700-kst-r2", headers=CONTROL_HEADERS).status_code == 404
    assert client.get("/api/internal/scheduler-runs/research-nope", headers=CONTROL_HEADERS).status_code == 422
    assert client.get(f"/api/internal/scheduler-runs/{keys[0]}", headers=HEADERS).status_code == 403


def test_exact_observed_forged_done_control_exploit_is_rejected_and_keeps_started():
    client = TestClient(app); key = "research-2026-09-11-0700-kst"
    _, digest = start(client, key, ["20260911000001"])
    forged = {"status": "done", "count": 0, "detail": {"completion_receipt": receipt(key, digest, [])}}
    assert client.post(f"/api/internal/scheduler-runs/{key}/finish", json=forged, headers=HEADERS).status_code == 422
    latest = client.get("/api/internal/scheduler-runs/latest?kind=research&date=2026-09-11", headers=HEADERS)
    assert latest.json()["status"] == "started"
    assert latest.json()["detail"]["control_commitment"]["control_contract"]["expected_rcp_nos"] == ["20260911000001"]


def test_research_done_binds_exact_control_ids_contract_and_terminal_idempotence():
    client = TestClient(app); key = "research-2026-09-14-0700-kst"
    contract, digest = start(client, key, ["20260914000001", "20260914000002"])
    for bad_receipts in (["20260914000001"], ["20260914000001", "20260914000001"], ["20260914000001", "20260914999999"]):
        payload = {"status": "done", "count": 0, "detail": {"completion_receipt": receipt(key, digest, bad_receipts)}}
        assert client.post(f"/api/internal/scheduler-runs/{key}/finish", json=payload, headers=HEADERS).status_code == 422
    bad_run = {"status": "done", "count": 0, "detail": {"completion_receipt": receipt("research-2026-09-12-0700-kst", digest, contract["expected_rcp_nos"])}}
    assert client.post(f"/api/internal/scheduler-runs/{key}/finish", json=bad_run, headers=HEADERS).status_code == 422
    bad_hash = {"status": "done", "count": 0, "detail": {"completion_receipt": receipt(key, "b" * 64, contract["expected_rcp_nos"])}}
    assert client.post(f"/api/internal/scheduler-runs/{key}/finish", json=bad_hash, headers=HEADERS).status_code == 422
    payload = {"status": "done", "count": 0, "detail": {"completion_receipt": receipt(key, digest, contract["expected_rcp_nos"])}}
    first = client.post(f"/api/internal/scheduler-runs/{key}/finish", json=payload, headers=HEADERS)
    assert first.status_code == 200 and first.json()["status"] == "done"
    second = client.post(f"/api/internal/scheduler-runs/{key}/finish", json={"status": "error", "count": 0, "detail": {}}, headers=HEADERS)
    assert second.json()["status"] == "done"


def test_internal_scheduler_cannot_create_or_forge_canonical_research_runs():
    client = TestClient(app); key = "research-2026-09-15-0700-kst"
    assert client.post(f"/api/internal/scheduler-runs/{key}/start", json={"kind": "research"}, headers=HEADERS).status_code == 404
    contract, _ = commitment(key, ["20260915000001"])
    assert contract["dates"] == ["20260914", "20260915"]
    assert client.post(f"/api/internal/scheduler-runs/{key}/start", json={"kind": "research", "control_contract": contract}, headers=HEADERS).status_code == 422
    assert client.post(f"/api/internal/research-runs/{key}/register", json={"control_contract": contract}, headers=HEADERS).status_code == 403
    assert client.post(f"/api/internal/research-runs/{key}/register", json={"control_contract": contract}, headers={"X-Research-Control-Key": "wrong"}).status_code == 403
    _, digest = register(client, key, ["20260915000001"])
    conflict, _ = commitment(key, ["20260915000002"])
    assert client.post(f"/api/internal/research-runs/{key}/register", json={"control_contract": conflict}, headers=CONTROL_HEADERS).status_code == 409
    valid = {"status": "done", "count": 0, "detail": {"completion_receipt": receipt(key, digest, ["20260915000001"])}}
    assert client.post(f"/api/internal/scheduler-runs/{key}/finish", json=valid, headers=HEADERS).status_code == 200


def test_registration_does_not_depend_on_caller_contract_path():
    client = TestClient(app); key = "research-2026-09-16-0700-kst"
    contract, _ = commitment(key, [])
    assert client.post(f"/api/internal/research-runs/{key}/register", json={"control_contract": contract}, headers=CONTROL_HEADERS).status_code == 200


def test_registration_uses_explicit_packet_root_when_container_home_differs(monkeypatch):
    client = TestClient(app); key = "research-2026-09-17-0700-kst"
    host_packet_root = Path("/Users/jaewoo/.hermes/runs/giraffe-7923/dart-source-packets")
    contract, _ = commitment(key, ["20260917000001"], source_root=host_packet_root)
    monkeypatch.setenv("GIRAFFE_RESEARCH_PACKET_ROOT", str(host_packet_root))
    monkeypatch.setattr(api_module.Path, "home", classmethod(lambda cls: (_ for _ in ()).throw(AssertionError("configured root must not use HOME"))))
    response = client.post(
        f"/api/internal/research-runs/{key}/register",
        json={"control_contract": contract},
        headers=CONTROL_HEADERS,
    )
    assert response.status_code == 200


def test_correction_contract_registers_and_completes_with_control_date_packet_path():
    client = TestClient(app); key = "research-2026-09-15-0700-kst"
    receipt_id = "20260914000432"
    contract, digest = start(client, key, [receipt_id], source_control_dates={receipt_id: "20260915"})
    assert contract["sources"] == [{
        "rcp_no": receipt_id, "date": "20260915", "receipt_source_date": "20260914",
        "packet_path": str(Path.home() / ".hermes" / "runs" / "giraffe-7923" / "dart-source-packets" / "20260915" / f"{receipt_id}.json"),
        "packet_sha256": "a" * 64,
    }]
    done = {"status": "done", "count": 0, "detail": {"completion_receipt": receipt(key, digest, [receipt_id])}}
    assert client.post(f"/api/internal/scheduler-runs/{key}/finish", json=done, headers=HEADERS).status_code == 200


def test_registration_rejects_legacy_or_forged_correction_source_schema():
    client = TestClient(app); key = "research-2026-09-15-0700-kst"; receipt_id = "20260914000432"
    contract, _ = commitment(key, [receipt_id], source_control_dates={receipt_id: "20260915"})
    variants = [
        dict(contract, sources=[{key: value for key, value in contract["sources"][0].items() if key != "receipt_source_date"}]),
        dict(contract, sources=[dict(contract["sources"][0], receipt_source_date="20260915")]),
        dict(contract, sources=[dict(contract["sources"][0], date="20260913", packet_path=str(Path.home() / ".hermes" / "runs" / "giraffe-7923" / "dart-source-packets" / "20260913" / f"{receipt_id}.json"))]),
        dict(contract, sources=[dict(contract["sources"][0], packet_path="/untrusted/20260915/20260914000432.json")]),
    ]
    for bad in variants:
        assert client.post(f"/api/internal/research-runs/{key}/register", json={"control_contract": bad}, headers=CONTROL_HEADERS).status_code == 422


def test_registration_rejects_contract_date_shrink_expand_reorder_and_cross_date_receipts():
    client = TestClient(app)
    key = "research-2026-09-14-0700-kst"  # Monday: Sat/Sun/Mon backlog.
    contract, _ = commitment(key, ["20260912000001", "20260913000001", "20260914000001"])
    variants = []
    variants.append(dict(contract, dates=contract["dates"][1:]))
    variants.append(dict(contract, dates=["20260911", *contract["dates"]]))
    variants.append(dict(contract, dates=list(reversed(contract["dates"]))))
    variants.append(dict(contract, sources=[dict(contract["sources"][0], date="20260911"), *contract["sources"][1:]]))
    for bad in variants:
        response = client.post(f"/api/internal/research-runs/{key}/register", json={"control_contract": bad}, headers=CONTROL_HEADERS)
        assert response.status_code == 422


def test_v2_backlog_unions_prior_error_dedupes_and_only_terminal_readback_clears():
    client = TestClient(app)
    old = "research-2026-09-15-0700-kst-r1"
    old_contract, _ = start(client, old, ["20260915000271"])
    assert client.post(f"/api/internal/scheduler-runs/{old}/finish", json={"status": "error", "count": 0, "detail": {}}, headers=HEADERS).status_code == 200
    pending = client.get("/api/internal/research-backlog", headers=CONTROL_HEADERS)
    assert pending.status_code == 200
    item = pending.json()["items"][0]
    assert item["identity"] == "dart:20260915000271" and item["first_run_key"] == old

    key = "research-2026-09-16-0700-kst"
    contract, digest = commitment(key, ["20260915000271"])
    contract.update({"schema_version": "giraffe-research-control-v2", "carry_forward": [{"identity": item["identity"], "kind": "dart", "payload": item["payload"]}]})
    assert client.post(f"/api/internal/research-runs/{key}/register", json={"control_contract": contract}, headers=CONTROL_HEADERS).status_code == 200
    assert client.post(f"/api/internal/scheduler-runs/{key}/start", json={"kind": "research"}, headers=HEADERS).status_code == 200
    digest = hashlib.sha256(canonical(contract)).hexdigest()
    value = receipt(key, digest, ["20260915000271"])
    done = {"status": "done", "count": 0, "detail": {"completion_receipt": value,
        "control_terminal_dispositions": [{"rcp_no": "20260915000271", "disposition": "hold", "evidence_id": None}]}}
    assert client.post(f"/api/internal/scheduler-runs/{key}/finish", json=done, headers=HEADERS).status_code == 200
    assert client.get("/api/internal/research-backlog", headers=CONTROL_HEADERS).json()["items"] == []


def test_error_discovery_candidate_is_durable_with_original_time_and_deduped():
    client = TestClient(app); key = "research-2026-09-16-0700-kst-r2"
    start(client, key, [])
    candidate = {"source_url": "https://issuer.example.com/doosan", "announcement_at": "2026-09-15T10:43:20+09:00", "payload": {"company": "두산퓨얼셀"}}
    payload = {"status": "error", "count": 0, "detail": {"carry_forward_candidates": [candidate, candidate]}}
    assert client.post(f"/api/internal/scheduler-runs/{key}/finish", json=payload, headers=HEADERS).status_code == 200
    items = client.get("/api/internal/research-backlog", headers=CONTROL_HEADERS).json()["items"]
    found = [item for item in items if item["kind"] == "discovery"]
    assert len(found) == 1 and found[0]["original_announcement_at"] == candidate["announcement_at"]


def test_manual_catch_up_preserves_delayed_truth_and_rejects_bad_chronology():
    client = TestClient(app)
    base = {"symbol":"336260", "kind":"news", "title":"두산퓨얼셀 계약", "summary":"3222억원", "source":"KIND",
            "source_url":"https://issuer.example.com/doosan", "announcement_at":"2026-09-15T10:43:20+09:00",
            "known_at":"2026-09-16T12:00:00+09:00", "collected_at":"2026-09-16T12:01:00+09:00", "snapshot":{},
            "dedupe_key":"doosan-manual-catch-up", "research_mode":"manual_catch_up", "research_run_key":"research-2026-09-16-0700-kst"}
    created = client.post("/api/internal/evidence", headers=HEADERS, json=base)
    assert created.status_code == 200
    detail = client.get(f"/api/internal/evidence/{created.json()['id']}", headers=HEADERS).json()
    assert detail["announcement_at"] == base["announcement_at"] and detail["known_at"] == base["known_at"]
    assert detail["research_mode"] == "manual_catch_up" and detail["eligible_for_original_cutoff"] == 0
    assert client.post("/api/internal/evidence", headers=HEADERS, json=base).status_code == 409
    scheduled = dict(base, dedupe_key="doosan-scheduled-late", research_mode="scheduled_as_of")
    assert client.post("/api/internal/evidence", headers=HEADERS, json=scheduled).status_code == 422
    stale_announcement = dict(base, dedupe_key="scheduled-stale-announcement", research_mode="scheduled_as_of",
                              announcement_at="2020-01-01T06:00:00+09:00", known_at="2026-09-16T06:59:00+09:00",
                              collected_at="2026-09-16T06:59:00+09:00")
    assert client.post("/api/internal/evidence", headers=HEADERS, json=stale_announcement).status_code == 422
    for field, value in (("known_at", "2026-09-15T10:00:00+09:00"), ("known_at", "2026-09-16T12:02:00+09:00"), ("known_at", "2026-09-16T12:00:00")):
        bad = dict(base, dedupe_key="bad-" + field + value, **{field: value})
        assert client.post("/api/internal/evidence", headers=HEADERS, json=bad).status_code == 422
    default_scheduled = dict(base, dedupe_key="default-scheduled-cannot-bypass")
    default_scheduled.pop("research_mode")
    default_scheduled.pop("research_run_key")
    # Dated generic callers remain accepted but cannot finish a research run.
    assert client.post("/api/internal/evidence", headers=HEADERS, json=default_scheduled).status_code == 200
    generic = dict(default_scheduled, dedupe_key="generic-no-research-provenance")
    generic.pop("announcement_at")
    assert client.post("/api/internal/evidence", headers=HEADERS, json=generic).status_code == 200


def test_v2_terminal_evidence_requires_exact_dart_provenance_before_clearing_cursor():
    client = TestClient(app); old = "research-2026-09-15-0700-kst-r8"; receipt_id = "20260915000888"
    start(client, old, [receipt_id])
    assert client.post(f"/api/internal/scheduler-runs/{old}/finish", json={"status": "error", "count": 0, "detail": {}}, headers=HEADERS).status_code == 200
    pending = client.get("/api/internal/research-backlog", headers=CONTROL_HEADERS).json()["items"]
    carried = next(item for item in pending if item["identity"] == "dart:" + receipt_id)
    key = "research-2026-09-16-0700-kst-r8"; contract, _ = commitment(key, [receipt_id])
    contract.update({"schema_version": "giraffe-research-control-v2", "carry_forward": [{"identity": carried["identity"], "kind": "dart", "payload": carried["payload"]}]})
    assert client.post(f"/api/internal/research-runs/{key}/register", json={"control_contract": contract}, headers=CONTROL_HEADERS).status_code == 200
    digest = hashlib.sha256(canonical(contract)).hexdigest()
    def finish(evidence_id):
        value = receipt(key, digest, [receipt_id], rejected_after_evidence=0, saved=1)
        candidate = value["coverage_lanes"]["kind_krx"]["checked_sources"][0]
        candidate.update({"url": "https://dart.fss.or.kr", "outcome": "candidate", "economic_disposition": "saved", "economic_reason": "DART source stored", "evidence_id": evidence_id})
        value["coverage_lanes"]["kind_krx"]["candidate_count"] = 1
        return client.post(f"/api/internal/scheduler-runs/{key}/finish", headers=HEADERS, json={"status": "done", "count": 1, "detail": {"completion_receipt": value, "control_terminal_dispositions": [{"rcp_no": receipt_id, "disposition": "saved", "evidence_id": evidence_id}]}})
    evidence_data = {"symbol":"005930", "kind":"news", "title":"DART", "summary":"x", "source":"dart", "source_url":"https://dart.fss.or.kr", "announcement_at":"2026-09-16T06:00:00+09:00", "known_at":"2026-09-16T06:00:00+09:00", "snapshot":{"rcp_no": receipt_id}, "research_mode":"scheduled_as_of", "research_run_key":key, "dedupe_key":"wrong-v2-provenance"}
    wrong = client.post("/api/internal/evidence", headers=HEADERS, json=evidence_data).json()["id"]
    assert finish(wrong).status_code == 422
    assert any(item["identity"] == carried["identity"] for item in client.get("/api/internal/research-backlog", headers=CONTROL_HEADERS).json()["items"])
    exact = client.post("/api/internal/evidence", headers=HEADERS, json={**evidence_data, "snapshot": {"rcp_no": receipt_id, "dart_source": carried["payload"]}, "dedupe_key":"exact-v2-provenance"}).json()["id"]
    assert finish(exact).status_code == 200


def test_discovery_provenance_uses_the_run_date_cutoff_and_exact_payload():
    run_key = "research-2026-09-16-0700-kst-r9"
    payload = {"company": "issuer", "detail": "contract"}
    source_url = "https://issuer.example.com/notice"
    identity = "discovery:" + hashlib.sha256(canonical({"url": source_url, "announcement_at": "2026-09-15T10:43:20+09:00"})).hexdigest()
    expected = {"identity": identity, "source_url": source_url, "announcement_at": "2026-09-15T10:43:20+09:00", "payload": payload}
    provenance = {"schema_version": "giraffe-discovery-evidence-v1", "run_key": run_key, **expected}
    evidence = {"source_url": source_url, "announcement_at": expected["announcement_at"], "known_at": "2026-09-16T07:00:00+09:00", "research_mode": "scheduled_as_of", "research_run_key": run_key, "eligible_for_original_cutoff": 1, "snapshot": json.dumps({"research_discovery_provenance": provenance})}
    assert api_module._discovery_evidence_matches_run(evidence, expected, run_key)
    evidence["known_at"] = "2020-01-01T06:59:59+09:00"
    assert not api_module._discovery_evidence_matches_run(evidence, expected, run_key)
    assert not api_module._bounded_discovery_payload({"api_key": "secret", "blob": "x" * 2001})


def test_carried_discovery_manual_catch_up_existing_is_exact_atomic_and_idempotent(monkeypatch, tmp_path):
    """A later run may bind only the exact prior manual recovery record."""
    import kr_stock_autotrader.db as db_module
    from kr_stock_autotrader.db import connect

    monkeypatch.setattr(db_module, "DATABASE_PATH", str(tmp_path / "manual-existing.db"))
    client = TestClient(app)
    old = "research-2026-09-16-0700-kst-r3"
    source_url = "https://kind.krx.co.kr/notice/doosan"
    announcement_at = "2026-09-15T10:43:20+09:00"
    payload = {"symbol": "336260", "name": "두산퓨얼셀", "title": "연료전지 시스템 공급 계약"}
    candidate = {"source_url": source_url, "announcement_at": announcement_at, "payload": payload}
    start(client, old, [])
    evidence_data = {"symbol": payload["symbol"], "name": payload["name"], "kind": "news", "title": payload["title"],
        "summary": "contract", "source": "KIND", "source_url": source_url, "announcement_at": announcement_at,
        "known_at": "2026-09-16T15:42:00+09:00", "collected_at": "2026-09-16T15:43:00+09:00", "snapshot": {},
        "dedupe_key": "manual-existing-exact", "research_mode": "manual_catch_up", "research_run_key": old}
    evidence_id = client.post("/api/internal/evidence", headers=HEADERS, json=evidence_data).json()["id"]
    assert client.post(f"/api/internal/scheduler-runs/{old}/finish", headers=HEADERS,
                       json={"status": "error", "count": 0, "detail": {"carry_forward_candidates": [candidate]}}).status_code == 200
    carried = client.get("/api/internal/research-backlog", headers=CONTROL_HEADERS).json()["items"]
    assert len(carried) == 1 and carried[0]["original_announcement_at"] == announcement_at

    key = "research-2026-09-17-0700-kst"
    contract, _ = commitment(key, [])
    contract.update({"schema_version": "giraffe-research-control-v2", "carry_forward": [{"identity": carried[0]["identity"], "kind": "discovery", **candidate}]})
    assert client.post(f"/api/internal/research-runs/{key}/register", json={"control_contract": contract}, headers=CONTROL_HEADERS).status_code == 200
    assert client.post(f"/api/internal/scheduler-runs/{key}/start", json={"kind": "research"}, headers=HEADERS).status_code == 200
    done_receipt = receipt(key, hashlib.sha256(canonical(contract)).hexdigest(), [], rejected_after_evidence=0, existing=1)
    done = {"status": "done", "count": 0, "detail": {"completion_receipt": done_receipt,
        "control_terminal_dispositions": [], "discovery_terminal_dispositions": [{"identity": carried[0]["identity"], "disposition": "existing", "evidence_id": evidence_id}]}}
    for field, value in (("source_url", "https://kind.krx.co.kr/notice/other"), ("announcement_at", "2026-09-15T10:43:21+09:00"),
                         ("symbol", "005930"), ("name", "다른회사"), ("title", "다른 계약")):
        bad = dict(evidence_data, dedupe_key=f"manual-existing-bad-{field}", **{field: value})
        if field == "announcement_at":
            bad["known_at"] = "2026-09-16T15:42:00+09:00"
        bad_id = client.post("/api/internal/evidence", headers=HEADERS, json=bad).json()["id"]
        rejected = json.loads(json.dumps(done))
        rejected["detail"]["discovery_terminal_dispositions"][0]["evidence_id"] = bad_id
        assert client.post(f"/api/internal/scheduler-runs/{key}/finish", headers=HEADERS, json=rejected).status_code == 422
        assert len(client.get("/api/internal/research-backlog", headers=CONTROL_HEADERS).json()["items"]) == 1
    generic = dict(evidence_data, dedupe_key="generic-existing-cannot-clear")
    generic.pop("research_mode"); generic.pop("research_run_key")
    generic_id = client.post("/api/internal/evidence", headers=HEADERS, json=generic).json()["id"]
    rejected = json.loads(json.dumps(done))
    rejected["detail"]["discovery_terminal_dispositions"][0]["evidence_id"] = generic_id
    assert client.post(f"/api/internal/scheduler-runs/{key}/finish", headers=HEADERS, json=rejected).status_code == 422
    assert client.get(f"/api/internal/scheduler-runs/{key}", headers=CONTROL_HEADERS).json()["status"] == "started"
    assert len(client.get("/api/internal/research-backlog", headers=CONTROL_HEADERS).json()["items"]) == 1
    assert client.post(f"/api/internal/scheduler-runs/{key}/finish", headers=HEADERS, json=done).status_code == 200
    assert client.get("/api/internal/research-backlog", headers=CONTROL_HEADERS).json()["items"] == []
    exact = client.get(f"/api/internal/scheduler-runs/{key}", headers=CONTROL_HEADERS).json()
    assert exact["status"] == "done" and exact["detail"]["detail"]["discovery_terminal_dispositions"] == done["detail"]["discovery_terminal_dispositions"]
    assert client.post(f"/api/internal/scheduler-runs/{key}/finish", headers=HEADERS, json=done).status_code == 200
    db = connect()
    try:
        assert db.execute("SELECT COUNT(*) AS count FROM material_evidence").fetchone()["count"] == 7
        row = db.execute("SELECT status,terminal_disposition,terminal_evidence_id FROM giraffe_research_backlog WHERE identity=?", (carried[0]["identity"],)).fetchone()
        assert dict(row) == {"status": "terminal", "terminal_disposition": "existing", "terminal_evidence_id": evidence_id}
    finally:
        db.close()


def test_backlog_seeding_skips_noncanonical_scheduler_rows_at_source():
    """A legacy scheduler key cannot manufacture an unvalidated DART cursor."""
    from kr_stock_autotrader.db import connect

    bad_key = "incident-stale-packet-contract-2026-09-12-1747"
    receipt_id = "20260912001747"
    db = connect()
    try:
        db.execute(
            "INSERT INTO scheduler_runs(run_key,kind,status,started_at,detail) VALUES(?,?,?,?,?)",
            (bad_key, "research", "error", "2026-09-16T07:00:00+09:00", json.dumps({
                "control_commitment": {"control_contract": {"sources": [{"rcp_no": receipt_id}]}}
            })),
        )
        db.commit()
    finally:
        db.close()

    response = TestClient(app).get("/api/internal/research-backlog", headers=CONTROL_HEADERS)
    assert response.status_code == 200
    assert all(item["identity"] != f"dart:{receipt_id}" for item in response.json()["items"])


def test_backlog_readback_terminalizes_noncanonical_pending_and_preserves_119_canonical_rows_idempotently(monkeypatch, tmp_path):
    import kr_stock_autotrader.db as db_module
    from kr_stock_autotrader.db import connect

    # A clean durable store models the RCA denominator without unrelated test
    # rows: all 119 manager-validated canonical entries must remain readable.
    monkeypatch.setattr(db_module, "DATABASE_PATH", str(tmp_path / "backlog.db"))
    bad_key = "incident-stale-packet-contract-2026-09-12-1747"
    bad_identity = "dart:20260912001747"
    canonical_key = "research-2026-09-16-0700-kst-r1"
    canonical_receipts = [f"20260915{number:06d}" for number in range(119)]
    db = connect()
    try:
        db.execute(
            "INSERT INTO giraffe_research_backlog(identity,kind,payload,first_run_key,created_at) VALUES(?,?,?,?,?)",
            (bad_identity, "dart", json.dumps({"rcp_no": "20260912001747"}), bad_key,
             "2026-09-16T07:00:00+09:00"),
        )
        for receipt_id in canonical_receipts:
            source = {"rcp_no": receipt_id, "date": "20260915", "receipt_source_date": "20260915",
                      "packet_path": f"/packets/20260915/{receipt_id}.json", "packet_sha256": "a" * 64}
            db.execute(
                "INSERT INTO giraffe_research_backlog(identity,kind,payload,first_run_key,created_at) VALUES(?,?,?,?,?)",
                (f"dart:{receipt_id}", "dart", json.dumps(source), canonical_key, "2026-09-16T07:00:00+09:00"),
            )
        db.commit()
    finally:
        db.close()

    client = TestClient(app)
    first = client.get("/api/internal/research-backlog", headers=CONTROL_HEADERS)
    assert first.status_code == 200
    items = first.json()["items"]
    assert len(items) == 119
    assert {item["identity"] for item in items} == {f"dart:{receipt_id}" for receipt_id in canonical_receipts}
    assert all(item["first_run_key"] == canonical_key and item["payload"]["receipt_source_date"] == item["payload"]["rcp_no"][:8] for item in items)

    db = connect()
    try:
        quarantined = db.execute(
            "SELECT status,terminal_disposition,terminal_run_key,terminal_at FROM giraffe_research_backlog WHERE identity=?",
            (bad_identity,),
        ).fetchone()
        assert dict(quarantined)["status"] == "terminal"
        assert dict(quarantined)["terminal_disposition"] == "invalid_noncanonical_seed"
        assert dict(quarantined)["terminal_run_key"] == bad_key
        assert dict(quarantined)["terminal_at"]
        first_audit = dict(quarantined)
    finally:
        db.close()

    repeated = client.get("/api/internal/research-backlog", headers=CONTROL_HEADERS)
    assert repeated.status_code == 200 and repeated.json()["items"] == items
    db = connect()
    try:
        repeated_audit = dict(db.execute(
            "SELECT status,terminal_disposition,terminal_run_key,terminal_at FROM giraffe_research_backlog WHERE identity=?",
            (bad_identity,),
        ).fetchone())
        assert repeated_audit == first_audit
    finally:
        db.close()


def test_backlog_seed_cleanup_and_canonical_seed_rollback_together(monkeypatch):
    from kr_stock_autotrader.db import connect

    bad_key = "incident-stale-packet-contract-2026-09-12-1747"
    db = connect()
    try:
        db.execute(
            "INSERT INTO giraffe_research_backlog(identity,kind,payload,first_run_key,created_at) VALUES(?,?,?,?,?)",
            ("dart:20260912009999", "dart", json.dumps({"rcp_no": "20260912009999"}), bad_key,
             "2026-09-16T07:00:00+09:00"),
        )
        canonical_key = "research-2026-09-16-0700-kst-r1"
        db.execute(
            "INSERT INTO scheduler_runs(run_key,kind,status,started_at,detail) VALUES(?,?,?,?,?)",
            (canonical_key, "research", "error", "2026-09-16T07:00:00+09:00", json.dumps({
                "control_commitment": {"control_contract": {"sources": [{"rcp_no": "20260916009999"}]}}
            })),
        )
        db.commit()
        monkeypatch.setattr(api_module, "_enqueue_backlog", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("injected failure")))
        try:
            api_module._seed_unfinished_research_backlog(db)
        except RuntimeError:
            db.rollback()
        else:
            raise AssertionError("seed must surface the injected write failure")
        assert db.execute("SELECT status FROM giraffe_research_backlog WHERE identity='dart:20260912009999'").fetchone()["status"] == "pending"
    finally:
        db.close()


def test_backlog_readback_rolls_back_quarantine_and_seed_when_payload_is_poisoned(monkeypatch, tmp_path):
    """No cursor mutation may commit until every returned payload is JSON-safe."""
    import kr_stock_autotrader.db as db_module
    from kr_stock_autotrader.db import connect

    monkeypatch.setattr(db_module, "DATABASE_PATH", str(tmp_path / "atomic-backlog.db"))
    bad_identity = "dart:legacy-poison"
    seeded_identity = "dart:20260916009999"
    db = connect()
    try:
        db.execute(
            "INSERT INTO giraffe_research_backlog(identity,kind,payload,first_run_key,created_at) VALUES(?,?,?,?,?)",
            (bad_identity, "dart", json.dumps({"rcp_no": "legacy-poison"}),
             "incident-stale-packet-contract-2026-09-12-1747", "2026-09-16T07:00:00+09:00"),
        )
        db.execute(
            "INSERT INTO giraffe_research_backlog(identity,kind,payload,first_run_key,created_at) VALUES(?,?,?,?,?)",
            ("dart:poison-canonical", "dart", "{", "research-2026-09-16-0700-kst-r1", "2026-09-16T07:00:00+09:00"),
        )
        db.execute(
            "INSERT INTO scheduler_runs(run_key,kind,status,started_at,detail) VALUES(?,?,?,?,?)",
            ("research-2026-09-16-0700-kst-r2", "research", "error", "2026-09-16T07:00:00+09:00", json.dumps({
                "control_commitment": {"control_contract": {"sources": [{"rcp_no": "20260916009999"}]}}
            })),
        )
        db.commit()
    finally:
        db.close()

    response = TestClient(app, raise_server_exceptions=False).get(
        "/api/internal/research-backlog", headers=CONTROL_HEADERS
    )
    assert response.status_code == 500
    db = connect()
    try:
        assert db.execute("SELECT status FROM giraffe_research_backlog WHERE identity=?", (bad_identity,)).fetchone()["status"] == "pending"
        assert db.execute("SELECT identity FROM giraffe_research_backlog WHERE identity=?", (seeded_identity,)).fetchone() is None
    finally:
        db.close()


def test_backlog_seed_requires_a_real_calendar_date_and_preserves_valid_reruns(monkeypatch, tmp_path):
    import kr_stock_autotrader.db as db_module
    from kr_stock_autotrader.db import connect

    monkeypatch.setattr(db_module, "DATABASE_PATH", str(tmp_path / "calendar-backlog.db"))
    invalid_keys = [
        "research-2026-99-99-0700-kst",
        "research-2026-02-29-0700-kst-r1",
        "research-2026-09-16-0700-kst-r0",
    ]
    valid_keys = ["research-2024-02-29-0700-kst-r1", "research-2026-09-16-0700-kst-r2"]
    db = connect()
    try:
        for index, run_key in enumerate(invalid_keys + valid_keys):
            db.execute(
                "INSERT INTO giraffe_research_backlog(identity,kind,payload,first_run_key,created_at) VALUES(?,?,?,?,?)",
                (f"dart:calendar-{index}", "dart", json.dumps({"rcp_no": f"calendar-{index}"}), run_key,
                 "2026-09-16T07:00:00+09:00"),
            )
        db.execute(
            "INSERT INTO scheduler_runs(run_key,kind,status,started_at,detail) VALUES(?,?,?,?,?)",
            ("research-2026-99-99-0700-kst", "research", "error", "2026-09-16T07:00:00+09:00", json.dumps({
                "control_commitment": {"control_contract": {"sources": [{"rcp_no": "20260916999999"}]}}
            })),
        )
        db.commit()
    finally:
        db.close()

    response = TestClient(app).get("/api/internal/research-backlog", headers=CONTROL_HEADERS)
    assert response.status_code == 200
    assert {item["first_run_key"] for item in response.json()["items"]} == set(valid_keys)
    db = connect()
    try:
        rows = db.execute("SELECT identity,status FROM giraffe_research_backlog ORDER BY identity").fetchall()
        statuses = {row["identity"]: row["status"] for row in rows}
        assert all(statuses[f"dart:calendar-{index}"] == "terminal" for index in range(len(invalid_keys)))
        assert all(statuses[f"dart:calendar-{index}"] == "pending" for index in range(len(invalid_keys), len(invalid_keys) + len(valid_keys)))
        assert "dart:20260916999999" not in statuses
    finally:
        db.close()
