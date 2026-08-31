#!/usr/bin/env bash
set -uo pipefail

PROJECT_ROOT="${1:-/mnt/d/Code Agent}"
EVAL_ROOT="${MINICODER_EVAL_ROOT:-/home/sammy/minicoder-eval}"
PYTHON_BIN="${EVAL_ROOT}/runtime-env/bin/python"
CLAW_ROOT="${EVAL_ROOT}/claw-swe-bench"
PARQUET="${PROJECT_ROOT}/tmp/claw-dataset-cache/snapshot/data/lite-test.parquet"
AUTH_FILE="${PROJECT_ROOT}/auth.json"
LOG_ROOT="${EVAL_ROOT}/overnight"
STATUS_FILE="${LOG_ROOT}/phase1.status"

mkdir -p "${LOG_ROOT}"
export HTTP_PROXY="${HTTP_PROXY:-http://172.31.0.1:7897}"
export HTTPS_PROXY="${HTTPS_PROXY:-http://172.31.0.1:7897}"
export CLAW_AGENT_PROXY="${CLAW_AGENT_PROXY:-http://host.docker.internal:7897}"
export SWEBENCH_WORK_DIR="${SWEBENCH_WORK_DIR:-${EVAL_ROOT}/swe-bench-work}"

cd "${PROJECT_ROOT}"
printf 'started %s\n' "$(date --iso-8601=seconds)" > "${STATUS_FILE}"

for pair in 1 2 3 4 5 6 7 8; do
  printf 'pair %s pulling %s\n' "${pair}" "$(date --iso-8601=seconds)" | tee -a "${STATUS_FILE}"
  if ! "${PYTHON_BIN}" -m benchmarks.claw_swe_bench.pull_images \
      --phase phase1 --pair "${pair}" --retries 5; then
    printf 'pair %s pull_failed %s\n' "${pair}" "$(date --iso-8601=seconds)" | tee -a "${STATUS_FILE}"
    exit 20
  fi

  printf 'pair %s running %s\n' "${pair}" "$(date --iso-8601=seconds)" | tee -a "${STATUS_FILE}"
  if ! "${PYTHON_BIN}" -m benchmarks.claw_swe_bench.run_experiment \
      --claw-root "${CLAW_ROOT}" \
      --parquet "${PARQUET}" \
      --auth-file "${AUTH_FILE}" \
      --phase phase1 --pair "${pair}" --agent both; then
    printf 'pair %s run_failed %s\n' "${pair}" "$(date --iso-8601=seconds)" | tee -a "${STATUS_FILE}"
    exit 30
  fi
  printf 'pair %s complete %s\n' "${pair}" "$(date --iso-8601=seconds)" | tee -a "${STATUS_FILE}"
done

printf 'scoring %s\n' "$(date --iso-8601=seconds)" | tee -a "${STATUS_FILE}"
if ! "${PYTHON_BIN}" -m benchmarks.claw_swe_bench.evaluate_experiment \
    --claw-root "${CLAW_ROOT}" \
    --parquet "${PARQUET}" \
    --swebench-python "${EVAL_ROOT}/swe-bench-env/bin/python" \
    --phase phase1 --agent both; then
  printf 'scoring_failed %s\n' "$(date --iso-8601=seconds)" | tee -a "${STATUS_FILE}"
  exit 40
fi

printf 'complete %s\n' "$(date --iso-8601=seconds)" | tee -a "${STATUS_FILE}"
