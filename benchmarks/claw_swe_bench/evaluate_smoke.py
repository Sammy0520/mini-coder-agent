from __future__ import annotations

import argparse
import json
import os
import subprocess
from pathlib import Path

from benchmarks.claw_swe_bench.evaluate_experiment import (
    DEFAULT_SWEBENCH_SOURCE,
    _load_official_report,
    _swebench_environment,
    _verify_swebench_source,
)
from benchmarks.claw_swe_bench.run_experiment import _sha256
from benchmarks.claw_swe_bench.run_smoke import (
    EXPECTED_PARQUET_SHA256,
    SMOKE_INSTANCE_ID,
)

def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Score excluded smoke predictions with the official SWE-bench harness."
    )
    parser.add_argument("--claw-root", type=Path, required=True)
    parser.add_argument("--parquet", type=Path, required=True)
    parser.add_argument(
        "--swebench-python",
        type=Path,
        default=Path("/home/sammy/minicoder-eval/swe-bench-env/bin/python"),
    )
    parser.add_argument(
        "--swebench-source", type=Path, default=DEFAULT_SWEBENCH_SOURCE
    )
    parser.add_argument(
        "--mini-run-id", default="minicoder-claw-smoke-v3-mini"
    )
    parser.add_argument(
        "--codex-run-id", default="minicoder-claw-smoke-v4-codex"
    )
    parser.add_argument("--timeout", type=int, default=1800)
    parser.add_argument("--evaluation-suffix", default="official-eval")
    parser.add_argument("--dry-run", action="store_true")
    return parser


def _prediction_is_nonempty(path: Path) -> bool:
    if not path.is_file():
        return False
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        if not line.strip():
            continue
        data = json.loads(line)
        if (
            data.get("instance_id") == SMOKE_INSTANCE_ID
            and str(data.get("model_patch") or "").strip()
        ):
            return True
    return False


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if _sha256(args.parquet) != EXPECTED_PARQUET_SHA256:
        raise RuntimeError("Lite parquet does not match the pinned dataset revision")
    if not args.swebench_python.is_file():
        raise FileNotFoundError(f"official SWE-bench Python not found: {args.swebench_python}")
    environment = _swebench_environment(args.swebench_source)
    _verify_swebench_source(
        args.swebench_python, args.swebench_source, environment
    )

    run_ids = (args.mini_run_id, args.codex_run_id)
    predictions = {
        run_id: args.claw_root / "artifacts" / run_id / "predictions.jsonl"
        for run_id in run_ids
    }
    for run_id, path in predictions.items():
        if not _prediction_is_nonempty(path):
            raise RuntimeError(f"missing non-empty smoke prediction: {run_id}")

    print("Official smoke evaluation jobs:")
    for run_id in run_ids:
        print(f"- {run_id} -> {run_id}-{args.evaluation_suffix}")
    if args.dry_run:
        return 0

    from datasets import load_dataset

    dataset = load_dataset(
        "parquet", data_files=str(args.parquet.resolve()), split="train"
    )
    matching = [dict(row) for row in dataset if row["instance_id"] == SMOKE_INSTANCE_ID]
    if len(matching) != 1:
        raise RuntimeError(f"smoke instance not found exactly once: {SMOKE_INSTANCE_ID}")
    artifact_dir = args.claw_root / "artifacts" / "minicoder-claw-smoke-official-eval"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    exact_dataset = artifact_dir / f"{SMOKE_INSTANCE_ID}.json"
    exact_dataset.write_text(
        json.dumps(matching, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    work_dir = Path(
        os.environ.get("SWEBENCH_WORK_DIR", "/home/sammy/minicoder-eval/swe-bench-work")
    )
    work_dir.mkdir(parents=True, exist_ok=True)

    failures = 0
    for run_id in run_ids:
        evaluation_run = f"{run_id}-{args.evaluation_suffix}"
        command = [
            str(args.swebench_python),
            "-m",
            "swebench.harness.run_evaluation",
            "--dataset_name",
            str(exact_dataset),
            "--predictions_path",
            str(predictions[run_id]),
            "--max_workers",
            "1",
            "--timeout",
            str(args.timeout),
            "--run_id",
            evaluation_run,
            "--instance_ids",
            SMOKE_INSTANCE_ID,
        ]
        print(f"SCORE {run_id}", flush=True)
        completed = subprocess.run(
            command, cwd=work_dir, env=environment, check=False
        )
        if completed.returncode != 0:
            failures += 1
            print(f"FAILED evaluation job: {evaluation_run}")
            continue
        try:
            report = _load_official_report(
                work_dir, evaluation_run, predictions[run_id]
            )
        except RuntimeError as exc:
            failures += 1
            print(f"FAILED evaluation report: {exc}")
            continue
        if SMOKE_INSTANCE_ID not in set(report.get("resolved_ids") or []):
            failures += 1
            print(f"UNRESOLVED smoke prediction: {run_id}")
            continue
        print(f"RESOLVED smoke prediction: {run_id}", flush=True)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
