# KIS read-only dry-run rework receipt

Method: TDD + EDD — exact client lifecycle/idempotency invariants plus the authenticated API path; no live probe was run by this lane.

## Manager-supplied sanitized production evidence

Manager performed exactly one OAuth and one `005930` current-price request only (no account/order calls): OAuth HTTP 200, token present, `expires_in=86400`, and `access_token_token_expired` present; quote HTTP 200, `rt_cd=0`, price/volume present. Sanitized quote evidence SHA-256: `a25377a8d78cd22845d2627ce7cedb0b757d6aaba6f1d9fd5210719ef2d74310`.

Critically, that current-price response has no `stck_cntg_hour` and no current business-date field. Date-like high/low fields are not observation or trade time and are never used.

## Repair

- Host is literal-pinned to `https://openapi.koreainvestment.com:9443` (only a trailing slash normalizes); injected transports cannot change it.
- OAuth and quote require successful HTTP status. Tokens and bounded expiries remain process-memory-only; a recognized expired-auth quote/401 clears the token and permits exactly one OAuth+quote retry.
- Every provider outcome (including non-dict values, malformed statuses, and exceptions) passes through one strict projection before API response, cache, or dry-run persistence. Failure output is a fixed unavailable schema with only a valid retrieval time and approved timestamp source when present; it never carries provider reasons, headers, credentials, tokens, cookies, or raw payloads. Safe quote output derives `quote_known_at` at network retrieval completion with `timestamp_source=network_retrieved_at`, never an exchange trade-time claim. At that same API/cache boundary, both timestamps must parse as the exact same instant and be within the dry-run five-minute freshness horizon (including no future timestamps); 2099/future, stale, and incoherent provider timestamps become unavailable and are never cached. Dry-run additionally requires that source, freshness, market session/calendar, and safe optional status checks.
- Receipt lookup occurs before provider/OAuth work. Ref-counted keyed locks serialize concurrent same-key work, include queued waiters, and remove their registry entry after the last exit; duplicate insert conflict rereads the receipt. Successful safe quotes use a two-second per-symbol single-flight cache; stale entries are purged, insertion order is deterministically capped at 256 entries, and failures are not cached.
- Account readiness is presence-only but structural: canonical `KIS_ACCOUNT_NO` or legacy `R_ACCOUNT_NUMBER` must be exactly 8 digits and `KIS_ACCOUNT_PRODUCT_CODE` exactly 2 digits; `LS_ACCOUNT` remains ignored and no account endpoint is called.
- `dry_run_key` is restricted to `[A-Za-z0-9_-]{8,64}`. Receipts are durable SQLite audit records; the UI cache/cooldown is not authorization for a network burst, and no new durable-rate infrastructure was added.

## Verification

- Focused KIS tests: `39 passed` (including deterministic API/cache-boundary 2099, future, stale, and incoherent-timestamp rejection plus current coherent-cache control; concurrent same-symbol/same-key and hundreds-of-keys cleanup regressions; and unavailable/malformed/non-dict/exception leakage cases through both authenticated quote and dry-run receipt paths).
- Full pytest: `115 passed`.
- `python -m compileall -q kr_stock_autotrader tests`: passed.
- No live probe, deploy, push, merge, account call, allocation, or order was performed by this implementation lane.
