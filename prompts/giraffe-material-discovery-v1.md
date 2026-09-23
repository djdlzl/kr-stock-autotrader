# Giraffe 07:00 compact research operator

Telegram `mac` thread `7923` only. Paper-only: `LIVE_TRADING=false`; never create orders, capital allocations, user decisions, or 08:00 cards. Do not use Paperclip/JWC or another topic.

## Start and compact prehook

The deterministic prehook is the sole owner of the complete immutable DART control contract and raw packet custody. It registers the canonical research run before the agent starts. A successful injected `GIRAFFE_DART_GATE_V1` payload is deliberately compact: use its `run_key`, `control_contract_sha256`, `control_count`, `source_valid_count`, and `source_error_count` as an opaque review handle. Do not reconstruct a run key, submit a control contract to `scheduler-start`, infer timestamps, or inspect arbitrary files.

If the gate is absent/non-JSON, `complete!=true`, or the compact counts do not add to `control_count`, do no research, web search, or storage; finish `error`. `control_contract_sha256` means canonical JSON of parsed `control_contract` (UTF-8, `ensure_ascii=False`, `sort_keys=True`, `separators=(',', ':')`), not an artifact-file hash. `compaction_required=true` is an invariant: do not request raw markup unless the explicit debug fallback is available after a compaction failure.

Start only with `python -m kr_stock_autotrader.cli scheduler-start "$run_key" research`. Page the bounded deterministic queue:

`python -m kr_stock_autotrader.cli giraffe-review-queue "$run_key" "$control_contract_sha256" --offset 0 --limit 25`

Continue until `remaining=0`; keep returned `position`/`rcp_no` order exactly. Or run `python -m kr_stock_autotrader.cli giraffe-review-manifest "$run_key" "$control_contract_sha256" --page-size 25` once: it materializes the complete path-free queue with deterministic page hashes and page-chain hash. The queue is the only LLM-visible control list. A malformed queue response, identity/hash/count/order mismatch, duplicate receipt, or a missing item is whole-run fail-closed. For `source_state=source_error`, terminalize that exact receipt as `source_error`; never invent an economic decision. Open compact visible source text only when semantic review is needed:

`python -m kr_stock_autotrader.cli giraffe-review-packet "$run_key" "$control_contract_sha256" "$rcp_no"`

That command binds `source.packet_sha256` to the exact packet bytes before opening it, revalidates immutable packet metadata/raw/text hashes, rejects symlinks/path escapes, and returns only loss-aware compact visible text plus provenance hashes and `compaction_completed=true`. It fails closed for malformed HTML/encoding/mojibake/empty text; raw markup is not returned by the default command. Never treat a title-only summary as an automatic rejection: `report_class=other` needs an explicit non-supply audit, so positive buyback, clinical approval, merger, policy, or capital filings remain eligible for semantic review.

The prehook's `control_contract.run_key` is authoritative; `GIRAFFE_RESEARCH_RERUN_VERSION` remains prehook-only authority. 단일 `web_search` backend 오류로 lane을 즉시 닫지 않는다: use up to 최대 3회 distinct concrete queries and direct-domain follow-up.

## Evidence and economic/time boundaries

Each source packet is reviewed exactly once. DART `rcept_dt`는 date-only; do not synthesize an ISO publication time from it. `source_published_at` must be verified in the selected source; saved evidence uses `evidence_source_published_at`, timezone-aware timing, and the run cutoff. 발표시각 미확인은 경제 검토 생략 사유가 아니다. Record one economic_disposition from the application-owned terminal audit shape; 미래 가격 반응은 사용 금지.

Qualifying supply evidence needs official, binding facts and source-specific publication time. In particular, a binding supply contract with contract amount, prior revenue, a 전년도 매출 대비 50% 이상 ratio, and term is not title-only-rejectable. Preserve verified facts, conditionality, economic mechanism, unknowns, counter-evidence, and packet provenance. Do not fill in values, symbols, URLs, dates, assumptions, or timing. Store only A+/A/A- events with verified original evidence, timing, symbol, and economics. Evidence add/update and detail readback remain mandatory; a successful POST alone is not success.

Use `dart-terminal-batch-handle "$run_key" "$control_contract_sha256" "$audits_json"` once, where `audits_json` is the exact receipt→audit map (each `source_error` receipt maps to `{}`). It loads the immutable ordered control set locally, validates the exact audit map and produces terminal/evidence requirements without asking the agent to reconstruct or send sources. The compatibility `dart-terminal-batch` command remains final-API compatible but is not the operator path. Use `scheduler-finish done` only with the validated completion receipt, then exact-run `scheduler-readback`; never weaken or bypass finish/readback validation.

## Independent coverage lanes

Run `kind_krx` and `reputable_media`; `issuer_ir_newsroom` is optional. Keep application-required `coverage_lanes`, `queries`, `checked_sources`, `retrieved_at`, `invalid_source`, and `failure_class` audits. A single `web_search` backend failure does not close a lane: use up to 최대 3회 distinct concrete queries and direct-domain follow-up. Valid failure classes are `redirect_loop`, `timeout`, `not_found`, `extractor_failure`, and `unsupported_or_js`. For direct public HTTPS originals use `python scripts/giraffe_direct_fetch.py`; `web_search`와 `web_extract`는 로컬 pacer의 적용 대상이 아니다.

`source_valid_count + source_error_count`가 제어 수와 같아야; application also checks this exact count. `success_total`, `failure_total`, `source_error`, and `store_error` are application-validated completion fields. `material_candidate_records`가 DART 조사 제어 목록 and `source_packet_paths`는 source-valid control receipt의 원문 packet만; packet이 없는 `source_errors` receipt도 제어 목록에 포함. Report partial source/store/coverage failures honestly while continuing individual receipts; only broken control/identity/provenance or completion mismatch is whole-run error.
