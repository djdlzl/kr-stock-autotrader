# Hybrid Decision Engine Summary

## Method

Implemented the smallest vertical slice for a recommendation-only hybrid second stage using deterministic FastAPI + SQLite patterns.

Scope delivered:
- versioned server-owned policy spec with frozen identity, version, hash, horizon, cost, gate, and threshold fields
- append-only outcome ledger with server-side first-touch realization and conservative same-bar ambiguity handling
- immutable calibration snapshots with chronological IS/OOS windows, counts, lower bound, holdout reuse, controls, and eligibility reasons
- immutable second-stage evaluation tied to one frozen scenario set, one verified 09:05 market-context result, one calibration snapshot, and one policy hash
- authenticated readback of the latest immutable hybrid decision from card detail
- no order, allocation, fill, live receipt, or broker side effects

## Files

- `/Users/jaewoo/_workspace/giraffe-hybrid-decision-engine/kr_stock_autotrader/hybrid_recommendations.py`
- `/Users/jaewoo/_workspace/giraffe-hybrid-decision-engine/kr_stock_autotrader/db.py`
- `/Users/jaewoo/_workspace/giraffe-hybrid-decision-engine/kr_stock_autotrader/api.py`
- `/Users/jaewoo/_workspace/giraffe-hybrid-decision-engine/kr_stock_autotrader/decision_cards.py`
- `/Users/jaewoo/_workspace/giraffe-hybrid-decision-engine/tests/test_hybrid_decision_engine.py`
- `/Users/jaewoo/_workspace/giraffe-hybrid-decision-engine/.hermes/runs/giraffe-hybrid-decision-engine/artifacts/red.txt`
- `/Users/jaewoo/_workspace/giraffe-hybrid-decision-engine/.hermes/runs/giraffe-hybrid-decision-engine/artifacts/evaluation-card.md`
- `/Users/jaewoo/_workspace/giraffe-hybrid-decision-engine/.hermes/runs/giraffe-hybrid-decision-engine/artifacts/edd-results.md`

## RED / GREEN

RED evidence:
- Command: `/Users/jaewoo/_workspace/giraffe-0905-market-context/.venv/bin/python -m pytest -q tests/test_hybrid_decision_engine.py`
- Result: `4 failed, 0 passed`
- Cause: missing hybrid outcome, calibration, evaluation, and card readback contracts.

GREEN evidence:
- Command: `/Users/jaewoo/_workspace/giraffe-0905-market-context/.venv/bin/python -m pytest -q tests/test_hybrid_decision_engine.py`
- Result: `4 passed`

## Verification

Commands run:

```text
/Users/jaewoo/_workspace/giraffe-0905-market-context/.venv/bin/python -m pytest -q tests/test_hybrid_decision_engine.py
/Users/jaewoo/_workspace/giraffe-0905-market-context/.venv/bin/python -m pytest -q
/Users/jaewoo/_workspace/giraffe-0905-market-context/.venv/bin/python -m compileall -q .
git diff --check
```

Results:
- targeted hybrid tests: `4 passed`
- full suite: `286 passed, 2 warnings`
- compileall: passed
- diff check: passed

Warnings:
- existing Starlette/httpx deprecation warning from the environment

## EDD

Executed an authenticated FastAPI -> SQLite -> readback journey in a throwaway database.

Observed normal-case outputs:
- scenario set created successfully
- market context: `VERIFIED`
- calibration: `eligible=true`
- calibration IS window: `2026-09-08T00:00:00+09:00` to `2026-09-09T00:00:00+09:00`, count `1`
- calibration OOS window: `2026-09-09T00:00:00+09:00` to `2026-09-11T00:00:00+09:00`, count `2`
- overall samples: `3`
- GOOD count: `2`
- GOOD probability: `1.0`
- GOOD lower bound: `0.34237195`
- holdout reuse count: `1`
- second-stage result: `GOOD` + `BUY_REVIEW`
- card readback included the latest immutable hybrid decision

Observed invalidation case:
- second-stage result changed to `BAD` + `REDUCE_REVIEW`
- invalidation dominated the BUY path

Observed side-effect check:
- `order_plans=0`
- `order_fills=0`
- `positions=0`
- `order_events=0`
- `live_dry_run_receipts=0`

## Quant Sanity Report

- Units and signs: calibration probabilities are unitless; costs are tracked in bps and converted to KRW only from the frozen market price; spread is a percent and top-of-book imbalance is bounded to a symmetric ratio.
- Denominators: OOS denominator was `2` in the normal path; probability and lower bound were derived only from completed OOS rows.
- Timestamps / known-at boundaries: outcome rows required KST timestamps; scenario freeze, observation cutoff, and market-context known_at values were enforced as immutable boundaries.
- No-lookahead: post-cutoff rows were rejected; calibration selected only rows with `observation_cutoff_at <= cutoff_at`.
- Duplicate / missing rows: duplicate exchange timestamps and malformed OHLC rows were rejected; tests covered duplicate idempotency collisions and missing/forged fields.
- Cost arithmetic: transaction cost was `8 bps`; round-trip cost was `16 bps`; evaluation exposed both bps and KRW translation from the frozen price.
- Concentration / outlier dependence: calibration reported the dominant realized label and its share; the synthetic normal path concentrated entirely in GOOD.
- Probability invariants: final probability came from the immutable calibration snapshot, not the client payload; derived probability and lower-bound forging was rejected.
- Neutral controls: the calibration snapshot recorded HOLD and structural-prior-only control counts/results.
- Exact IS/OOS windows/counts: IS `1`, OOS `2`, overall `3` in the normal path.
- Parameter selection scope: exactly one frozen scenario set, one policy hash, one market-context run, and one calibration snapshot were linked into the evaluation.
- Holdout reuse: holdout reuse count was tracked and repeated holdout keys were marked ineligible.
- Failure gates: insufficient evidence returned `HOLD` + `HOLD_INSUFFICIENT_EVIDENCE`; invalidation dominated and prevented BUY; market-context verification and price guardrail were both required for BUY.

## Limitations

- This is recommendation-only and does not build ML infrastructure or trained alpha.
- Synthetic fixtures are contract tests, not performance evidence or live OOS evidence.
- The hybrid slice is intentionally narrow and does not create or mutate any trading execution tables.

## Side-Effect Counts

- `order_plans=0`
- `order_fills=0`
- `positions=0`
- `order_events=0`
- `live_dry_run_receipts=0`
