# Giraffe fill filters implementer summary

Method: TDD + EDD — focused user-scoped fill-summary invariants; authenticated TestClient/API/UI-shell and Node dropdown-predicate workflow.

## RED → GREEN evidence

- RED: `python -m pytest tests/test_date_lifecycle.py::test_card_fill_summary_is_user_scoped_across_all_plan_generations -q` initially failed with `KeyError: 'fill_summary'`.
- GREEN: the same focused test passes after adding the safe `user_fill_summary` read model.
- The focused test covers no fills, buy-only, partial sell, complete sell, ISO KST timestamps, two plan generations for one user, and another user's independent fills/no leakage.
- UI source contract test verifies list/detail timestamp labels, `-` fallbacks, green `purchased` class only from `first_buy_at`, four labeled dropdowns/options, reset, date-load preservation, mobile constraints, and no raw JSON rendering.
- Node predicate fixture verifies AND filtering: `[3,1,1,0]` for all, matching card, matching missing row, and conflicting selections.

## EDD/readback

- Authenticated TestClient `/app` shell and card API regression tests passed.
- Full suite: `69 passed in 8.15s`.
- `python -m compileall -q kr_stock_autotrader app.py` passed.
- `git diff --check` passed.

## Implementation

- `user_fill_summary` aggregates every `order_fills` row joined only via the exact card and authenticated user's `order_plans`; it never infers from approval/plan status and exposes only `first_buy_at`, `last_full_sell_at`, `fill_state`.
- The UI keeps server `known_at` KST date queries, replaces flat filters with four AND-combined selects/reset, renders KST-friendly fill dates, and marks only bought cards with an accessible subtle green border/background.
- No order evaluator, scheduler, auth, live-trading, or allocation behavior was changed. `LIVE_TRADING=False` remains untouched.

## Limitations

- This local implementation did not deploy, push, alter cron, create ticks/orders, or use production credentials/runtime.
