# Decision-card hostile fix 2

- **Code commit SHA:** `3260800e3ebc63b5e79fc0b43126e0cd65770483`
- **Method:** TDD — focused API lifecycle, known-at ordering, and strict-schema boundary regressions.

## Narrow fixes

1. Internal `PATCH /api/internal/evidence/{id}` no longer returns before audit/lineage work. `status=invalidated` now increments evidence version and atomically revokes every entry-capable plan; open positions become `review_required` for frozen exits. Mixed status/content updates validate status and revoke lineage when mutable content changes.
2. Filter inputs now require `evidence.known_at <= market_data_known_at <= filter.known_at <= filter.as_of` in KST. The direct save path rejects market data newer than `filter.known_at`.
3. Decision-card trading scalars reject boolean/non-numeric coercion. Quantities use `StrictInt`; price/amount/stop-loss/take-profit price/confidence accept normal finite `int`/`float` values only.

## RED → GREEN evidence

- RED: `.venv/bin/python -m pytest -q tests/test_verifier_rework.py -k 'filter_rejects_market_data_newer or schema_rejects_boolean or internal_status_invalidated'` → **3 failed** before implementation.
- GREEN (same focused command) → **3 passed, 6 deselected**.
- Full suite: `.venv/bin/python -m pytest -q` → **58 passed, 1 upstream Starlette/TestClient deprecation warning**.
- Compile: `.venv/bin/python -m compileall -q kr_stock_autotrader` → **passed**.
- Node UI probe: `node /tmp/giraffe-verifier2-7923/ui_runtime_probe.js` → `NODE_PARSE_RUNTIME 2026-08-31T09:00 2026-08-31T15:00 true true 2026-08-31T09:00:00+09:00`.
- Diff whitespace: `git diff --check` → **passed**.
