# Decision-card verifier rework receipt

Method: TDD + EDD — deterministic lineage/schema regressions; authenticated API and CLI user journeys.

## RED

Independent verifier report `/tmp/giraffe-verifier-7923/report.md` recorded against source SHA `be50bf33c40661c512e1d85f15e4633a5b13f86b`:

- B1 partial-fill evidence mutation allowed a fresh second buy (`partial_or_filled`).
- B2 accepted evidence look-ahead and allowed a stale filter after evidence mutation.
- B4 approved-plan UI passed SQL-shaped plan fields to the draft form, producing null window values.
- `pending-cards` omitted `?missing=true`; card payload validation was presence-only.

## GREEN

Code SHA: `16a1ece83f3a813a0820a9fd940021343627695a`

- `tests/test_verifier_rework.py`: 6 passed (partial mutation/frozen exit, look-ahead/version rejection, schema malformations, repeat-safe migration backfill, authenticated approve→detail snapshot→edit→reapprove API journey, and local HTTP CLI secrecy/query check).
- Full suite: `55 passed, 1 upstream TestClient deprecation warning`.
- `python -m compileall -q kr_stock_autotrader`: passed.
- Node check: `node --check` of extracted decision-card UI script passed; snapshot selection and KST datetime-local conversion passed.
- `git diff --check`: passed.

B3 remains an external topic-specific deployment/cron gate. No application scheduler, daemon, cron, or runtime infrastructure was added; scheduler summary fields remain bookkeeping/display only.
