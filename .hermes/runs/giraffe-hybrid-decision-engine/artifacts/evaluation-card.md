# Hybrid GOOD/BASE/BAD Second-Stage EDD Card

## Normal
- Eligible frozen scenario set with VERIFIED 09:05 market context and eligible untouched OOS calibration.
- Expected result: immutable `GOOD` + `BUY_REVIEW`.
- Expected readback: policy identity/version/hash, IS/OOS windows and counts, lower bound, denominator, and `recommendation_only=true`.

## Insufficient Evidence
- Same frozen scenario set with missing or undersized OOS calibration.
- Expected result: immutable `HOLD` + `HOLD_INSUFFICIENT_EVIDENCE`.
- Expected readback: explicit failure reasons, no optimistic fallback, no order side effects.

## Invalidated / BAD
- Same scenario set after business invalidation or closed-fail BAD condition.
- Expected result: immutable `BAD` + `REDUCE_REVIEW`.
- Expected readback: invalidation dominates and prevents BUY.

## Hostile
- Future or post-cutoff outcome rows.
- Forged derived policy hash / probability fields.
- Idempotency key collision with mismatched payload.
- Expected result: `422` or `409` and atomic no-op on forbidden tables.

## Prior-Only Control
- Structural prior looks positive but untouched OOS is unavailable or reused.
- Expected result: `HOLD` + `HOLD_INSUFFICIENT_EVIDENCE`.
- Expected readback: no BUY promotion from the prior alone.
