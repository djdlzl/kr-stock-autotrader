# Giraffe event-scenario successor verification

Synthetic/local-only verification; no production API, DB, deployment, order,
allocation, fill, position, or live receipt side effects were used.

## TDD + EDD evidence

- Pre-change command: `python -m pytest -q tests/test_event_scenarios_hostile.py`
- Initial blocker: `ModuleNotFoundError: No module named 'fastapi'`.
- After installing declared `requirements.txt` into local `.venv`, hostile baseline
  was RED: `5 failed, 2 warnings` (typed LICENSE unsupported, forged values
  accepted, orphan lineage accepted, duplicate observations accepted, and no
  orderbook projection).
- GREEN focused command: `.venv/bin/python -m pytest -q tests/test_event_scenarios.py tests/test_kis_readonly_dryrun.py tests/test_decision_card_ui.py`
- GREEN result: `59 passed, 2 warnings`.

## Final checks

- `.venv/bin/python -m pytest -q` → `175 passed, 2 warnings`.
- `.venv/bin/python -m compileall -q kr_stock_autotrader` → pass.
- `git diff --check` → pass.
- `docker compose -f compose.prod.yml config` → blocked without required env;
  rerun with synthetic required values → pass.

The compose check rendered configuration only. No network or production calls
were made.
