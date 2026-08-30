# Decision-card non-buy null fields

Method: TDD + EDD — schema/approval boundary regressions plus the full HTTP/SQLite paper lifecycle.

## Change
- Versioned `DecisionCard` permits `null`/empty unavailable order fields only for `관찰`, `제외`, and `판단 보류`.
- `매수 검토 가능` remains fail-closed: concrete finite positive order fields, a real KST window, valid KST exit times, nonempty invalidation, positive take-profit rule, and valid order type are required both on save and before draft/approval.
- The prompt tells non-buy generation to emit JSON null/empty unavailable order fields and prohibits fabricated placeholders.
- The UI renders unavailable values as `해당 없음`; non-buy cards retain only hold/reject actions and cannot show draft/approval controls.

## Verification
- RED: the added null-card tests failed against the prior schema (non-buy nulls rejected; buy missing fields accepted).
- Focused: `3 passed` for new non-buy/null EDD regressions.
- Full: `60 passed` (`pytest -q`; one existing FastAPI TestClient deprecation warning).
- `python -m compileall -q kr_stock_autotrader tests app.py` passed.
- Extracted decision-card JavaScript passed `node --check`.
- `git diff --check` passed.
