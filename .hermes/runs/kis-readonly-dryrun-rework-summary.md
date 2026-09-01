# KIS read-only dry-run rework receipt

Method: TDD + EDD — exact client lifecycle/idempotency invariants plus the authenticated API path; no live probe was run by this lane.

## Manager-supplied sanitized production evidence

Manager performed exactly one OAuth and one `005930` current-price request only (no account/order calls): OAuth HTTP 200, token present, `expires_in=86400`, and `access_token_token_expired` present; quote HTTP 200, `rt_cd=0`, price/volume present. Sanitized quote evidence SHA-256: `a25377a8d78cd22845d2627ce7cedb0b757d6aaba6f1d9fd5210719ef2d74310`.

Critically, that current-price response has no `stck_cntg_hour` and no current business-date field. Date-like high/low fields are not observation or trade time and are never used.

## Repair

- Host is literal-pinned to `https://openapi.koreainvestment.com:9443` (only a trailing slash normalizes); injected transports cannot change it.
- OAuth and quote require successful HTTP status. Tokens and bounded expiries remain process-memory-only; a recognized expired-auth quote/401 clears the token and permits exactly one OAuth+quote retry.
- Safe quote output derives `quote_known_at` at network retrieval completion with `timestamp_source=network_retrieved_at`, never an exchange trade-time claim. Dry-run requires that source, freshness, market session/calendar, and safe optional status checks.
- Receipt lookup occurs before provider/OAuth work. Single-process key locks serialize concurrent same-key work; duplicate insert conflict rereads the receipt. Successful safe quotes use a two-second local cache; failures are not cached.
- `dry_run_key` is restricted to `[A-Za-z0-9_-]{8,64}`.

## Verification

- Focused KIS tests: `4 passed`.
- Full pytest: `80 passed`.
- No live probe, deploy, push, merge, account call, allocation, or order was performed by this implementation lane.
