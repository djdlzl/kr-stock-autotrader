# Giraffe date lifecycle implementer summary

Method: TDD + EDD — focused KST business-date/current-card/user-isolation contracts; authenticated TestClient/SQLite multi-date workflow.

## RED → GREEN

- RED: `.venv/bin/pytest -q tests/test_date_lifecycle.py` → `2 failed` before API/UI implementation (missing KST lifecycle UI and date grouping).
- GREEN: `.venv/bin/pytest -q tests/test_date_lifecycle.py` → `2 passed, 1 warning`.
- Full regression: `.venv/bin/pytest -q` → `62 passed, 1 warning` (Starlette TestClient deprecation warning).
- Compile: `.venv/bin/python -m compileall -q kr_stock_autotrader app.py` → passed.
- Whitespace: `git diff --check` → passed.

## Independent-review rework (B1–B3, N1)

- Method: TDD + EDD — timezone-pure UI calendar helper and hostile/TestClient API + SQLite lifecycle fixtures.
- RED: `.venv/bin/pytest -q tests/test_date_lifecycle.py` → `4 failed, 2 passed` before repairs (timezone helper absent, raw evidence fields exposed, invalidated newer card suppressed active prior version, and historical summary showed newer-day scheduler status).
- GREEN: `.venv/bin/pytest -q tests/test_date_lifecycle.py` → `6 passed, 1 warning`; the Node regression runs the browser helper under `Pacific/Kiritimati`, `Asia/Seoul`, and `America/Los_Angeles` and returns `2026-08-31` in each.
- Full regression: `.venv/bin/pytest -q` → `66 passed, 1 warning` (the pre-existing Starlette TestClient deprecation warning only).
- Compile: `python -m compileall -q app.py kr_stock_autotrader` → passed; `git diff --check` → passed.
- B1: Previous-day navigation now applies UTC calendar-component arithmetic to `YYYY-MM-DD`, never mixing a KST-offset parse with local `setDate`/ISO serialization.
- B2: `/api/cards/missing` projects a safe evidence read model only; its hostile sentinel test confirms no snapshot/raw/audit/dedupe/creator/internal fields leak and anonymous access remains 401.
- B3: The current-card anti-join now ignores newer invalidated versions, so the latest active card and its current-user decision are counted while history remains append-only.
- N1: Scheduler status in a selected historical overview is filtered by the selected run-key business day (with selected KST started-day fallback), never a global latest run.

## Changed files

- `kr_stock_autotrader/api.py`
  - Strict ISO `YYYY-MM-DD` date validation with today-KST default.
  - Server-side date filtering for summary, cards, and missing cards.
  - Summary joins cards/filters to evidence `known_at` business day and counts latest active card per lineage; user decisions are scoped to the authenticated user and active date card.
  - Adds PASS/FAIL, error, invalidated, and decision-pending metrics.
- `kr_stock_autotrader/decision_cards.py`
  - Adds server-side evidence-day query filtering while retaining append-only card history in detail/list output.
- `kr_stock_autotrader/decision_card_app.html`
  - Adds prominent KST date picker, previous/today navigation, server reload of all three API views, Korean empty/error/session messaging, lifecycle metadata labels, fresh quote/tick and frozen-condition notice, and existing mobile overflow protections.
- `tests/test_date_lifecycle.py`
  - New focused RED/GREEN + TestClient EDD: two business days, delayed generated card, invalidated evidence, latest active card counts, missing exclusion, two-user isolation, strict invalid dates, empty date, UI source assertions.
- `tests/test_decision_card_packaging_edd.py`
  - Makes the pre-existing EDD list read explicitly date-scoped, matching the new default-today contract.

## Side effects / limitations

- Local-only files in the dedicated worktree; no push, merge, deployment, runtime/browser verification, scheduler/cron change, credentials, orders, allocation, or live trading side effect.
- `LIVE_TRADING` remained covered by the existing EDD as `False`; the scheduler remains read-only with respect to paper orders.
- UI verification is TestClient source/runtime assertion only; no real browser or production claim.
