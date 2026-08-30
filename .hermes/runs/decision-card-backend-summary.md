# Decision-card backend summary

Method: TDD + EDD — focused invariant tests plus FastAPI/SQLite HTTP-boundary checks.

## RED → GREEN
- Replaced the quarantined timeout diff rather than repairing its unsafe snapshot/sell scheduler model.
- Focused `tests/test_decision_cards.py` now verifies deterministic future/zero-denominator rejection, prompt file hash, evidence dedupe, immutable card save, approval snapshot, buy-before-distinct-tick sell, sell remaining cap/idempotency, and fail-closed internal writes.
- GREEN: `.venv/bin/pytest -q tests/test_decision_cards.py` → `3 passed`; full `.venv/bin/pytest -q` → `29 passed` (one upstream TestClient deprecation warning); `compileall` and `git diff --check` passed.

## Schema/API/CLI
- Additive/idempotent SQLite tables cover evidence, deterministic filters, card versions, user decisions, frozen order plans, evals/events/fills/positions/exit lineage, scheduler runs, and audit logs.
- Internal writer endpoints require constant-time `X-Internal-API-Key` against `INTERNAL_API_KEY`; authenticated user card reads/approve-hold-reject remain separate.
- Prompt is `prompts/decision-card-v1.md`; hash is computed from file bytes. Filter output documents signs/units/denominator and fails closed.
- `python -m kr_stock_autotrader.cli` is an HTTP-only client using `GIRAFFE_URL`/`INTERNAL_API_KEY`; no secret output, no daemon. Scheduler API only records start/finish, while the order-plan evaluator is tick driven.

## Limitations
- This backend candidate deliberately does not implement the detailed decision-card UI (next lane), operational deployment, or Hermes cron registration.
- No LLM call is made by the server: `cards/generate` returns the versioned prompt/input contract and `cards/results` stores externally generated structured output.
- `LIVE_TRADING` remains hard-coded false; there is no live broker adapter.

Candidate SHA: `095a043` (`feat: add immutable paper decision-card backend`).
