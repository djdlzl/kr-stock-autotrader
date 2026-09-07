# Hybrid Decision Engine Summary

## Method

Implemented the hybrid second stage as a small, server-owned FastAPI + SQLite slice and verified it with TDD/EDD-style contract tests.

Rework history:
- rework-v3: made the calibration plan immutable, enforced one global holdout use per `(policy_id, cohort_key, holdout_key)`, and replaced the neutral controls with real per-case economic controls over the full OOS denominator.
- rework-v4: bound every historical calibration row to an immutable pre-outcome decision context via persisted `market_context_run_id`, stored a canonical entry-gate snapshot/hash, computed `policy_qualified` server-side, and calibrated only on policy-qualified historical outcomes.

Delivered behavior:
- one realized outcome per independent `(scenario_set_id, policy_id)` case
- cohort-based calibration over other independent scenario sets with the same server-derived cohort key
- immutable frozen IS/OOS plan identity with snapshot creation bound to that plan
- global holdout-once enforcement across plans for the same `(policy_id, cohort_key, holdout_key)`
- cost-adjusted first-touch realization using Decimal arithmetic
- canonical market-context readback that surfaces server-observed top-of-book quantities from persisted orderbook observations
- final BUY review gating that requires `structural_good_compatibility.status == GOOD_COMPATIBLE`
- policy-qualified historical calibration with auditable selected/excluded IDs, excluded counts, and excluded reasons
- meaningful HOLD, structural-prior-only, and hybrid-candidate control arithmetic over the same OOS denominator
- 09:05 market-context gating with top-book depth, spread, imbalance, and baseline-volume context
- controlled 404/422/409 responses for missing scenario, empty bars, bad timestamps, idempotency collisions, and drift
- hard `recommendation_only=True`
- no order, allocation, fill, live receipt, or broker side effects

## Files

- `/Users/jaewoo/_workspace/giraffe-hybrid-decision-engine/kr_stock_autotrader/hybrid_recommendations.py`
- `/Users/jaewoo/_workspace/giraffe-hybrid-decision-engine/kr_stock_autotrader/intraday_market_context.py`
- `/Users/jaewoo/_workspace/giraffe-hybrid-decision-engine/tests/test_hybrid_decision_engine.py`
- `/Users/jaewoo/_workspace/giraffe-hybrid-decision-engine/.hermes/runs/giraffe-hybrid-decision-engine/artifacts/edd-results.md`

## Verification

Command results from this pass:

```text
 /Users/jaewoo/_workspace/giraffe-0905-market-context/.venv/bin/python -m pytest -q tests/test_hybrid_decision_engine.py tests/test_intraday_market_context.py tests/test_event_scenarios.py
29 passed, 2 warnings in 23.14s

/Users/jaewoo/_workspace/giraffe-0905-market-context/.venv/bin/python -m pytest -q
303 passed, 2 warnings in 59.46s

/Users/jaewoo/_workspace/giraffe-0905-market-context/.venv/bin/python -m compileall -q .
passed

git diff --check
passed
```

Warnings:
- existing `starlette.testclient` / `anyio` deprecation warnings from the environment

## Limits

- This pass only verifies the throwaway synthetic contract flow.
- It does not claim live trading performance, order execution readiness, or production alpha.
- Synthetic fixtures are contract evidence only, not performance evidence.
- The historical calibration cohort is policy-qualified only; unqualified historical outcomes remain excluded and are surfaced only as auditable selection metadata.
- BUY review is only claimed when the persisted market-context readback exposes server-observed top-of-book quantities and the final decision reports `structural_good_compatibility.status == GOOD_COMPATIBLE`.
