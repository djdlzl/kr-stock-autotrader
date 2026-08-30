# Decision-card residual backend closure

Method: TDD — focused contract regressions plus full API/domain suite.

Base: `586b78be135806017543691dfcd152a8a04532be`

## Implemented

- Approval hashes are generation-scoped. Immediate approval is idempotent only for an active executable plan; an invalidated post-edit plan remains immutable and reapproval creates a fresh plan/hash/generation.
- Draft overrides are strict allowlist-only and validate integer quantity, order type, take-profit rules, and KST timing.
- Paper exits now require fresh quotes and KST market-open status; entry window/price/expiry constraints remain entry-only.
- FAIL-filter cards may be retained only for non-buy verdicts and cannot create drafts or order plans.
- Evidence lifecycle defaults to `new`, saves transition to `card_generated`, errors are recordable without invalidation, and status filtering remains available.
- Added internal pending-card (`missing=true`) and scheduler latest-by-kind/date readback. User card responses include current user's decision, newest plan, and draft state.

## Verification

- RED: `tests/test_decision_card_residual_backend.py` initially failed on four unimplemented contract gaps.
- GREEN: `pytest -q tests/test_decision_card_residual_backend.py tests/test_decision_card_lifecycle_repair.py` — 10 passed.
- Full: `pytest -q` — 45 passed (one pre-existing TestClient deprecation warning).
- `python -m compileall -q kr_stock_autotrader tests` — passed.
- `git diff --check` — passed.

Commit SHA: recorded by the final Git `HEAD` verification (the report is committed with this change).
