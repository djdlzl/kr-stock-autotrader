# Giraffe default paper-budget implementation + verifier rework

Method: TDD + EDD — RED/GREEN regression tests for authenticated safe-card/UI parity, immutable partial draft merges, and SQLite storage typing; TestClient + Node UI helper workflow.

## Implemented
- Safe `user_state` exposes only the authenticated user's `default_paper_amount`; a fresh PASS + buy-review form selects it over card-authored `max_amount`, while active draft and latest plan snapshots retain precedence.
- Direct approval freezes exactly the server/UI default snapshot when no draft or plan exists. Non-buy cards continue to expose no plan editor and cannot be approved.
- `edit_draft` merges partial updates over active draft → latest plan → current default. `edit_order_plan` merges repeated updates over its own active draft and rejects a draft from a different plan lineage.
- Reapproval retires the prior approved draft snapshot before freezing the next snapshot, maintaining the per-card/user lifecycle constraint.
- `user_settings.default_paper_amount` now checks SQLite INTEGER storage class and bounds, rejecting direct SQL fractional and nonnumeric values while retaining strict API validation/auth/CSRF behavior.

## Evidence
- RED: newly focused tests initially failed for missing safe default, draft merge preservation, storage-type checks, and UI selection.
- GREEN focused: `uv run --with-requirements requirements.txt pytest -q tests/test_default_paper_budget.py tests/test_decision_card_ui.py tests/test_decision_card_lifecycle_repair.py` → `15 passed`.
- GREEN full: `uv run --with-requirements requirements.txt pytest -q` → `76 passed` (one existing Starlette TestClient deprecation warning).
- Compile: `uv run --with-requirements requirements.txt python -m compileall -q .` passed.
- Diff whitespace: `git diff --check` passed.

No live trading, order execution, allocation, deploy, scheduler, credentials, push, or merge actions occurred.
