# KIS DB token cache — implementation summary

## Verdict
Implemented and committed a DB-backed, fail-closed KIS OAuth cache with SQLite transaction coordination. No order, allocation, or live-trading behavior was changed.

Method: TDD + EDD — focused cache invariants; SQLite-backed fresh-client and concurrent-client runtime evaluation.

## Files
- `kr_stock_autotrader/db.py`
- `kr_stock_autotrader/kis_readonly.py`
- `kr_stock_autotrader/market_data.py`
- `tests/test_kis_readonly_dryrun.py`
- `tests/test_market_data_pipeline.py`

## Schema behavior
`kis_oauth_token_cache` stores one opaque OAuth token and its exact ISO expiry per SHA-256 digest of the application key. The app key itself and the app secret are not stored in this table.

A refresh takes `BEGIN IMMEDIATE`, rechecks the row under that lock, then atomically upserts the issued token and computed expiry before commit. A valid durable row is reused by a fresh client. A broker-rejected token is conditionally removed only when it still matches the rejected value. SQLite, malformed-cache, and cache-write failures raise the specific internal `KIS OAuth cache unavailable` error and do not fall back to OAuth issuance.

Public premarket market-data output classifies this as `kis_oauth_cache_unavailable`, rather than `daily_bars_unavailable_or_invalid`; no token is returned in that output.

## Evidence
### RED (before production edit)
` .venv/bin/pytest -q tests/test_kis_readonly_dryrun.py -k 'db_token_cache'`

Result: **3 failed**. Fresh clients each issued OAuth; concurrent clients issued four OAuth requests; the durable cache table did not exist.

### GREEN
- `.venv/bin/pytest -q tests/test_kis_readonly_dryrun.py -k 'db_token_cache'` → **3 passed**
- `.venv/bin/pytest -q tests/test_market_data_pipeline.py -k 'oauth_cache_failure'` → **1 passed**
- `.venv/bin/pytest -q tests/test_kis_readonly_dryrun.py tests/test_market_data_pipeline.py` → **87 passed** (2 dependency deprecation warnings)
- `python -m py_compile kr_stock_autotrader/db.py kr_stock_autotrader/kis_readonly.py kr_stock_autotrader/market_data.py` → passed
- `git diff --check` → passed before commit

Full suite attempt: `.venv/bin/pytest -q` → **306 passed, 1 failed**. The unrelated existing test `tests/test_hybrid_decision_engine.py::test_hybrid_calibration_rejects_posthoc_windows_and_target_case_reuse` failed with `outcome known_at outside observation window`; it does not exercise the KIS cache files.

## Commit
Implementation commit: `75dabd4c4ae0dff2f6fdf9aceebf6213046f8499` (`fix(kis): coordinate OAuth tokens through SQLite`).

## Residual risks
- SQLite coordinates writers within its configured five-second busy timeout; prolonged database lock contention fails closed rather than issuing an extra OAuth token.
- The full-suite hybrid calibration failure remains outside this task's scope.
