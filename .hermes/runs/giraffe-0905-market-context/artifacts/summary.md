# Giraffe 09:05 market-context rework

## Scope
- Kept the rework limited to `source_topic=mac:7923` and the existing 09:05 market-context workflow.
- Preserved the append-only store, readback endpoints, and the frozen 08:00 lineage model.

## RED Evidence
- Verifier review: `REQUEST_CHANGES`.
- Backdated probe before the fix returned `200 VERIFIED` for a `09:05` request whose providers reported `retrieved_at=2026-09-07T10:00:00+09:00`.
- Concurrent duplicate `run_key` requests produced an uncaught `sqlite3.IntegrityError: UNIQUE constraint failed: intraday_market_context_runs.run_key`.
- The route did not pass same-time history or previous-close lineage into the evaluator.
- Scenario observations still fabricated `volume_ratio=1.0`, `benchmark_excess_pct=0.0`, and `sector_excess_pct=0.0`.
- Baseline suite before the rework was `1 failed, 277 passed`; the failure was the UI renderer extraction for `${marketContext(c)}`.

## GREEN Evidence
- Focused rework suite:
  - `.venv/bin/pytest -q tests/test_intraday_market_context.py tests/test_kis_readonly_dryrun.py tests/test_event_scenarios.py tests/test_release0_app_ui.py`
  - Result: `92 passed, 2 warnings`
- Full suite:
  - `.venv/bin/python -m pytest -q`
  - Result: `282 passed, 2 warnings`

## Live Boundary
- The observed KIS 30-row response can contain six requested-day rows followed by prior-business-day padding. Projection hard-filters the requested KST date and `09:00:00..requested_as_of` before numeric parsing, then deduplicates and sorts provider rows.
- Each retained bar's `known_at` is parsed directly from `stck_bsop_date + stck_cntg_hour` in KST; rows later than `retrieved_at` and requests later than retrieval are rejected.
- Mixed-day same-time history now resolves from prior completed sessions on `2026-09-02`, `2026-09-03`, and `2026-09-04` for the `2026-09-07T09:05:00+09:00` request.
- The historical baseline is computed from comparable completed intervals only, yielding `same_time_baseline_cumulative_volume_0900_to_as_of=40.0` and `same_time_baseline_volume_ratio=1.25`.
- The operational boundary test accepts the `09:05` request and rejects the same-day `10:00` retrieval with `409` `market context outside operational window`.

## Reviewer Findings Closed
- Atomic idempotency: moved the `run_key` existence check inside the write transaction and kept duplicate requests deterministic.
- Operational-time gate: added explicit business-date/window checks and late-retrieval rejection before persistence.
- Historical baseline: same-time history now uses earlier session dates and the same requested minute window, with a minimum sample count of `3`.
- Previous close: lineage is now sourced only from the frozen 08:00 filter/card chain.
- Benchmark identity: benchmark symbol is resolved from the frozen lineage and fails closed if missing or mismatched.
- Scenario observations: removed fabricated values and now only pass authoritative market-context values when available.
- UI regression: the persisted detail readback path covers the same string-item preservation contract without reintroducing the renderer extraction failure.
- Status honesty: `MARKET_CONTEXT_HOLD` remains the output when same-time baseline or lineage is insufficient.

## Controls
- Added or kept focused tests for concurrency, operational boundaries, same-time history, previous-close lineage, conflict handling, and UI readback.
- Preserved no-lookahead behavior in production code.
- Left cron, deployment, and Obsidian untouched.

## Limitations
- No live external KIS network call was required for the rework verification; the route-level boundary and readback were validated with deterministic provider fixtures.
- The committed diff is limited to the 09:05 provider/API/persistence/evaluation/CLI/readback path, its scheduler contract, focused tests, the minimal card-detail rendering hook, and this evidence artifact.

## Commit
- Read the authoritative commit identity with `git rev-parse HEAD`; a commit cannot embed its own final SHA without changing that SHA.
