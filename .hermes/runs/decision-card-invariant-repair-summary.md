# Decision-card invariant repair

Method: TDD + EDD — focused invariant boundaries plus FastAPI/SQLite scheduler readback.

## RED evidence
- Baseline repair candidate: `b717744`.
- Command: `.venv/bin/pytest -q tests/test_decision_card_invariants.py`
- Result before repair: `4 failed, 1 passed`.
- Failures demonstrated unsafe raw-input/relative-return handling, FAIL-filter card creation, empty/reversed approval window acceptance, and stale quote purchase.

## Repair
- Enforced raw stock/benchmark/sector returns and current/baseline volume; calculates signed relative performance and volume ratio fail-closed.
- Enforced active same-evidence PASS filter, append-only card lineage invalidation, frozen valid KST approval windows, positive limits and exit rules.
- Evaluator uses quote `known_at` against server time, respects amount/quantity/market/window gates, supports capped partial buys, and permits exits only via frozen rules.
- Added immutable scheduler semantics, authenticated plan-edit invalidation, HTTP-only CLI coverage, and expanded prompt contract.

## GREEN evidence
- `.venv/bin/pytest -q tests/test_decision_card_invariants.py` → `6 passed` (one upstream TestClient deprecation warning).
- `.venv/bin/pytest -q` → `35 passed` (one upstream TestClient deprecation warning).
- `.venv/bin/python -m compileall -q kr_stock_autotrader app.py` → success.
- `git diff --check` → success.

Repair commit SHA: `9845161d41441b1106dd187def8d189463de3acd` (`fix: harden decision card invariants`).
