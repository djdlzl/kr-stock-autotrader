# Giraffe KIS read-only + live dry-run — implementer summary

Method: TDD + EDD — allowlist/redaction/idempotency/no-side-effect boundary tests; authenticated status/UI workflow readback.

## Official KIS evidence

- OAuth: `POST /oauth2/tokenP`, production base `https://openapi.koreainvestment.com:9443`. Official KIS sample authentication source: https://github.com/koreainvestment/open-trading-api/blob/main/examples_llm/kis_auth.py
- Domestic current price: `GET /uapi/domestic-stock/v1/quotations/inquire-price`, `tr_id=FHKST01010100`, query `FID_COND_MRKT_DIV_CODE=J`, `FID_INPUT_ISCD=<six digits>`. Official KIS sample: https://github.com/koreainvestment/open-trading-api/blob/main/examples_llm/domestic_stock/inquire_price/inquire_price.py

No credential-bearing or authenticated external call was made.

## RED → GREEN

- RED scope: hard allowlist / six-digit validation; malformed response fail-closed; dry-run outcome boundaries; legacy account presence alias.
- GREEN: `python -m pytest -q` → `79 passed`; `python -m compileall -q .`; `git diff --check`.
- Authenticated API readback (isolated temporary DB, no KIS credentials): signup 200; `GET /api/kis/status` 200 with `account_readiness=blocked_missing_account_env`, `LIVE_TRADING=false`, `broker_mode=read_only_dry_run`, `network_order_calls=0`; invalid quote symbol returns 422.

## Implemented safety boundaries

- `KISReadOnlyClient` has no public arbitrary-path method. Its private transport guard permits exactly OAuth and current price; no order, amend/cancel, balance, or account paths exist.
- Credentials/token/auth headers/raw KIS body never enter status, quote projection, DB receipt, or logs. OAuth token stays in process memory.
- Account readiness accepts canonical `KIS_ACCOUNT_NO`, plus verified presence-only `R_ACCOUNT_NUMBER` fallback, and canonical `KIS_ACCOUNT_PRODUCT_CODE`. `LS_ACCOUNT` is intentionally ignored. `KIS_ACCOUNT_NO` absent (and no fallback) blocks account readiness; this scope makes **zero** balance/account calls.
- `live_dry_run_receipts` is additive and safe-field-only, unique on `(order_plan_id,user_id,dry_run_key)`. The pure evaluator never imports/calls PaperBroker or the order evaluator and does not mutate plans/fills/positions/order evaluations/events.
- UI adds read-only KIS status/current quote and an approved-plan `실전주문 사전점검` action. It explicitly says `실전 주문은 아직 비활성화`; no live order/toggle was added.

## Limitations / handoff

- No KIS live probe was run; manager gate should do at most OAuth + `005930` quote with controlled credentials.
- Account presence says only readiness: no balance, buying-power, allocation, or order capability was added.
- `KIS_BASE_URL` defaults in code to production and is only read from the explicitly named override env variable.
