# Hybrid Decision Engine Summary

## Method

Implemented the hybrid second stage as a small, server-owned FastAPI + SQLite slice and verified it with TDD/EDD-style contract tests.

Delivered behavior:
- one realized outcome per independent `(scenario_set_id, policy_id)` case
- cohort-based calibration over other independent scenario sets with the same server-derived cohort key
- frozen IS/OOS plan identity with snapshot creation bound to that plan
- cost-adjusted first-touch realization using Decimal arithmetic
- canonical market-context readback that surfaces server-observed top-of-book quantities from persisted orderbook observations
- final BUY review gating that requires `structural_good_compatibility.status == GOOD_COMPATIBLE`
- meaningful HOLD and structural-prior control arithmetic
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
uv run --with fastapi --with httpx --with pytest --with uvicorn pytest -q tests/test_hybrid_decision_engine.py -k 'topbook_positive or cost_adjusted_entry_outside_frozen_good_band or min_top_of_book_qty_blocks_buy_review'
2 passed, 10 deselected, 2 warnings in 3.71s

uv run --with fastapi --with httpx --with pytest --with uvicorn pytest -q tests/test_hybrid_decision_engine.py tests/test_intraday_market_context.py tests/test_event_scenarios.py
20 passed, 2 warnings in 11.39s

uv run --with fastapi --with httpx --with pytest --with uvicorn pytest -q
294 passed, 2 warnings in 49.59s

uv run --with fastapi --with httpx --with pytest --with uvicorn python -m compileall -q .
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
- BUY review is only claimed when the persisted market-context readback exposes server-observed top-of-book quantities and the final decision reports `structural_good_compatibility.status == GOOD_COMPATIBLE`.
