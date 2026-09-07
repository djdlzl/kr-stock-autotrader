# EDD Results

## Actual Journey

This pass verified the hybrid slice through the automated contract suite against a throwaway SQLite database. The key probe was a normal persisted market-context run, followed by evaluation through the canonical readback path, with one positive and one negative BUY gate check.

### Commands

```text
uv run --with fastapi --with httpx --with pytest --with uvicorn pytest -q tests/test_hybrid_decision_engine.py -k 'topbook_positive or cost_adjusted_entry_outside_frozen_good_band or min_top_of_book_qty_blocks_buy_review'
uv run --with fastapi --with httpx --with pytest --with uvicorn pytest -q tests/test_hybrid_decision_engine.py tests/test_intraday_market_context.py tests/test_event_scenarios.py
uv run --with fastapi --with httpx --with pytest --with uvicorn pytest -q
uv run --with fastapi --with httpx --with pytest --with uvicorn python -m compileall -q .
git diff --check
```

### Results

- targeted hybrid tests: `2 passed, 10 deselected, 2 warnings`
- affected hybrid/intraday/event suites: `20 passed, 2 warnings`
- full suite: `294 passed, 2 warnings`
- compileall: passed
- diff check: passed

### What The Suite Exercises

- one realized outcome per independent scenario-set case
- calibration over other independent cohort-matched scenario sets
- target-case exclusion from calibration
- preregistered plan freeze before OOS evidence is accepted
- minimum sample gates of `30` overall and `20` OOS
- cost-adjusted first-touch outcome labels
- canonical exposure of server-observed top-book depth on persisted market-context readback
- BUY review only when `structural_good_compatibility.status == GOOD_COMPATIBLE`
- HOLD control arithmetic with the same denominator as OOS
- structural-prior-only control arithmetic over the same OOS case IDs
- top-book depth, spread, imbalance, and baseline-volume market-context gates
- controlled `404`, `409`, and `422` responses for the hostile inputs in the prompt
- `recommendation_only` hard rejection when forced false
- no writes to order, allocation, fill, or live-receipt tables

### Evidence Notes

- The test suite builds independent historical scenario/card cases in a throwaway database.
- One test confirms that 30 rows from the same case do not satisfy the cohort sample requirement.
- One test confirms the target case is excluded from calibration.
- One test confirms calibration snapshot creation refuses post-hoc window replacement.
- One test confirms a normal persisted market-context run exposes top-book quantities and can reach `BUY_REVIEW`.
- One test confirms a cost-adjusted entry outside the frozen GOOD band blocks `BUY_REVIEW`.
- One test confirms low top-of-book depth blocks the BUY review path.
- One test confirms invalidation dominates the final decision path.

### Claim Limits

- This is synthetic contract evidence, not live trading evidence.
- It does not claim execution readiness, broker integration readiness, or production alpha.
- It does not create orders, allocations, fills, or live dry-run receipts.
- It only claims `BUY_REVIEW` when both the canonical market-context readback and the structural GOOD compatibility gate succeed.
