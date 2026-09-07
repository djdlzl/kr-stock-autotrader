# EDD Results

## Actual Journey

Synthetic contract flow executed against a throwaway SQLite database through FastAPI and authenticated internal endpoints.

### Commands

```text
SESSION_SECRET='test-session-secret-that-is-at-least-thirty-two-bytes-long' SIGNUP_ENABLED=true INTERNAL_API_KEY=hybrid-key /Users/jaewoo/_workspace/giraffe-0905-market-context/.venv/bin/python - <<'PY'
...
PY
```

### Normal Case

- Scenario set created successfully.
- 09:05 market-context evaluation returned `VERIFIED`.
- Outcome ledger accepted 3 append-only rows.
- Calibration snapshot returned:
  - `eligible=true`
  - `IS count=1`
  - `OOS count=2`
  - `overall count=3`
  - `GOOD count=2`
  - `GOOD probability=1.0`
  - `GOOD lower_bound=0.34237195`
  - `holdout_reuse_count=1`
- Second-stage evaluation returned:
  - `final_state=GOOD`
  - `recommendation=BUY_REVIEW`
  - `recommendation_only=true`
  - `policy_identity=giraffe-hybrid-good-base-bad-v1`
  - `policy_version=1`
  - `policy_hash=2d0fc94d20f439fd5deccdaf1654169fff84f2524a4b1dedb0daa06aab159845`
- Readback from `/api/cards/{card_id}` exposed the latest immutable hybrid decision.

### Insufficient Evidence

- Covered by `tests/test_hybrid_decision_engine.py`.
- Result: `HOLD` + `HOLD_INSUFFICIENT_EVIDENCE`.

### Invalidated / BAD

- After explicit invalidation of the frozen card, second-stage evaluation returned:
  - `final_state=BAD`
  - `recommendation=REDUCE_REVIEW`
- This confirmed invalidation dominates and blocks BUY.

### Hostile Inputs

- Covered by contract tests for:
  - future / post-cutoff outcome rows
  - forged derived fields
  - idempotency collision
- Result: `422` or `409`, no side effects.

### Prior-Only Control

- Covered by `tests/test_hybrid_decision_engine.py`.
- Result: structural prior alone did not promote BUY without eligible untouched OOS evidence.

### Side-Effect Check

- Forbidden tables remained unchanged:
  - `order_plans=0`
  - `order_fills=0`
  - `positions=0`
  - `order_events=0`
  - `live_dry_run_receipts=0`
