import hashlib
import io
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


def test_production_shaped_discovery_backlog_normalizes_into_registerable_v2_contract(monkeypatch, tmp_path):
    """The prehook must consume the API's DB-row envelope, not a legacy flattened shape."""
    import importlib.util
    import sys
    import kr_stock_autotrader.db as db_module
    from kr_stock_autotrader.db import connect

    monkeypatch.setattr(db_module, "DATABASE_PATH", str(tmp_path / "prehook-contract.db"))
    scripts = Path(__file__).parents[1] / "scripts"
    prior_manifest = sys.modules.get("giraffe_dart_manifest")
    sys.path.insert(0, str(scripts))
    try:
        manifest_spec = importlib.util.spec_from_file_location("giraffe_dart_manifest_e2e", scripts / "giraffe_dart_manifest.py")
        assert manifest_spec and manifest_spec.loader
        manifest = importlib.util.module_from_spec(manifest_spec)
        sys.modules[manifest_spec.name] = manifest; manifest_spec.loader.exec_module(manifest)
        sys.modules["giraffe_dart_manifest"] = manifest
        gate_spec = importlib.util.spec_from_file_location("giraffe_dart_manifest_gate_e2e", scripts / "giraffe_dart_manifest_gate.py")
        assert gate_spec and gate_spec.loader
        gate = importlib.util.module_from_spec(gate_spec)
        sys.modules[gate_spec.name] = gate; gate_spec.loader.exec_module(gate)
        announced, source_url = "2026-09-15T10:43:20+09:00", "https://KIND.KRX.CO.KR:443/notice/doosan"
        canonical_url = "https://kind.krx.co.kr/notice/doosan"
        identity = hashlib.sha256(canonical({"url": canonical_url, "announcement_at": announced})).hexdigest()
        envelope = {"source_url": source_url, "announcement_at": announced, "payload": {"symbol": "336260", "name": "두산퓨얼셀", "title": "공급 계약", "source": "KIND", "reason": "material"}}
        db = connect()
        try:
            api_module._enqueue_backlog(db, kind="discovery", identity=identity, payload=envelope,
                                        run_key="research-2026-09-15-0700-kst-r1", announcement_at=announced)
            db.commit()
        finally:
            db.close()
        client = TestClient(app)
        backlog = client.get("/api/internal/research-backlog", headers=CONTROL_HEADERS)
        assert backlog.status_code == 200
        contract, _ = commitment("research-2026-09-16-0700-kst", [])
        contract.update({"schema_version": "giraffe-research-control-v2", "carry_forward": gate.control_contract(contract["run_key"], [], backlog.json()["items"])["carry_forward"]})
        registered = client.post(f"/api/internal/research-runs/{contract['run_key']}/register", json={"control_contract": contract}, headers=CONTROL_HEADERS)
        assert registered.status_code == 200
        readback = client.get(f"/api/internal/scheduler-runs/{contract['run_key']}", headers=CONTROL_HEADERS)
        assert readback.status_code == 200
        assert readback.json()["detail"]["control_commitment"]["control_contract"]["carry_forward"] == [{"identity": "discovery:" + identity, "kind": "discovery", "source_url": canonical_url, "announcement_at": announced, "payload": envelope["payload"]}]
    finally:
        sys.path.remove(str(scripts))
        sys.modules.pop("giraffe_dart_manifest_e2e", None)
        sys.modules.pop("giraffe_dart_manifest_gate_e2e", None)
        if prior_manifest is None:
            sys.modules.pop("giraffe_dart_manifest", None)
        else:
            sys.modules["giraffe_dart_manifest"] = prior_manifest


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


def test_manual_existing_requires_a_strictly_earlier_terminal_canonical_research_run(monkeypatch, tmp_path):
    """Future, started, later, same, and noncanonical evidence runs cannot clear discovery."""
    import kr_stock_autotrader.db as db_module
    from kr_stock_autotrader.db import connect

    monkeypatch.setattr(db_module, "DATABASE_PATH", str(tmp_path / "manual-order.db"))
    db = connect()
    completing = "research-2026-09-17-0700-kst-r4"
    cases = (
        ("research-2026-09-18-0700-kst", "started", False),  # future started
        ("research-2026-09-16-0700-kst", "started", False),  # earlier started
        ("research-2026-09-19-0700-kst", "done", False),     # later terminal
        (completing, "error", False),                           # same run
        ("research-2026-09-16-0700-kst-r3", "error", True),  # earlier terminal error
        ("research-2026-09-16-0700-kst-r4", "done", True),   # earlier terminal done
        ("research-2026-09-17-0700-kst-r3", "error", True),  # same-date lower rerun
    )
    try:
        for key, status, _ in cases:
            db.execute("INSERT INTO scheduler_runs(run_key,kind,status,started_at,detail) VALUES(?,?,?,?,?)",
                       (key, "research", status, "2026-09-16T07:00:00+09:00", "{}"))
        db.execute("INSERT INTO scheduler_runs(run_key,kind,status,started_at,detail) VALUES(?,?,?,?,?)",
                   ("research-2026-09-15-0700-kst-r0", "research", "done", "2026-09-16T07:00:00+09:00", "{}"))
        db.commit()
        for key, _, expected in cases:
            assert api_module._is_terminal_prior_research_run(db, key, completing) is expected
        assert not api_module._is_terminal_prior_research_run(db, "research-2026-09-15-0700-kst-r0", completing)
    finally:
        db.close()


def test_carried_discovery_manual_catch_up_existing_is_exact_atomic_and_idempotent(monkeypatch, tmp_path):
    """A later run may bind only the exact prior manual recovery record."""
    import kr_stock_autotrader.db as db_module
    from kr_stock_autotrader.db import connect

    monkeypatch.setattr(db_module, "DATABASE_PATH", str(tmp_path / "manual-existing.db"))
    client = TestClient(app)
    old = "research-2026-09-17-0700-kst-r3"
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

    key = "research-2026-09-17-0700-kst-r4"
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


def test_v3_terminal_exclusion_and_beautyskin_append_only_correction(monkeypatch, tmp_path):
    """A prior rejected terminal row is excluded normally and recoverable only by a new correction cursor."""
    import kr_stock_autotrader.db as db_module
    from kr_stock_autotrader.db import connect
    monkeypatch.setattr(db_module, "DATABASE_PATH", str(tmp_path / "beautyskin.db"))
    client = TestClient(app)
    old, key, rcp = "research-2026-09-16-0700-kst-r7", "research-2026-09-17-0700-kst-r8", "20260916900230"
    source = commitment(key, [rcp])[0]["sources"][0]
    source.update({'report_class': 'dart_single_sale_supply_contract', 'report_name': '단일판매ㆍ공급계약체결'})
    # Historical terminal payload predates v3 classification and must remain unchanged.
    legacy_terminal_source = {field: source[field] for field in ('rcp_no', 'date', 'receipt_source_date', 'packet_path', 'packet_sha256')}
    db = connect()
    try:
        db.execute("INSERT INTO scheduler_runs(run_key,kind,status,started_at,finished_at,detail) VALUES(?,?,?,?,?,?)", (old, "research", "done", "2026-09-16T07:00:00+09:00", "2026-09-16T07:01:00+09:00", "{}"))
        db.execute("INSERT INTO giraffe_research_backlog(identity,kind,payload,first_run_key,status,terminal_disposition,terminal_run_key,terminal_evidence_id,created_at,terminal_at) VALUES(?,?,?,?,?,?,?,?,?,?)", ("dart:" + rcp, "dart", json.dumps(legacy_terminal_source, sort_keys=True), old, "terminal", "rejected", old, None, "2026-09-16T07:00:00+09:00", "2026-09-16T07:01:00+09:00"))
        db.commit()
        original = dict(db.execute("SELECT identity,kind,payload,terminal_disposition,terminal_run_key,terminal_evidence_id,terminal_at FROM giraffe_research_backlog WHERE identity=?", ("dart:" + rcp,)).fetchone())
        original["payload"] = json.loads(original["payload"])
    finally:
        db.close()
    assert original['payload'] == legacy_terminal_source

    excluded, _ = commitment(key, [])
    excluded.update({"schema_version": "giraffe-research-control-v3", "carry_forward": [], "terminal_exclusions": [original], "correction_of": []})
    response = client.post(f"/api/internal/research-runs/{key}/register", json={"control_contract": excluded}, headers=CONTROL_HEADERS)
    assert response.status_code == 200, response.text
    excluded_digest = hashlib.sha256(canonical(excluded)).hexdigest()
    assert client.post(f"/api/internal/scheduler-runs/{key}/finish", headers=HEADERS, json={"status": "done", "count": 0, "detail": {"completion_receipt": receipt(key, excluded_digest, []), "control_terminal_dispositions": [], "discovery_terminal_dispositions": []}}).status_code == 200

    correction_key = "research-2026-09-17-0700-kst-r9"; contract, _ = commitment(correction_key, [rcp])
    contract['sources'][0].update({'report_class': 'dart_single_sale_supply_contract', 'report_name': '단일판매ㆍ공급계약체결'})
    contract.update({"schema_version": "giraffe-research-control-v3", "carry_forward": [], "terminal_exclusions": [], "correction_of": [original]})
    assert client.post(f"/api/internal/research-runs/{correction_key}/register", json={"control_contract": contract}, headers=CONTROL_HEADERS).status_code == 200
    evidence = client.post("/api/internal/evidence", headers=HEADERS, json={"symbol": "406820", "name": "뷰티스킨", "kind": "contract", "title": "공급계약", "summary": "30bn KRW China contract", "source": "DART", "source_url": "https://dart.fss.or.kr/beautyskin", "announcement_at": "2026-09-17T06:00:00+09:00", "known_at": "2026-09-17T13:19:00+09:00", "research_mode": "manual_catch_up", "research_run_key": correction_key, "snapshot": {"rcp_no": rcp, "dart_source": source}, "dedupe_key": "beautyskin-append-only-correction"})
    assert evidence.status_code == 200
    stored_correction = client.get(f"/api/internal/evidence/{evidence.json()['id']}", headers=HEADERS)
    assert stored_correction.status_code == 200
    assert stored_correction.json()["research_mode"] == "manual_catch_up"
    assert stored_correction.json()["eligible_for_original_cutoff"] == 0
    assert stored_correction.json()["known_at"] == "2026-09-17T13:19:00+09:00"
    digest = hashlib.sha256(canonical(contract)).hexdigest()
    result = receipt(correction_key, digest, [rcp], rejected_after_evidence=0, correction_stored=1)
    candidate = result["coverage_lanes"]["kind_krx"]["checked_sources"][0]
    candidate.update({"url": "https://dart.fss.or.kr/beautyskin", "outcome": "candidate", "economic_disposition": "correction_stored", "economic_reason": "KRW 30bn, 54.62% prior revenue, China, 5% advance", "evidence_id": evidence.json()["id"]})
    result["coverage_lanes"]["kind_krx"]["candidate_count"] = 1
    audit = {'economic_disposition': 'qualifying_A_or_better', 'economic_reason': 'binding contract is 54.62% of prior revenue', 'economic_facts': {'binding_contract': True, 'contract_amount': 30000000000, 'prior_revenue': 54920000000, 'ratio_percent': 54.62, 'term': '2026-09-17 to 2027-09-16'}}
    done = {"status": "done", "count": 1, "detail": {"completion_receipt": result, "control_terminal_dispositions": [{"rcp_no": rcp, "disposition": "correction_stored", "evidence_id": evidence.json()["id"], **audit}]}}
    assert client.post(f"/api/internal/scheduler-runs/{correction_key}/finish", headers=HEADERS, json=done).status_code == 200

    # The second contract is assembled through the actual prehook terminal-lookup
    # path.  Its correction selection fails before registration because the
    # lookup exposes both the immutable original and the terminal correction.
    import importlib.util
    import sys
    scripts = Path(__file__).parents[1] / 'scripts'
    sys.path.insert(0, str(scripts))
    try:
        spec = importlib.util.spec_from_file_location('giraffe_dart_manifest_gate_replay', scripts / 'giraffe_dart_manifest_gate.py')
        assert spec and spec.loader
        gate = importlib.util.module_from_spec(spec); sys.modules[spec.name] = gate; spec.loader.exec_module(gate)
        class LocalResponse(io.BytesIO):
            def __init__(self, request):
                response = client.post('/api/internal/research-backlog/terminal-items', content=request.data,
                                       headers={'X-Research-Control-Key': CONTROL_HEADERS['X-Research-Control-Key'], 'Content-Type': 'application/json'})
                assert response.status_code == 200, response.text
                super().__init__(response.content); self.status = response.status_code
            def __enter__(self): return self
            def __exit__(self, *_): self.close()
        monkeypatch.setenv('GIRAFFE_URL', 'http://giraffe.test')
        monkeypatch.setenv('RESEARCH_CONTROL_KEY', CONTROL_HEADERS['X-Research-Control-Key'])
        monkeypatch.setattr(gate.urllib.request, 'urlopen', lambda request, timeout: LocalResponse(request))
        terminal_history = gate.fetch_terminal_history([rcp])
        assert original in terminal_history
        assert len([item for item in terminal_history if item['identity'].startswith('dart:correction:')]) == 1
        with __import__('pytest').raises(gate.ManifestError, match='selected correction receipt'):
            gate.control_contract('research-2026-09-17-0700-kst-r10', [], terminal_history=terminal_history, correction_receipts=[rcp])
    finally:
        sys.path.remove(str(scripts)); sys.modules.pop('giraffe_dart_manifest_gate_replay', None)
    assert client.get('/api/internal/scheduler-runs/research-2026-09-17-0700-kst-r10', headers=CONTROL_HEADERS).status_code == 404
    db = connect()
    try:
        preserved = dict(db.execute("SELECT identity,kind,payload,terminal_disposition,terminal_run_key,terminal_evidence_id,terminal_at FROM giraffe_research_backlog WHERE identity=?", ("dart:" + rcp,)).fetchone()); preserved["payload"] = json.loads(preserved["payload"])
        assert preserved == original
        correction_rows = db.execute("SELECT identity,terminal_disposition,terminal_run_key,terminal_evidence_id FROM giraffe_research_backlog WHERE identity LIKE 'dart:correction:%' ORDER BY identity").fetchall()
        assert len(correction_rows) == 1
        assert dict(correction_rows[0]) == {"identity": correction_rows[0]["identity"], "terminal_disposition": "correction_stored", "terminal_run_key": correction_key, "terminal_evidence_id": evidence.json()["id"]}
    finally:
        db.close()


def test_v3_registration_derives_contract_class_from_exact_authoritative_report_name():
    client = TestClient(app); rcp = '20260917000002'
    for number, name, supplied, expected in (
        (1, '단일판매ㆍ공급계약체결', 'other', 422),
        (2, '단일판매 · 공급계약 / 체결', 'other', 422),
        (3, '단일판매ㆍ공급계약체결(자율공시)', 'dart_single_sale_supply_contract', 422),
        (4, '단일판매ㆍ공급계약체결', 'dart_single_sale_supply_contract', 200),
        (5, '[기재정정]단일판매ㆍ공급계약체결', 'dart_single_sale_supply_contract', 200),
        (6, '[기재정정] 단일판매 · 공급계약 / 체결', 'dart_single_sale_supply_contract', 200),
        (7, '[기재정정]단일판매ㆍ공급계약체결(자율공시)', 'dart_single_sale_supply_contract', 422),
        (8, '임의[기재정정]단일판매ㆍ공급계약체결', 'dart_single_sale_supply_contract', 422),
        (9, '[기재정정]단일판매ㆍ공급계약체결추가', 'dart_single_sale_supply_contract', 422),
        (10, None, None, 422),
        (11, '   ', 'other', 422),
        (12, '기타경영사항', None, 422),
    ):
        key = f'research-2026-09-17-0700-kst-r{number}'
        contract, _ = commitment(key, [rcp])
        contract['sources'][0].update({'report_class': supplied, 'report_name': name})
        contract.update({'schema_version': 'giraffe-research-control-v3', 'carry_forward': [], 'terminal_exclusions': [], 'correction_of': []})
        response = client.post(f'/api/internal/research-runs/{key}/register', json={'control_contract': contract}, headers=CONTROL_HEADERS)
        assert response.status_code == expected, response.text
        if expected == 422:
            assert client.get(f'/api/internal/scheduler-runs/{key}', headers=CONTROL_HEADERS).status_code == 404
        else:
            stored = client.get(f'/api/internal/scheduler-runs/{key}', headers=CONTROL_HEADERS).json()
            assert stored['detail']['control_commitment']['control_contract']['sources'][0]['report_class'] == 'dart_single_sale_supply_contract'


def test_v3_registration_accepts_legacy_dart_carry_only_for_exact_immutable_core():
    client = TestClient(app); rcp = '20260917000002'
    source_contract, _ = commitment('research-2026-09-17-0700-kst-r11', [rcp])
    source = source_contract['sources'][0]
    source.update({'report_class': 'dart_single_sale_supply_contract', 'report_name': '단일판매ㆍ공급계약체결'})
    legacy = {field: source[field] for field in ('rcp_no', 'date', 'receipt_source_date', 'packet_path', 'packet_sha256')}
    contract = {**source_contract, 'schema_version': 'giraffe-research-control-v3',
                'carry_forward': [{'identity': 'dart:' + rcp, 'kind': 'dart', 'payload': legacy}],
                'terminal_exclusions': [], 'correction_of': []}
    response = client.post('/api/internal/research-runs/research-2026-09-17-0700-kst-r11/register', json={'control_contract': contract}, headers=CONTROL_HEADERS)
    assert response.status_code == 200, response.text
    stored = client.get('/api/internal/scheduler-runs/research-2026-09-17-0700-kst-r11', headers=CONTROL_HEADERS).json()
    stored_contract = stored['detail']['control_commitment']['control_contract']
    assert stored_contract['carry_forward'][0]['payload'] == legacy
    assert stored_contract['sources'][0] == source

    for number, field, value in ((12, 'packet_sha256', 'b' * 64), (13, 'packet_path', source['packet_path'] + '.other'),
                                 (14, 'date', '20260916'), (15, 'receipt_source_date', '20260916')):
        key = f'research-2026-09-17-0700-kst-r{number}'
        bad = json.loads(json.dumps(contract)); bad['run_key'] = key
        bad['carry_forward'][0]['payload'][field] = value
        response = client.post(f'/api/internal/research-runs/{key}/register', json={'control_contract': bad}, headers=CONTROL_HEADERS)
        assert response.status_code == 422, response.text

    # Carry-forward payloads are legacy five-field provenance only; they cannot
    # introduce a caller-selected classification that downgrades the v3 source.
    key = 'research-2026-09-17-0700-kst-r16'
    classified = json.loads(json.dumps(contract)); classified['run_key'] = key
    classified['carry_forward'][0]['payload']['report_class'] = 'other'
    response = client.post(f'/api/internal/research-runs/{key}/register', json={'control_contract': classified}, headers=CONTROL_HEADERS)
    assert response.status_code == 422, response.text


def test_v3_finish_accepts_pending_legacy_carry_without_rewriting_its_payload(monkeypatch, tmp_path):
    import kr_stock_autotrader.db as db_module
    from kr_stock_autotrader.db import connect
    monkeypatch.setattr(db_module, 'DATABASE_PATH', str(tmp_path / 'legacy-carry-finish.db'))
    client = TestClient(app); key = 'research-2026-09-17-0700-kst-r11'; rcp = '20260916000465'
    contract, _ = commitment(key, [rcp])
    source = contract['sources'][0]
    source.update({'report_class': 'other', 'report_name': '주요사항보고서(유상증자결정)'})
    legacy = {field: source[field] for field in ('rcp_no', 'date', 'receipt_source_date', 'packet_path', 'packet_sha256')}
    contract.update({'schema_version': 'giraffe-research-control-v3',
                     'carry_forward': [{'identity': 'dart:' + rcp, 'kind': 'dart', 'payload': legacy}],
                     'terminal_exclusions': [], 'correction_of': []})
    db = connect()
    try:
        db.execute("INSERT INTO giraffe_research_backlog(identity,kind,payload,first_run_key,created_at) VALUES(?,?,?,?,?)",
                   ('dart:' + rcp, 'dart', json.dumps(legacy, sort_keys=True), 'research-2026-09-16-0700-kst-r7', '2026-09-16T07:00:00+09:00'))
        db.commit()
    finally:
        db.close()
    assert client.post(f'/api/internal/research-runs/{key}/register', json={'control_contract': contract}, headers=CONTROL_HEADERS).status_code == 200
    digest = hashlib.sha256(canonical(contract)).hexdigest()
    value = receipt(key, digest, [rcp])
    done = {'status': 'done', 'count': 0, 'detail': {'completion_receipt': value,
            'control_terminal_dispositions': [{'rcp_no': rcp, 'disposition': 'rejected', 'evidence_id': None,
                                               'economic_disposition': 'negative_risk', 'economic_reason': 'dilutive financing risk',
                                               'economic_facts': None}]}}
    response = client.post(f'/api/internal/scheduler-runs/{key}/finish', headers=HEADERS, json=done)
    assert response.status_code == 200, response.text
    db = connect()
    try:
        row = db.execute('SELECT payload,status,terminal_run_key FROM giraffe_research_backlog WHERE identity=?', ('dart:' + rcp,)).fetchone()
        assert json.loads(row['payload']) == legacy
        assert row['status'] == 'terminal' and row['terminal_run_key'] == key
    finally:
        db.close()


def test_v3_finish_terminalizes_normalized_core_carry_against_authoritative_classified_backlog(monkeypatch, tmp_path):
    import kr_stock_autotrader.db as db_module
    from kr_stock_autotrader.db import connect
    monkeypatch.setattr(db_module, 'DATABASE_PATH', str(tmp_path / 'classified-carry-finish.db'))
    client = TestClient(app)

    def finish_with_backlog(number, mutation, expected_status):
        key = f'research-2026-09-17-0700-kst-r{number}'
        rcp = f'202609160004{number:02d}'
        contract, _ = commitment(key, [rcp])
        source = contract['sources'][0]
        source.update({'report_class': 'other', 'report_name': '주요사항보고서(유상증자결정)'})
        stored_source = {**source, **mutation}
        core = {field: source[field] for field in ('rcp_no', 'date', 'receipt_source_date', 'packet_path', 'packet_sha256')}
        contract.update({'schema_version': 'giraffe-research-control-v3',
                         'carry_forward': [{'identity': 'dart:' + rcp, 'kind': 'dart', 'payload': core}],
                         'terminal_exclusions': [], 'correction_of': []})
        assert client.post(f'/api/internal/research-runs/{key}/register', json={'control_contract': contract}, headers=CONTROL_HEADERS).status_code == 200
        db = connect()
        try:
            existing = db.execute('SELECT payload,status FROM giraffe_research_backlog WHERE identity=?', ('dart:' + rcp,)).fetchone()
            assert json.loads(existing['payload']) == source and existing['status'] == 'pending'
            db.execute('UPDATE giraffe_research_backlog SET payload=? WHERE identity=?',
                       (json.dumps(stored_source, sort_keys=True), 'dart:' + rcp))
            db.commit()
        finally:
            db.close()
        done = {'status': 'done', 'count': 0, 'detail': {'completion_receipt': receipt(key, hashlib.sha256(canonical(contract)).hexdigest(), [rcp]),
                'control_terminal_dispositions': [{'rcp_no': rcp, 'disposition': 'rejected', 'evidence_id': None,
                                                   'economic_disposition': 'negative_risk', 'economic_reason': 'dilutive financing risk',
                                                   'economic_facts': None}]}}
        response = client.post(f'/api/internal/scheduler-runs/{key}/finish', headers=HEADERS, json=done)
        assert response.status_code == expected_status, response.text
        db = connect()
        try:
            row = db.execute('SELECT payload,status,terminal_run_key FROM giraffe_research_backlog WHERE identity=?', ('dart:' + rcp,)).fetchone()
            assert json.loads(row['payload']) == stored_source
            assert row['status'] == ('terminal' if expected_status == 200 else 'pending')
            assert row['terminal_run_key'] == (key if expected_status == 200 else None)
        finally:
            db.close()

    finish_with_backlog(66, {}, 200)
    for field, value in (('report_class', 'dart_single_sale_supply_contract'), ('report_name', '변경된 보고서명')):
        finish_with_backlog(67 if field == 'report_class' else 68, {field: value}, 422)


def test_v3_economic_audit_rejects_bare_malformed_and_qualifying_rejected_hold_then_accepts_saved():
    client = TestClient(app); rcp = "20260917000001"

    def v3_started(key):
        contract, _ = commitment(key, [rcp])
        source = contract["sources"][0]
        source.update({"report_class": "dart_single_sale_supply_contract", "report_name": "단일판매ㆍ공급계약체결"})
        contract.update({"schema_version": "giraffe-research-control-v3", "carry_forward": [], "terminal_exclusions": [], "correction_of": []})
        assert client.post(f"/api/internal/research-runs/{key}/register", json={"control_contract": contract}, headers=CONTROL_HEADERS).status_code == 200
        registered = client.get(f"/api/internal/scheduler-runs/{key}", headers=CONTROL_HEADERS)
        assert registered.status_code == 200
        assert registered.json()["detail"]["control_commitment"]["control_contract"]["sources"] == [source]
        assert client.post(f"/api/internal/scheduler-runs/{key}/start", json={"kind": "research"}, headers=HEADERS).status_code == 200
        return contract

    audit = {"economic_disposition": "qualifying_A_or_better", "economic_reason": "binding contract exceeds half of prior revenue", "economic_facts": {"binding_contract": True, "contract_amount": 50, "prior_revenue": 100, "ratio_percent": 50, "term": "2026-09-19 to 2027-09-18"}}
    stale_ratio = {**audit, "economic_facts": {**audit["economic_facts"], "ratio_percent": 49.99}}
    for number, disposition, extra in ((1, "rejected", {}), (2, "hold", {}), (3, "rejected", {"economic_reason": ""}), (4, "rejected", {"economic_reason": "x" * 1001}), (5, "rejected", {"economic_facts": {}}), (6, "rejected", {"economic_disposition": None, "economic_reason": None, "economic_facts": None}), (7, "rejected", stale_ratio), (8, "rejected", {"report_class": "other"})):
        key = f"research-2026-09-17-0700-kst-r{number}"; contract = v3_started(key)
        item = {"rcp_no": rcp, "disposition": disposition, "evidence_id": None, **audit, **extra}
        done = {"status": "done", "count": 0, "detail": {"completion_receipt": receipt(key, hashlib.sha256(canonical(contract)).hexdigest(), [rcp]), "control_terminal_dispositions": [item]}}
        assert client.post(f"/api/internal/scheduler-runs/{key}/finish", headers=HEADERS, json=done).status_code == 422

    key = "research-2026-09-17-0700-kst-r7"; contract = v3_started(key); source = contract["sources"][0]
    evidence = client.post("/api/internal/evidence", headers=HEADERS, json={"symbol": "406820", "kind": "contract", "title": "공급계약", "summary": "binding contract", "source": "DART", "source_url": "https://dart.fss.or.kr/qualifying", "announcement_at": "2026-09-17T06:00:00+09:00", "known_at": "2026-09-17T06:30:00+09:00", "research_mode": "scheduled_as_of", "research_run_key": key, "snapshot": {"rcp_no": rcp, "dart_source": source}, "dedupe_key": "v3-qualifying-saved"})
    assert evidence.status_code == 200, evidence.text
    value = receipt(key, hashlib.sha256(canonical(contract)).hexdigest(), [rcp], rejected_after_evidence=0, saved=1)
    candidate = value["coverage_lanes"]["kind_krx"]["checked_sources"][0]
    candidate.update({"url": "https://dart.fss.or.kr/qualifying", "outcome": "candidate", "economic_disposition": "saved", "economic_reason": "binding contract stored", "evidence_id": evidence.json()["id"]})
    value["coverage_lanes"]["kind_krx"]["candidate_count"] = 1
    done = {"status": "done", "count": 1, "detail": {"completion_receipt": value, "control_terminal_dispositions": [{"rcp_no": rcp, "disposition": "saved", "evidence_id": evidence.json()["id"], **audit}]}}
    assert client.post(f"/api/internal/scheduler-runs/{key}/finish", headers=HEADERS, json=done).status_code == 200


def test_v3_corrected_contract_requires_incremental_amendment_economics():
    client = TestClient(app); rcp = "20260917000003"

    def started(number):
        key = f"research-2026-09-17-0700-kst-r{number}"
        contract, _ = commitment(key, [rcp])
        source = contract["sources"][0]
        source.update({"report_class": "dart_single_sale_supply_contract", "report_name": "[기재정정]단일판매ㆍ공급계약체결"})
        contract.update({"schema_version": "giraffe-research-control-v3", "carry_forward": [], "terminal_exclusions": [], "correction_of": []})
        assert client.post(f"/api/internal/research-runs/{key}/register", json={"control_contract": contract}, headers=CONTROL_HEADERS).status_code == 200
        assert client.post(f"/api/internal/scheduler-runs/{key}/start", json={"kind": "research"}, headers=HEADERS).status_code == 200
        return key, contract

    # HanJeon-shaped amended total is 81.2% of revenue; its actual increment is only 1.97%.
    facts = {"binding_contract": True, "economic_basis": "amendment_delta", "original_contract_amount": 792300,
             "amended_contract_amount": 812000, "incremental_contract_amount": 19700, "prior_revenue": 1000000,
             "incremental_ratio_percent": 1.97, "original_term": "2026-01-01 to 2026-12-31", "amended_term": "2026-01-01 to 2027-06-30"}
    audit = {"economic_disposition": "below_threshold", "economic_reason": "amendment increment is below half of prior revenue", "economic_facts": facts}
    key, contract = started(20)
    done = {"status": "done", "count": 0, "detail": {"completion_receipt": receipt(key, hashlib.sha256(canonical(contract)).hexdigest(), [rcp]), "control_terminal_dispositions": [{"rcp_no": rcp, "disposition": "rejected", "evidence_id": None, **audit}]}}
    assert client.post(f"/api/internal/scheduler-runs/{key}/finish", headers=HEADERS, json=done).status_code == 200

    legacy = {"binding_contract": True, "contract_amount": 812000, "prior_revenue": 1000000, "ratio_percent": 81.2, "term": "2026-01-01 to 2027-06-30"}
    invalid_facts = [
        legacy,
        {**facts, "incremental_contract_amount": 19701},
        {**facts, "incremental_ratio_percent": 1.96},
        {**facts, "incremental_ratio_percent": float("nan")},
        {**facts, "original_contract_amount": True},
        {**facts, "unexpected": 1},
        {key: value for key, value in facts.items() if key != "amended_term"},
    ]
    for number, bad_facts in enumerate(invalid_facts, 21):
        key, contract = started(number)
        bad = {**audit, "economic_facts": bad_facts}
        done = {"status": "done", "count": 0, "detail": {"completion_receipt": receipt(key, hashlib.sha256(canonical(contract)).hexdigest(), [rcp]), "control_terminal_dispositions": [{"rcp_no": rcp, "disposition": "rejected", "evidence_id": None, **bad}]}}
        response = (client.post(f"/api/internal/scheduler-runs/{key}/finish", headers={**HEADERS, "content-type": "application/json"}, content=json.dumps(done))
                    if isinstance(bad_facts.get("incremental_ratio_percent"), float) and __import__("math").isnan(bad_facts["incremental_ratio_percent"])
                    else client.post(f"/api/internal/scheduler-runs/{key}/finish", headers=HEADERS, json=done))
        assert response.status_code == 422

    qualifying = {**facts, "amended_contract_amount": 1292300, "incremental_contract_amount": 500000,
                  "incremental_ratio_percent": 50}
    source = {"report_class": "dart_single_sale_supply_contract", "report_name": "[기재정정]단일판매ㆍ공급계약체결"}
    assert api_module._valid_control_economic_audit(source, {**audit, "economic_disposition": "qualifying_A_or_better", "economic_facts": qualifying})
    assert not api_module._valid_control_economic_audit(source, {**audit, "economic_facts": qualifying})
    zero = {**facts, "amended_contract_amount": 792300, "incremental_contract_amount": 0, "incremental_ratio_percent": 0}
    assert not api_module._valid_control_economic_audit(source, {**audit, "economic_disposition": "qualifying_A_or_better", "economic_facts": zero})

def test_v3_fresh_corrected_qualifying_terminalization_requires_bound_evidence(monkeypatch, tmp_path):
    """Every assertion starts a new corrected-title cursor, never a prior terminal receipt."""
    import kr_stock_autotrader.db as db_module
    from kr_stock_autotrader.db import connect

    monkeypatch.setattr(db_module, "DATABASE_PATH", str(tmp_path / "fresh-corrected-terminalization.db"))
    client = TestClient(app)
    qualifying_facts = {
        "binding_contract": True, "economic_basis": "amendment_delta", "original_contract_amount": 500000,
        "amended_contract_amount": 1000000, "incremental_contract_amount": 500000, "prior_revenue": 1000000,
        "incremental_ratio_percent": 50, "original_term": "2026-01-01 to 2026-12-31",
        "amended_term": "2026-01-01 to 2027-06-30",
    }
    qualifying_audit = {
        "economic_disposition": "qualifying_A_or_better",
        "economic_reason": "binding amendment increment meets half of prior revenue",
        "economic_facts": qualifying_facts,
    }

    def fresh_started(case):
        key, rcp = f"research-2026-09-17-0700-kst-r{600 + case}", f"20260917{600 + case:06d}"
        contract, _ = commitment(key, [rcp])
        source = contract["sources"][0]
        source.update({"report_class": "dart_single_sale_supply_contract", "report_name": "[기재정정]단일판매ㆍ공급계약체결"})
        contract.update({"schema_version": "giraffe-research-control-v3", "carry_forward": [], "terminal_exclusions": [], "correction_of": []})
        assert client.post(f"/api/internal/research-runs/{key}/register", json={"control_contract": contract}, headers=CONTROL_HEADERS).status_code == 200
        assert client.post(f"/api/internal/scheduler-runs/{key}/start", json={"kind": "research"}, headers=HEADERS).status_code == 200
        return key, rcp, contract, source

    def done(key, rcp, contract, disposition, evidence_id, audit, *, evidence_url=None):
        totals = {"rejected_after_evidence": int(disposition in {"rejected", "hold"}), "saved": int(disposition == "saved"), "correction_stored": int(disposition == "correction_stored")}
        value = receipt(key, hashlib.sha256(canonical(contract)).hexdigest(), [rcp], **totals)
        if evidence_id is not None:
            candidate = value["coverage_lanes"]["kind_krx"]["checked_sources"][0]
            candidate.update({"url": evidence_url or "https://dart.fss.or.kr/no-bound-evidence", "outcome": "candidate", "economic_disposition": disposition, "economic_reason": "evidence candidate", "evidence_id": evidence_id})
            value["coverage_lanes"]["kind_krx"]["candidate_count"] = 1
        return {"status": "done", "count": int(disposition in {"saved", "correction_stored"}), "detail": {"completion_receipt": value, "control_terminal_dispositions": [{"rcp_no": rcp, "disposition": disposition, "evidence_id": evidence_id, **audit}]}}

    for case, disposition in ((1, "rejected"), (2, "hold")):
        key, rcp, contract, _ = fresh_started(case)
        response = client.post(f"/api/internal/scheduler-runs/{key}/finish", headers=HEADERS, json=done(key, rcp, contract, disposition, None, qualifying_audit))
        assert response.status_code == 422
        assert response.json()["detail"] == "research control terminal dispositions are not exact"
        db = connect()
        try:
            row = db.execute("SELECT status FROM giraffe_research_backlog WHERE identity=?", ("dart:" + rcp,)).fetchone()
            assert row["status"] == "pending"
        finally:
            db.close()

    for case, disposition in ((3, "saved"), (4, "correction_stored")):
        key, rcp, contract, _ = fresh_started(case)
        response = client.post(f"/api/internal/scheduler-runs/{key}/finish", headers=HEADERS, json=done(key, rcp, contract, disposition, 999999, qualifying_audit))
        assert response.status_code == 422

    key, rcp, contract, source = fresh_started(5)
    evidence_url = "https://dart.fss.or.kr/fresh-corrected-evidence"
    evidence = client.post("/api/internal/evidence", headers=HEADERS, json={
        "symbol": "406820", "kind": "contract", "title": "corrected supply contract", "summary": "bound amendment evidence",
        "source": "DART", "source_url": evidence_url, "announcement_at": "2026-09-17T06:00:00+09:00",
        "known_at": "2026-09-17T06:30:00+09:00", "research_mode": "scheduled_as_of", "research_run_key": key,
        "snapshot": {"rcp_no": rcp, "dart_source": source}, "dedupe_key": "fresh-corrected-bound-evidence",
    })
    assert evidence.status_code == 200, evidence.text
    evidence_id = evidence.json()["id"]
    stored = client.get(f"/api/internal/evidence/{evidence_id}", headers=HEADERS)
    assert stored.status_code == 200
    assert stored.json()["research_run_key"] == key
    assert stored.json()["snapshot"] == {"rcp_no": rcp, "dart_source": source}
    assert client.post(f"/api/internal/scheduler-runs/{key}/finish", headers=HEADERS, json=done(key, rcp, contract, "correction_stored", evidence_id, qualifying_audit, evidence_url=evidence_url)).status_code == 200
    db = connect()
    try:
        terminal = db.execute("SELECT status,terminal_disposition,terminal_run_key,terminal_evidence_id FROM giraffe_research_backlog WHERE identity=?", ("dart:" + rcp,)).fetchone()
        assert dict(terminal) == {"status": "terminal", "terminal_disposition": "correction_stored", "terminal_run_key": key, "terminal_evidence_id": evidence_id}
    finally:
        db.close()

    key, rcp, contract, _ = fresh_started(6)
    nonqualifying = {**qualifying_audit, "economic_disposition": "below_threshold", "economic_reason": "amendment increment is below half of prior revenue", "economic_facts": {**qualifying_facts, "amended_contract_amount": 999999, "incremental_contract_amount": 499999, "incremental_ratio_percent": 49.9999}}
    assert client.post(f"/api/internal/scheduler-runs/{key}/finish", headers=HEADERS, json=done(key, rcp, contract, "rejected", None, nonqualifying)).status_code == 200


def test_v3_terminal_lookup_is_bounded_canonical_and_exact(monkeypatch, tmp_path):
    import kr_stock_autotrader.db as db_module
    from kr_stock_autotrader.db import connect
    monkeypatch.setattr(db_module, "DATABASE_PATH", str(tmp_path / "terminal-lookup.db"))
    requested, unrequested = "20260919900001", "20260919900002"
    db = connect()
    try:
        for rcp in (requested, unrequested):
            payload = {"rcp_no": rcp, "date": "20260919"}
            db.execute("INSERT INTO giraffe_research_backlog(identity,kind,payload,first_run_key,status,terminal_disposition,terminal_run_key,terminal_evidence_id,created_at,terminal_at) VALUES(?,?,?,?,?,?,?,?,?,?)", ("dart:" + rcp, "dart", json.dumps(payload, sort_keys=True), "research-2026-09-17-0700-kst-r1", "terminal", "hold", "research-2026-09-17-0700-kst-r1", None, "2026-09-19T07:00:00+09:00", "2026-09-19T07:01:00+09:00"))
        db.commit()
    finally:
        db.close()
    client = TestClient(app)
    response = client.post("/api/internal/research-backlog/terminal-items", headers=CONTROL_HEADERS, json={"rcp_nos": [requested]})
    assert response.status_code == 200
    assert response.json()["requested_rcp_nos"] == [requested]
    assert [item["payload"]["rcp_no"] for item in response.json()["items"]] == [requested]
    for bad in ([], [requested] * 2, [unrequested, requested], ["bad"], [f"20260919{i:06d}" for i in range(201)]):
        assert client.post("/api/internal/research-backlog/terminal-items", headers=CONTROL_HEADERS, json={"rcp_nos": bad}).status_code == 422
