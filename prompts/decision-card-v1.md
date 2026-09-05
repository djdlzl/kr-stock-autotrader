# decision-card-v1 — evidence-bound paper-trading judgment

Produce one structured, immutable decision card from the supplied **same-lineage** evidence and deterministic filter. Do not invent a market value, time, source, certainty, or rule. This is paper-only research, never an instruction to place a live order.

## Judgment sequence
1. Identify the issuer, event, source URL/snapshot, announcement time, `known_at`, and what actually changed.
2. State business value and the economic mechanism; quantify only when evidence supports it.
3. Assess certainty. Treat `MOU`, `검토`, `신청`, conditional wording, financing, CB, and 유상증자 as low-certainty/conflict signals unless disproven by the supplied evidence.
4. Assess whether the effect is already priced in using only supplied observations; separate unknowns.
5. Preserve the deterministic filter result. A FAIL filter can only produce `제외`, `관찰`, or `판단 보류`—never `매수 검토 가능`.
6. For biotech, distinguish economically calculable events from binary clinical/regulatory risk. Do not exclude merely because the issuer is biotech.
7. Specify frozen paper-only entry/exit constraints only for `매수 검토 가능`. If data is insufficient, choose `판단 보류`; for `관찰`, `제외`, or `판단 보류`, use JSON `null` (or an empty string/list/object where the field type calls for it) for unavailable order fields. Never fabricate placeholder prices, quantities, times, order types, or exit rules. A `매수 검토 가능` card instead requires concrete, evidence-supported entry, exit, invalidation, and KST time values for every order-plan field.
8. For every scenario-eligible new card (`판단 보류` or `매수 검토 가능`), source-define a positive `proof_point` and a concrete `next_check`. These create only a frozen conditional research scenario; when price evidence is unavailable, leave order fields null. `proof_point` must never be an unknown or question, and `next_check` must name the next source-bound confirmation. Historical persisted v1 cards remain compatible and are not retroactively invalidated.

## Required JSON fields
`symbol`, `headline`, `conclusion`, `change`, `source_evidence`, `source_urls`, `business_value`, `certainty`, `priced_in`, `filter_verdict`, `price_cap`, `window` (`start` and `end`, KST), `max_amount`, `max_qty`, `stop_loss`, `take_profit` (including split-sell rules), `evidence_invalidation`, `holding_until`, `review_at`, `false_positive`, `unknowns`, `proof_point` (when source-supported), `next_check` (when source-supported), `verdict`, `confidence`.

The persisted envelope additionally records `prompt_version`, SHA-256 `prompt_hash` of these exact bytes, `model`, `provider`, `evidence_id`, `filter_id`, filter/evidence versions, generated time, card lineage and version. Cards are append-only; changed evidence, filter, or prompt requires a new card version and reapproval.
