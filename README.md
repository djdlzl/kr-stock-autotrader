# Giraffe — Paper-first Korean Stock Autotrader

**Giraffe**는 한국 주식의 예약 매수와 조건부 매도를 **모의 체결**하는 standalone FastAPI MVP입니다. 투자 추천이나 성과를 주장하지 않습니다.

## 로그인 게이트와 Giraffe UX

- 처음 방문한 `/`는 **로그인/회원가입 화면만** 제공합니다. 인증된 세션은 `/app`으로 이동하며, `/app`은 비로그인 방문자를 `/`로 돌려보냅니다. 기존 private API도 계속 `401`을 반환합니다.
- 가입·로그인 뒤 `/app`은 **결정 카드 대시보드**를 제공합니다. 기준일은 서버의 `material_evidence.known_at` KST 업무일이며, 판단·내 결정·체결·카드 상태 드롭다운은 함께(AND) 적용되고 초기화할 수 있습니다. 각 카드의 `매수일시`/`매도일시`는 현재 로그인 사용자의 실제 paper fill만으로 표시합니다. 최초 buy fill이 있으면 연한 초록 카드가 되지만 수익이나 성과를 뜻하지 않습니다. `매수 검토 가능` + deterministic `PASS` 카드만 사용자 승인·초안·재승인을 할 수 있습니다. 승인된 계획은 immutable snapshot이며, 이후 편집은 이전 계획의 **진입만** 무효화하고 열린 paper 포지션의 손절·익절·수동 매도는 계속 관리합니다.
- **기본 모의투자 금액**은 사용자별 `500,000원` fallback이며 `GET/PATCH /api/settings/paper`로 현재 사용자만 읽고 바꿉니다. 정수 원 단위 `10,000..1,000,000,000`만 허용합니다. 새 PASS 매수검토 카드의 첫 초안/즉시 승인에서만 동결 후보가 되고, 카드의 **이 카드에 적용할 금액** 초안, 이전 승인계획, 체결/포지션은 이후 기본값 변경으로 절대 바뀌지 않습니다.
- 기존 `/api/plans` 수동 계획은 legacy 기록으로 남아 있으며 **manual_only**입니다. 새 자동 실행 경로가 아니고, 이 대시보드에서 새 legacy 계획을 만들거나 실행하지 않습니다.
- 대시보드는 390px 모바일부터 태블릿/데스크톱까지 단일 열 또는 2열 레이아웃으로 바뀌며, 44px 이상의 터치 대상, label, focus-visible을 제공합니다.

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

`http://127.0.0.1:8000`에서 가입/로그인 후 결정 카드 대시보드를 사용합니다. 내부 수집자는 evidence → filter → immutable card를 저장하고, 사용자는 PASS인 `매수 검토 가능` 카드만 paper-only 계획으로 승인합니다. 사후 편집은 새 draft를 만들고 기존 진입을 무효화한 뒤 명시 재승인을 요구합니다. Legacy `POST /api/plans`는 수동 기록 전용입니다.

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

`material_evidence`와 filter/card lineage는 additive SQLite 테이블입니다. evidence mutation은 version을 증가시키고 모든 이전 filter/card/entry lineage를 무효화합니다. 정상 snapshot filter는 `evidence.known_at <= filter.known_at <= filter.as_of`와 `market_data_known_at <= filter.known_at <= filter.as_of`를 각각 기록해야 하며, evidence와 market data 사이의 순서는 요구하지 않습니다. unavailable snapshot은 관측값을 꾸미지 않고 정확히 `market_data_status="unavailable"`와 안전한 snapshot `market_data_attempted_at`만 기록하며 `market_data_attempted_at <= filter.known_at <= filter.as_of`일 때만 deterministic `FAIL (market data unavailable)`로 저장됩니다. 복구 시 attempt가 요청 as_of보다 늦으면 422이며 과거 시장자료라고 backdate하지 않습니다. card는 그 evidence version과 같은 filter만 사용할 수 있습니다. Card payload는 `schema_version: 1`, 절대 HTTP(S) source URL, KST ordered window, 유한 양수 cap/수량/exit rule 및 0..1 confidence의 Pydantic schema로 검증됩니다. `POST/PATCH /api/internal/*` is fail-closed unless `X-Internal-API-Key` constant-time matches `INTERNAL_API_KEY`; ordinary session users can only read `/api/cards` and submit decisions/drafts/edits.

The HTTP-only CLI never prints secrets and never starts a daemon: `GIRAFFE_URL=https://giraffe.example INTERNAL_API_KEY=... python -m kr_stock_autotrader.cli evidence-add '{...}'`. `pending-cards` calls `/api/internal/cards?missing=true`; `market-snapshot SYMBOL AS_OF --announcement-at KST_ISO` returns the safe completed-bar projection and reviewed-config filter inputs. Supported commands are `today-evidence`, `evidence-add`, `evidence-detail`, `market-snapshot`, `filter-run`, `pending-cards`, `card-request`, `card-save-result`, `card-detail`, `scheduler-start`, and `scheduler-finish`. Scheduler rows and the dashboard's “next run” are **bookkeeping/display only**: they do not run research or card jobs. Use the approved topic-specific external schedule after deployment; this application deliberately includes no scheduler daemon or app job infrastructure.


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
- 내부 수집/카드/평가 API에는 `INTERNAL_API_KEY`를 `.env`에 설정하고 호출 시 `X-Internal-API-Key`로 전달합니다. 값은 이미지·문서·Git에 넣지 않습니다.
- 증권사/live adapter는 없으며 이 배포도 paper-only입니다.

## KIS 읽기전용·실전주문 사전점검

KIS 연결은 **읽기전용**입니다. `KISReadOnlyClient`가 허용하는 외부 경로는 pinned production host `https://openapi.koreainvestment.com:9443`의 `POST /oauth2/tokenP`, 국내주식 현재가 `GET /uapi/domestic-stock/v1/quotations/inquire-price`(TR ID `FHKST01010100`), 그리고 premarket projection 전용 일봉 `GET /uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice`(TR ID `FHKST03010100`)뿐입니다. Official KIS contract/source: https://apiportal.koreainvestment.com/api/apis/public/detail?accessUrl=/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice . It specifies `output2` and the `stck_bsop_date`, `stck_clpr`, `acml_vol`, and `acml_tr_pbmn` fields; its published sample's `hts_avls`, listed shares, and price cross-check the declared 100,000,000-KRW capitalization unit (guarded by a contract fixture). `KIS_BASE_URL`은 이 literal host(마지막 `/`만 정규화) 외에는 거부합니다. 토큰은 process memory에서 official expiry 또는 bounded `expires_in`까지 쓰고 refresh-skew 전에 갱신합니다. API, 로그, SQLite receipt에 키·비밀·토큰·Authorization header·원문 응답을 반환하거나 저장하지 않습니다. 모든 provider 결과는 API/cache/dry-run 경계에서 safe projection을 거치며, unavailable/error는 고정된 unavailable schema만 반환합니다.

로그인 사용자는 `/api/kis/status`, `/api/kis/quote/{six-digit-symbol}`을 이용할 수 있습니다. `KIS_ACCOUNT_NO` 또는 verified legacy `R_ACCOUNT_NUMBER`은 값 노출 없이 정확히 8자리 숫자여야 하며, `KIS_ACCOUNT_PRODUCT_CODE`은 정확히 2자리 숫자여야 하는 presence-only readiness 신호입니다. `LS_ACCOUNT`은 사용하지 않습니다. 이 구조 검증을 통과하지 않으면 `blocked_missing_account_env`이고 잔고/계좌 endpoint는 호출하지 않습니다.

승인된 자기 order plan에서 `POST /api/order-plans/{plan_id}/live-dry-run`에 `[A-Za-z0-9_-]{8,64}` `dry_run_key`를 보내면 server-side KIS quote로 `WOULD_SUBMIT`, `WOULD_WAIT`, 또는 `WOULD_REJECT` receipt만 남깁니다. durable SQLite `(plan,user,key)` receipt는 provider/OAuth 호출 전에 확인되고 ref-counted single-process key lock으로 concurrent click도 serialize합니다. 성공 quote는 per-symbol single-flight 2초 safe-projection cache만 사용하며 stale entries를 제거하고 256 entries로 제한합니다. UI cooldown/cache is not a network-burst authorization: each distinct key can create a durable receipt and no rate-limiting infrastructure is implied by this release. KIS current-price에는 reliable last-trade date/time이 없으므로 `quote_known_at`은 `timestamp_source=network_retrieved_at`인 KST retrieval-completion time이며 exchange trade time을 주장하지 않습니다. freshness, KST market window/calendar, and any available halt/management status must all pass; closed/stale/unavailable data is `WOULD_WAIT`. 이 경로는 PaperBroker/order engine을 호출하지 않고 plan, fills, positions, order evaluations/events를 변경하지 않으며 항상 `LIVE_TRADING=false`, `broker_mode=read_only_dry_run`, `network_order_calls=0`입니다. **실전 주문은 아직 비활성화**입니다.
