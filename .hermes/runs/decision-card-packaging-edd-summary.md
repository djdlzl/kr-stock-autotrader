# Decision-card packaging + API/DB lifecycle EDD

- Method: TDD + EDD — deterministic Docker COPY/summary-union boundaries; a real TestClient + isolated SQLite paper-lifecycle journey.
- Implementation SHA: `05f2c792a0059554fdc34eb40523a86afd403f18`

## RED

` .venv/bin/pytest -q tests/test_decision_card_packaging_edd.py ` on the original Dockerfile: **2 failed**.

1. `test_container_copy_contract_and_runtime_prompt_asset` failed because `COPY prompts ./prompts` was absent.
2. The initial lifecycle assertion exposed the observed `order_evaluations` denominator as 7 rather than the initially misstated 8; the final regression fixes its asserted observed DB lineage to 7.

## GREEN

- ` .venv/bin/pytest -q tests/test_decision_card_packaging_edd.py `: **2 passed** (one pre-existing TestClient deprecation warning).
- ` .venv/bin/pytest -q `: **49 passed** (same warning).
- ` .venv/bin/python -m compileall -q app.py kr_stock_autotrader tests `: passed.
- ` git diff --check `: passed before the implementation commit.

The EDD executes internal research scheduler start/finish; evidence create/list/detail/dedupe; PASS filtering; prompt request/hash; structured PASS and FAIL cards; authenticated list/detail/draft/approval; paper-only negative controls; buy, frozen split take-profit, and manual close. It reads all requested SQLite lineage tables and blocks socket network creation for the in-process journey. `LIVE_TRADING` remains false.

## Docker blocker

No image was built: the local Docker daemon is unavailable (`docker version` returned exit 1). The deterministic Docker-context COPY regression is the available pre-daemon packaging proof.
