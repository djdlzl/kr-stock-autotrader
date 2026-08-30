# Decision-card paper lifecycle repair

Method: TDD + EDD — focused lifecycle invariants plus SQLite/FastAPI readback.

## RED
` .venv/bin/pytest -q tests/test_decision_card_lifecycle_repair.py ` initially failed collection because the new `edit_order_plan` contract did not exist. The added tests cover actual notional, weighted average price, exit/entry-gate separation, tranche idempotency, full manual close, pre-approval draft freezing, and post-approval reapproval.

## Repair
- `order_plans.bought_amount` is additive and constrains every buy; positions use a weighted average.
- Entry gates and frozen exit rules are independent. Existing paper positions can exit after entry expiry/invalidation; split take-profit stages are recorded in `exit_lineage` and sell only their frozen tranche.
- Added draft snapshots, authenticated pre/post approval edits, plan detail, and explicit authenticated manual close.
- Legacy `/api/plans` now creates `manual_only` non-executable records; `/api/ticks` cannot fill them.
- Updated old legacy tests to assert the universal zero-unapproved-fill invariant.

## GREEN / verification
```sh
.venv/bin/pytest -q tests/test_decision_card_lifecycle_repair.py
# 5 passed (one upstream TestClient deprecation warning)
.venv/bin/pytest -q
# 40 passed (one upstream TestClient deprecation warning)
.venv/bin/python -m compileall -q kr_stock_autotrader app.py
# success
git diff --check
# success
```

Repair code/test commit SHA: `7f61ff4b7eef7aebc3081d99d7cd0d2f59cc9e53` (`fix: enforce decision card paper lifecycle`).
LIVE_TRADING remains false; no live adapter was added.
