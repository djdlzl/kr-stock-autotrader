# Decision-card UI repair summary

Method: TDD + EDD — focused API/UI regression tests; authenticated `/app` and API workflow source checks.

## Blocker

`21st` CLI was unavailable (`command not found`) before this lane. Per task instruction it was not retried or installed. The existing `.21st` context and project semantic primitives were used instead; tokens were moved to blue and density to 6.

## RED → GREEN

- RED: `python -m py_compile kr_stock_autotrader/ui.py && .venv/bin/pytest -q tests/test_decision_card_ui.py`
  - Result: failed: unterminated triple-quoted `APP_HTML` string.
- GREEN: `python -m compileall -q kr_stock_autotrader app.py && .venv/bin/pytest -q`
  - Result: **47 passed** (one upstream Starlette `TestClient` deprecation warning).
- Also passed: `git diff --check`.

## Repair

- Moved the authenticated HTML into `kr_stock_autotrader/decision_card_app.html`, preventing Python quote hazards while preserving the public login shell.
- Added user/date-scoped summary metrics, ISO-date validation, next-future KST scheduler times, and authenticated missing-card evidence endpoint.
- Retained safe owner read model and PASS/non-buy persistence behavior; updated UI tests from obsolete manual-plan assertions to decision-card behavior.
- Decision dashboard uses Korean labels and safe object/list formatting; legacy planning is explanatory read-only.

## Limitations

No browser/Safari runtime review was run (explicitly out of scope). The app uses its existing vanilla server-rendered UI; no frontend build or 21st dependency was added.

Commit SHA: `98da366`
