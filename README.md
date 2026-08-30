# Giraffe — Paper-first Korean Stock Autotrader

**Giraffe**는 한국 주식의 예약 매수와 조건부 매도를 **모의 체결**하는 standalone FastAPI MVP입니다. 투자 추천이나 성과를 주장하지 않습니다.

## 로그인 게이트와 Giraffe UX

- 처음 방문한 `/`는 **로그인/회원가입 화면만** 제공합니다. 인증된 세션은 `/app`으로 이동하며, `/app`은 비로그인 방문자를 `/`로 돌려보냅니다. 기존 private API도 계속 `401`을 반환합니다.
- 가입·로그인 성공 뒤에는 `/app`에서 상태 요약(전체/예약 대기/보유 중/종료), 계획 목록, 단계형 `새 매수 계획`, 별도 `시세 입력` 패널을 사용합니다. 로그아웃하면 다시 인증 화면으로 이동합니다.
- 이 UI는 [SEED 디자인 시스템](https://seed-design.io)의 공개 semantic-color/role-typography 원칙에서 **영감을 받은 독자 구현**입니다. SEED 패키지나 당근 로고·캐릭터·상표 자산은 포함하지 않으며, 당근 또는 SEED와 제휴하거나 공식 통합한 제품이 아닙니다.
- 390px 모바일부터 태블릿/데스크톱까지 단일 열 또는 2열 레이아웃으로 바뀌며, 44px 이상의 터치 대상, label, focus-visible, reduced-motion을 제공합니다.

## 안전 경계

- `LIVE_TRADING=False`는 코드에 고정되어 있습니다. `BrokerPort`의 유일한 구현은 `PaperBroker`이며 네트워크/실주문 어댑터는 없습니다.
- Paper fill은 전량 체결만 기록하고, 수수료·세금·슬리피지는 명시적으로 모두 `0`입니다.
- 비밀번호는 salt를 포함한 scrypt hash로 저장됩니다. 세션은 HMAC 서명된 사용자 ID·발급시각·만료시각을 포함하며 HttpOnly/SameSite=Lax 쿠키입니다. HTTPS 배포는 `COOKIE_SECURE=true`를 설정하세요.
- SQLite는 MVP 저장소입니다. 로그인 rate limiting, 비밀 rotation, DB 백업/권한, HTTPS는 운영 전 보강 대상입니다.
- write API는 same-host `Origin`만 허용합니다. `Origin` 헤더가 없는 TestClient/CLI/API 클라이언트는 허용하는 계약이며, 브라우저 cross-origin 요청은 거부됩니다.

## 설치·실행

지원 Python은 **3.11+** (CI: 3.11)입니다.

```bash
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env # 값을 안전하게 변경한 뒤 환경에 로드
# 필수: 공개/기본값이 아닌 32 bytes 이상의 고유한 난수입니다.
export SESSION_SECRET="$(python -c 'import secrets; print(secrets.token_urlsafe(48))')"
uvicorn app:app --host 127.0.0.1 --port 8000

`SESSION_SECRET`가 없거나 빈 값·공개 개발 기본값·32 bytes 미만이면 앱은 요청을 받기 전에 시작을 거부합니다.
```

`http://127.0.0.1:8000`에서 가입/로그인, 계획 입력, 조건 추가/삭제, OR/AND, tick 시세 입력, 상태·체결·충족사유·최근 평가·감사로그·취소를 사용할 수 있습니다. 390px 폭에서도 입력이 넘치지 않도록 단일열로 전환됩니다.

## 규칙

- 종목코드는 6자리 숫자, 종목명은 필수, 수량은 양수입니다. 지정가 주문은 양수 지정가가 필수입니다.
- 예약 시각과 deadline은 ISO 날짜/시간(KST로 정규화)입니다. `deadline`은 fresh quote의 평가 시각이 deadline 조건을 통과할 때만 충족합니다.
- 매도 조건: `absolute_price`(원), `relative_pct`(매수가 대비 %), `volume`(주), `relative_volume`(기준 대비 배수), `deadline`(ISO KST). `OR`/`AND`를 지원합니다. 빈 조건은 허용하지만 자동매도하지 않습니다.
- quote의 `known_at`은 서버 평가 시각에 대해 0~5분이어야 합니다. 미래/5분 초과 quote는 매수·매도 모두 fail-closed입니다. KST 평일 09:00–15:30과 `KR_HOLIDAYS=YYYY-MM-DD,...`만 장중입니다.
- tick 평가는 로그인 사용자의 같은 종목 계획만 대상으로 합니다. `(plan, idempotency_key)`는 한 번만 처리하며 매수 전 매도·수량 초과 매도를 차단합니다. **같은 fresh tick에서 매수와 매도를 함께 체결하지 않습니다**. 매수 후 매도 조건 평가는 다음 고유 fresh tick부터 시작합니다.
- `closed` 계획 취소는 상태 위조 대신 `409`과 `current_status=closed`를 반환합니다. 이미 `cancelled`인 계획은 `200`으로 진실한 idempotent 결과를 반환합니다.

## API와 테스트

인증된 세션으로 `POST /api/ticks`에 다음처럼 전달합니다.

```json
{"symbol":"005930","price":70000,"volume":120000,"baseline_volume":60000,"known_at":"2026-08-31T10:00:00+09:00","idempotency_key":"feed-001"}
```

API: 가입/로그인/로그아웃, `POST/GET /api/plans`, `POST /api/plans/{id}/cancel`, `POST /api/ticks`.

```bash
.venv/bin/pytest -q
.venv/bin/python -m compileall -q .
```

테스트는 정확한 ±5% 경계, stale/future known-at fail-closed, 상대 거래량 0분모, OR/AND, 사용자 격리, idempotency, 체결 수량, deadline KST, Paper E2E를 검증합니다.

### Decision-card internal API and CLI

`material_evidence`, deterministic filter outputs, prompt-byte hash/versioned cards, user decisions, immutable approved `order_plans`, evaluations/fills/positions and scheduler audit rows are additive SQLite tables. `POST/PATCH /api/internal/*` is fail-closed unless `X-Internal-API-Key` constant-time matches `INTERNAL_API_KEY`; ordinary session users can only read `/api/cards` and submit `/api/cards/{id}/decisions`.

The deterministic filter records explicit units/denominators and rejects missing, zero-denominator, future, and stale inputs. Cards never overwrite a lineage version; evidence/card changes invalidate approved plans. Decision-card paper execution is intentionally a tick evaluator: it requires an approved unexpired plan, buys before a later distinct tick can exit, caps sells by remaining position, and has no live adapter.

The HTTP-only CLI never prints secrets and never starts a daemon: `GIRAFFE_URL=https://giraffe.example INTERNAL_API_KEY=... python -m kr_stock_autotrader.cli evidence-add '{...}'`. Supported commands are `today-evidence`, `evidence-add`, `evidence-detail`, `filter-run`, `pending-cards`, `card-save-result`, `scheduler-start`, and `scheduler-finish`. Scheduler rows are 07:00/08:00 orchestration records, not paper execution.


## Docker 운영 배포

`compose.prod.yml`은 기존 Caddy Docker network 뒤에서만 실행하며 앱 포트를 호스트에 공개하지 않습니다.

```bash
cp .env.prod.example .env
python3 -c 'import secrets; print(secrets.token_urlsafe(48))'  # 출력값을 SESSION_SECRET에 설정
mkdir -p data
sudo chown 10001:10001 data
IMAGE_TAG="$(git rev-parse --short HEAD)" docker compose -f compose.prod.yml up -d --build
```

운영 계약:

- 외부 HTTPS는 Caddy가 종료하고 앱은 `caddy-web-gateway_default` network의 `kr-stock-autotrader:8000`으로만 연결됩니다.
- `COOKIE_SECURE=true`이며 Uvicorn은 Caddy의 forwarded headers를 사용합니다.
- SQLite는 호스트 `./data/autotrader.db`에 영속 저장됩니다.
- `SESSION_SECRET`는 `.env`에만 두고 Git에 커밋하지 않습니다.
- 증권사/live adapter는 없으며 이 배포도 paper-only입니다.
