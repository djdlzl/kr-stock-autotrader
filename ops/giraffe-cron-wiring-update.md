# Giraffe cron wiring update — post-deploy only

Do **not** run these commands until this commit is deployed at the 08:00 job's `workdir`. They deliberately mutate the live Hermes script directory and cron registry, never this worktree's `~/.hermes/cron/jobs.json`.

## Fixed inputs

```bash
export GIRAFFE_DEPLOYED_REPO=/Users/jaewoo/kr-stock-autotrader
export HERMES_HOME=${HERMES_HOME:-$HOME/.hermes}
export GIRAFFE_07_PROMPT_SHA256=d6454659697510e0667026378423d8b9a7972672bca2257656272d6fab8c3f4d
export GIRAFFE_08_SCHEDULER_PROMPT_SHA256=287972a1b03bc986905cb86e62575451ecde11893bbe786c0a2fc216826dfb38
```

The 07:00 source file is tracked at `ops/giraffe-cron-07-prompt.txt` (introduced by `f2e6b726`) and its required SHA-256 is `d6454659697510e0667026378423d8b9a7972672bca2257656272d6fab8c3f4d`. Do not regenerate it.

## 1. Install the deployed 08:00 prehook and read it back

The installed script resolves `kr_stock_autotrader` from the cron job `workdir`; the registry step below asserts that `8a223a4fa499` uses `$GIRAFFE_DEPLOYED_REPO`.

```bash
set -euo pipefail
src="$GIRAFFE_DEPLOYED_REPO/scripts/giraffe_decision_card_gate.py"
dst="$HERMES_HOME/scripts/giraffe_decision_card_gate.py"
test -f "$src"
install -d -m 755 "$HERMES_HOME/scripts"
install -m 755 "$src" "$dst"
cmp -s "$src" "$dst"
src_sha=$(shasum -a 256 "$src" | cut -d ' ' -f 1)
dst_sha=$(shasum -a 256 "$dst" | cut -d ' ' -f 1)
test "$src_sha" = "$dst_sha"
printf 'installed giraffe_decision_card_gate.py sha256=%s\n' "$dst_sha"
```

## 2. Update both cron records atomically

This preserves every field except the named 07 prompt, 08 script, and the old 08 canonical-prompt hash embedded in the 08 prompt.

```bash
set -euo pipefail
export GIRAFFE_07_PROMPT_FILE="$GIRAFFE_DEPLOYED_REPO/ops/giraffe-cron-07-prompt.txt"
python3 - <<'PY'
import hashlib
import json
import os
from pathlib import Path

home = Path(os.environ["HERMES_HOME"])
registry = home / "cron" / "jobs.json"
prompt_file = Path(os.environ["GIRAFFE_07_PROMPT_FILE"])
expected_07 = os.environ["GIRAFFE_07_PROMPT_SHA256"]
old_08 = "287972a1b03bc986905cb86e62575451ecde11893bbe786c0a2fc216826dfb38"
new_08 = os.environ["GIRAFFE_08_SCHEDULER_PROMPT_SHA256"]
prompt_07 = prompt_file.read_text()
assert hashlib.sha256(prompt_07.encode()).hexdigest() == expected_07
payload = json.loads(registry.read_text())
jobs = payload["jobs"]
by_id = {str(job["id"]): job for job in jobs}
assert set(("ff7955881377", "8a223a4fa499")) <= set(by_id)
by_id["ff7955881377"]["prompt"] = prompt_07
job_08 = by_id["8a223a4fa499"]
assert old_08 in str(job_08["prompt"])
job_08["script"] = "giraffe_decision_card_gate.py"
job_08["prompt"] = str(job_08["prompt"]).replace(old_08, new_08)
temp = registry.with_suffix(".json.tmp")
temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
os.replace(temp, registry)
PY
```

## 3. Registry readback (required)

```bash
set -euo pipefail
python3 - <<'PY'
import hashlib
import json
import os
from pathlib import Path

home = Path(os.environ["HERMES_HOME"])
repo = os.environ["GIRAFFE_DEPLOYED_REPO"]
expected_07 = os.environ["GIRAFFE_07_PROMPT_SHA256"]
expected_08 = os.environ["GIRAFFE_08_SCHEDULER_PROMPT_SHA256"]
jobs = json.loads((home / "cron" / "jobs.json").read_text())["jobs"]
by_id = {str(job["id"]): job for job in jobs}
job_07, job_08 = by_id["ff7955881377"], by_id["8a223a4fa499"]
assert job_07["enabled"] is True
assert job_07["schedule"]["expr"] == "0 7 * * 1-5"
assert job_07["deliver"] == "telegram:-1003936097485:7923"
assert job_07["script"] == "giraffe_dart_manifest_gate.py"
assert hashlib.sha256(job_07["prompt"].encode()).hexdigest() == expected_07
assert "source_packet_paths" in job_07["prompt"] and "control_contract" in job_07["prompt"]
assert "material_candidate_records" not in job_07["prompt"]
assert "LIVE_TRADING=false" in job_07["prompt"]
assert job_08["enabled"] is True
assert job_08["schedule"]["expr"] == "0 8 * * 1-5"
assert job_08["deliver"] == "telegram:-1003936097485:7923"
assert job_08["workdir"] == repo
assert job_08["script"] == "giraffe_decision_card_gate.py"
assert expected_08 in job_08["prompt"]
assert "LIVE_TRADING=False" in job_08["prompt"]
print("cron registry readback passed")
PY
```

## 4. Installed-script wake-gate readback (required)

Run all checks from the deployed repository so the copied prehook resolves the deployed calendar package. A closed date must exit `0` and print a final JSON line containing `"wakeAgent": false`; an admission error must exit `2` with that same false wake gate, so Hermes skips the scheduler agent/LLM in both cases.

```bash
set -euo pipefail
cd "$GIRAFFE_DEPLOYED_REPO"
"$HERMES_HOME/scripts/giraffe_decision_card_gate.py" --at 2026-07-17T08:00:00+09:00 > /tmp/giraffe-08-closed.json
set +e
"$HERMES_HOME/scripts/giraffe_decision_card_gate.py" --at 2027-01-04T08:00:00+09:00 > /tmp/giraffe-08-error.json
status=$?
set -e
test "$status" -eq 2
python3 - <<'PY'
import json
from pathlib import Path
closed = json.loads(Path("/tmp/giraffe-08-closed.json").read_text().splitlines()[-1])
error = json.loads(Path("/tmp/giraffe-08-error.json").read_text().splitlines()[-1])
assert closed["wakeAgent"] is False
assert error["wakeAgent"] is False and error["complete"] is False
PY
"$HERMES_HOME/scripts/giraffe_decision_card_gate.py" --at 2026-07-20T08:00:00+09:00 > /tmp/giraffe-08-open.json
python3 - <<'PY'
import json
from pathlib import Path
payload = json.loads(Path("/tmp/giraffe-08-open.json").read_text().splitlines()[-1])
assert payload["wakeAgent"] is True
PY
```

No live registry mutation was made by this change.
