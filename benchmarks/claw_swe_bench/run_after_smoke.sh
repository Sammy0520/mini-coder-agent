#!/usr/bin/env bash
set -uo pipefail

SMOKE_PID="${1:?usage: run_after_smoke.sh SMOKE_PID [PROJECT_ROOT]}"
PROJECT_ROOT="${2:-/mnt/d/Code Agent}"
EVAL_ROOT="${MINICODER_EVAL_ROOT:-/home/sammy/minicoder-eval}"
CLAW_ROOT="${EVAL_ROOT}/claw-swe-bench"
LOG_ROOT="${EVAL_ROOT}/overnight"
STATUS_FILE="${LOG_ROOT}/coordinator.status"
LOCK_FILE="${LOG_ROOT}/phase1.lock"
PYTHON_BIN="${EVAL_ROOT}/runtime-env/bin/python"

mkdir -p "${LOG_ROOT}"
exec 9>"${LOCK_FILE}"
if ! flock -n 9; then
  printf 'another coordinator already owns %s\n' "${LOCK_FILE}" | tee -a "${STATUS_FILE}"
  exit 10
fi

printf 'waiting_for_smoke pid=%s %s\n' "${SMOKE_PID}" "$(date --iso-8601=seconds)" > "${STATUS_FILE}"
while kill -0 "${SMOKE_PID}" 2>/dev/null; do
  sleep 20
done

if ! "${PYTHON_BIN}" - "${CLAW_ROOT}" <<'PY'
import json
import sys
from pathlib import Path

root = Path(sys.argv[1]) / "artifacts"
instance = "django__django-11790"
for agent in ("mini", "codex"):
    path = root / f"minicoder-claw-smoke-v1-{agent}" / instance / "metadata.json"
    if not path.is_file():
        raise SystemExit(f"missing smoke metadata: {agent}")
    data = json.loads(path.read_text(encoding="utf-8"))
    agent_data = data.get("agent") or {}
    if (
        data.get("state") != "patch_collected"
        or data.get("patch_empty") is not False
        or agent_data.get("success") is not True
    ):
        raise SystemExit(f"unhealthy smoke result: {agent}")
print("smoke artifacts healthy")
PY
then
  printf 'smoke_failed %s\n' "$(date --iso-8601=seconds)" | tee -a "${STATUS_FILE}"
  exit 20
fi

if ! "${PYTHON_BIN}" -m benchmarks.claw_swe_bench.evaluate_smoke \
    --claw-root "${CLAW_ROOT}" \
    --parquet "${PROJECT_ROOT}/tmp/claw-dataset-cache/snapshot/data/lite-test.parquet" \
    --swebench-python "${EVAL_ROOT}/swe-bench-env/bin/python" \
    --swebench-source "${EVAL_ROOT}/swe-bench-v4.1.0" \
    --mini-run-id minicoder-claw-smoke-v1-mini \
    --codex-run-id minicoder-claw-smoke-v1-codex \
    --evaluation-suffix overnight-gate; then
  printf 'official_smoke_failed %s\n' "$(date --iso-8601=seconds)" | tee -a "${STATUS_FILE}"
  exit 30
fi

printf 'starting_phase1 %s\n' "$(date --iso-8601=seconds)" | tee -a "${STATUS_FILE}"
bash "${PROJECT_ROOT}/benchmarks/claw_swe_bench/run_overnight.sh" "${PROJECT_ROOT}"
exit_code=$?
printf 'phase1_exit=%s %s\n' "${exit_code}" "$(date --iso-8601=seconds)" | tee -a "${STATUS_FILE}"
exit "${exit_code}"
