# Hybrid Decision Engine Summary

## Method

Implemented the hybrid second stage as a small, server-owned FastAPI + SQLite slice and verified it with TDD/EDD-style contract tests.

Delivered behavior:
- one realized outcome per independent `(scenario_set_id, policy_id)` case
- cohort-based calibration over other independent scenario sets with the same server-derived cohort key
- frozen IS/OOS plan identity with snapshot creation bound to that plan
- cost-adjusted first-touch realization using Decimal arithmetic
- meaningful HOLD and structural-prior control arithmetic
- 09:05 market-context gating with top-book depth, spread, imbalance, and baseline-volume context
- controlled 404/422/409 responses for missing scenario, empty bars, bad timestamps, idempotency collisions, and drift
- hard `recommendation_only=True`
- no order, allocation, fill, live receipt, or broker side effects

## Files

- `/Users/jaewoo/_workspace/giraffe-hybrid-decision-engine/kr_stock_autotrader/hybrid_recommendations.py`
- `/Users/jaewoo/_workspace/giraffe-hybrid-decision-engine/kr_stock_autotrader/db.py`
- `/Users/jaewoo/_workspace/giraffe-hybrid-decision-engine/kr_stock_autotrader/api.py`
- `/Users/jaewoo/_workspace/giraffe-hybrid-decision-engine/tests/test_hybrid_decision_engine.py`
- `/Users/jaewoo/_workspace/giraffe-hybrid-decision-engine/.hermes/runs/giraffe-hybrid-decision-engine/artifacts/edd-results.md`

## Verification

Command results from this pass:

```text
/Users/jaewoo/_workspace/giraffe-0905-market-context/.venv/bin/python -m pytest -q tests/test_hybrid_decision_engine.py
10 passed, 2 warnings in 7.98s

/Users/jaewoo/_workspace/giraffe-0905-market-context/.venv/bin/python -m pytest -q
292 passed, 2 warnings in 44.36s

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
