# Review

APPROVED

## Findings

No concrete defects found in the changed hybrid decision-engine paths.

## Verification

Commands run:

```text
.venv/bin/pytest -q tests/test_hybrid_decision_engine.py
```

Result:

```text
21 passed, 2 warnings in 22.10s
```

Commands run for dependency setup:

```text
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

Result:

```text
Successfully installed annotated-doc-0.0.5 annotated-types-0.8.0 anyio-4.15.1 certifi-2026.7.22 click-8.5.0 fastapi-0.141.1 h11-0.16.0 httpcore-1.0.9 httpx-0.28.1 idna-3.19 iniconfig-2.3.0 packaging-26.3 pluggy-1.6.0 pydantic-2.13.5 pydantic-core-2.46.5 pygments-2.21.0 pytest-9.1.1 starlette-1.6.0 typing-extensions-4.16.0 typing-inspection-0.4.4 uvicorn-0.52.4
```

## Hostile Probes

Covered by the hybrid contract suite:

```text
test_hybrid_missing_scenario_and_empty_bars_are_controlled_4xx
test_hybrid_outcome_requires_market_context_run_id
test_hybrid_outcome_rejects_cross_card_market_context_run_id
test_hybrid_outcome_rejects_market_context_known_after_first_bar
test_hybrid_calibration_rejects_posthoc_windows_and_target_case_reuse
test_hybrid_requires_30_distinct_cases_not_30_rows_from_one_case
test_hybrid_unqualified_context_cases_are_excluded_from_calibration_denominator
test_hybrid_holdout_reuse_blocks_distinct_plans_and_exact_replay_is_idempotent
test_hybrid_policy_hash_drift_and_recommendation_only_false_are_rejected
```

Observed results from those probes:

- missing scenario returned `404`
- empty bars returned `422`
- missing `market_context_run_id` returned `422`
- cross-card market context reuse returned `409`
- future-known market context returned `422`
- target-case reuse in calibration returned `422`
- 30 rows from one case did not satisfy the cohort requirement
- 30 unqualified cases were excluded from calibration and left the selected count at zero
- distinct-plans holdout reuse was blocked with `409`
- `recommendation_only=false` was rejected with `422`

## Sanity And Neutral Controls

- cost-adjusted first-touch is enforced with Decimal arithmetic
- same-bar ambiguity resolves conservatively to `BAD`
- `GOOD` requires `structural_good_compatibility.status == GOOD_COMPATIBLE`
- policy-qualified calibration is separated from unqualified historical rows
- the structural-prior-only and hybrid-candidate controls use the same OOS denominator
- the readback surfaces policy identity/version/hash, selected/excluded counts, selected/excluded IDs, reason counts, windows, concentration, costs, and holdout reuse metadata

## Exact Windows And Holdout Reuse

Representative calibrated snapshot exercised in the suite:

- IS window: `2026-09-08T00:00:00+09:00` to `2026-09-09T00:00:00+09:00`
- OOS window: `2026-09-09T00:00:00+09:00` to `2026-09-11T23:59:59+09:00`
- selected denominator in the control test: `2`
- holdout identity on first snapshot: `use_count=1`, `reuse_count=0`
- exact replay of the same snapshot was idempotent
- reuse of the same holdout key on a distinct plan was blocked

## Claim Limits

- This is synthetic contract evidence only.
- It does not claim live trading performance, production alpha, or execution readiness.
- It does not create orders, allocations, fills, or live dry-run receipts.
- Any BUY review claim is limited to the canonical market-context readback plus the structural GOOD compatibility gate.

## Promotion Gate

Promote only if the current contract remains true for:

1. `recommendation_only=true` hard enforcement
2. server-owned `policy_qualified` gating
3. immutable plan and snapshot lineage
4. exact holdout-once enforcement
5. zero-order side effects on all hostile-input failures
