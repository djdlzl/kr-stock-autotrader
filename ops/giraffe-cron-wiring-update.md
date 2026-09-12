# Giraffe cron wiring update — post-deploy only

Do **not** apply before the committed application is deployed. This is the exact live-registry mutation plan; it intentionally does not modify `~/.hermes/cron/jobs.json` in this repository worktree.

## Preconditions

- Deployed revision includes deterministic `POST /api/internal/research-runs/{run_key}/register` and the prehook uses `GIRAFFE_URL` plus `RESEARCH_CONTROL_KEY`.
- Preserve both job IDs, `enabled`, schedule, delivery topic, model, tools, workdir, and paper-only/no-trading safeguards.

## Atomic registry values

| job | JSON pointer | exact new value |
|---|---|---|
| `ff7955881377` | `/prompt` | JSON string in `ops/giraffe-cron-07-prompt.txt` |
| `8a223a4fa499` | embedded scheduler-prompt SHA-256 | `74afdadd5f0bccc551a9d5b4f2799a000044893fabe5e33a28ebbcdd73afa9c2` |

The exact desired 07:00 prompt is stored separately so an operator can use it byte-for-byte. It consumes `source_packet_paths` and `control_contract`; it does **not** require or inspect `material_candidate_records`.

## Readback assertions after one atomic update

```text
ff7955881377: enabled=true; schedule.expr="0 7 * * *"; deliver="telegram:-1003936097485:7923"; script="giraffe_dart_manifest_gate.py"; prompt contains source_packet_paths and control_contract; prompt does not contain material_candidate_records; prompt retains LIVE_TRADING=false and no order/capital/card instructions.
8a223a4fa499: enabled=true; schedule.expr="0 8 * * *"; deliver="telegram:-1003936097485:7923"; prompt contains prompts/giraffe-decision-card-scheduler-v1.md and SHA `74afdadd5f0bccc551a9d5b4f2799a000044893fabe5e33a28ebbcdd73afa9c2`; prompt retains LIVE_TRADING=False and no order/capital side effects.
```

## Source prompt integrity

- `prompts/giraffe-material-discovery-v1.md` SHA-256: `8705d14095a846067fa68c4fc41ca3e2270d9cef3a282bfee9a5534eb138c215`
- `prompts/giraffe-decision-card-scheduler-v1.md` SHA-256: `74afdadd5f0bccc551a9d5b4f2799a000044893fabe5e33a28ebbcdd73afa9c2`

No live registry mutation was made by this change.
