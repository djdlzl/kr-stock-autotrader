# Giraffe SEED-inspired authenticated UX implementation

- Date: 2026-08-30
- Method: TDD + EDD — auth-gate/source invariants in `tests/test_ui_auth_gate.py`; real Uvicorn signup → app → plan → logout readback.

## Scope and side effects

- Implemented an independently branded Giraffe UI. It references public SEED design-system principles only; no SEED package, Daangn asset, affiliation, or official integration is used.
- Side effects: local disposable test/runtime SQLite databases and a local Uvicorn process only. The server was stopped after readback.
- Live orders: none. `LIVE_TRADING = False`; the only broker implementation remains `PaperBroker`.

## Commands and results

```bash
rm -rf .venv && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/pytest -q
.venv/bin/python -m compileall -q .
git diff --check
```

Result: clean venv installation completed; `16 passed, 1 warning in 5.27s`; compileall and diff check exited 0. The warning is FastAPI/Starlette's installed `TestClient` deprecation notice, not an application failure.

```bash
SESSION_SECRET='runtime-secret-strong-and-unique-at-least-32-bytes-long' \
DATABASE_PATH="$(mktemp -t giraffe-seed-ux.XXXXXX.db)" \
.venv/bin/uvicorn app:app --host 127.0.0.1 --port 8765
```

Readback used a fresh cookie jar and database:

```text
runtime-readback: unauth-root=auth-shell unauth-app=303 signup={"ok":true} app=dashboard plan={"id":1,"status":"scheduled"} logout={"ok":true} root-after=auth-shell
```

The source-level smoke test additionally checks desktop/tablet/mobile CSS breakpoints (768/390), token usage, 44px touch minimum, focus-visible, reduced motion, auth tabs, status cards, conditional controls, cancel confirmation, expired-session redirect, and absence of debug JSON.

## Remaining risks

- No browser rendering dependency was added; responsive assertions are source-level CSS smoke coverage rather than screenshot/browser-layout assertions.
- Existing production hardening follow-ups remain unchanged: rate limiting, secret rotation, SQLite backup/permissions, and HTTPS configuration.
