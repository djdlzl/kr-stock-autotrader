# EDD Results

## Actual Journey

This pass verified the hybrid slice through the automated contract suite against a throwaway SQLite database. I did not run a separate ad hoc end-to-end script in this pass.

### Commands

```text
/Users/jaewoo/_workspace/giraffe-0905-market-context/.venv/bin/python -m pytest -q tests/test_hybrid_decision_engine.py
/Users/jaewoo/_workspace/giraffe-0905-market-context/.venv/bin/python -m pytest -q
/Users/jaewoo/_workspace/giraffe-0905-market-context/.venv/bin/python -m compileall -q .
git diff --check
```

### Results

- targeted hybrid tests: `10 passed, 2 warnings`
- full suite: `292 passed, 2 warnings`
- compileall: passed
- diff check: passed

### What The Suite Exercises

- one realized outcome per independent scenario-set case
- calibration over other independent cohort-matched scenario sets
- target-case exclusion from calibration
- preregistered plan freeze before OOS evidence is accepted
- minimum sample gates of `30` overall and `20` OOS
- cost-adjusted first-touch outcome labels
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
- One test confirms a gross GOOD case can become BASE/BAD after transaction costs are applied.
- One test confirms low top-of-book depth blocks the BUY review path.
- One test confirms invalidation dominates the final decision path.

### Claim Limits

- This is synthetic contract evidence, not live trading evidence.
- It does not claim execution readiness, broker integration readiness, or production alpha.
- It does not create orders, allocations, fills, or live dry-run receipts.
