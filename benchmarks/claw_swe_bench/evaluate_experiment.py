from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
from pathlib import Path
from typing import Any


EXPECTED_PARQUET_SHA256 = "40fd4e1f9ac40c11c38ac68113b9b5b2026ae916a11d8ade39b40afd4adf0412"
DATASET_NAMES = {
    "multilingual": "SWE-bench/SWE-bench_Multilingual",
    "verified-mini": "princeton-nlp/SWE-bench_Verified",
}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Score preregistered predictions with the official SWE-bench harness."
    )
    parser.add_argument("--claw-root", type=Path, required=True)
    parser.add_argument("--parquet", type=Path, required=True)
    parser.add_argument(
        "--manifest", type=Path, default=Path(__file__).with_name("manifest.json")
    )
    parser.add_argument(
        "--swebench-python",
        type=Path,
        default=Path("/home/sammy/minicoder-eval/swe-bench-env/bin/python"),
    )
    parser.add_argument("--phase", choices=["phase1", "phase2"], default="phase1")
    parser.add_argument("--agent", choices=["mini", "codex", "both"], default="both")
    parser.add_argument("--run-prefix", default="minicoder-claw-lite-v1")
    parser.add_argument("--timeout", type=int, default=1800)
    parser.add_argument("--dry-run", action="store_true")
    return parser


def _jobs(
    manifest: dict[str, Any], *, phase: str, agent: str, run_prefix: str
) -> list[dict[str, Any]]:
    agents = ("mini", "codex") if agent == "both" else (agent,)
    rows = [row for row in manifest["instances"] if row["phase"] == phase]
    jobs: list[dict[str, Any]] = []
    for selected_agent in agents:
        for source, dataset_name in DATASET_NAMES.items():
            instance_ids = [
                row["instance_id"] for row in rows if row["source_dataset"] == source
            ]
            if not instance_ids:
                continue
            inference_run = f"{run_prefix}-{phase}-{selected_agent}"
            jobs.append(
                {
                    "agent": selected_agent,
                    "source_dataset": source,
                    "dataset_name": dataset_name,
                    "instance_ids": instance_ids,
                    "inference_run": inference_run,
                    "evaluation_run": f"{inference_run}-{source}-eval",
                }
            )
    return jobs


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if _sha256(args.parquet) != EXPECTED_PARQUET_SHA256:
        raise RuntimeError("Lite parquet does not match the preregistered dataset revision")
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    jobs = _jobs(
        manifest, phase=args.phase, agent=args.agent, run_prefix=args.run_prefix
    )
    print("Official SWE-bench evaluation jobs:")
    for job in jobs:
        print(
            f"- {job['agent']} {job['source_dataset']}: "
            f"{len(job['instance_ids'])} instance(s), run={job['evaluation_run']}"
        )
    if args.dry_run:
        return 0
    if not args.swebench_python.is_file():
        raise FileNotFoundError(f"official SWE-bench Python not found: {args.swebench_python}")

    from datasets import load_dataset

    dataset = load_dataset(
        "parquet", data_files=str(args.parquet.resolve()), split="train"
    )
    rows = {str(row["instance_id"]): dict(row) for row in dataset}
    exact_dataset_dir = args.claw_root / "artifacts" / f"{args.run_prefix}-pinned-datasets"
    exact_dataset_dir.mkdir(parents=True, exist_ok=True)
    work_dir = Path(
        os.environ.get("SWEBENCH_WORK_DIR", "/home/sammy/minicoder-eval/swe-bench-work")
    )
    work_dir.mkdir(parents=True, exist_ok=True)

    failures = 0
    for job in jobs:
        predictions = (
            args.claw_root / "artifacts" / job["inference_run"] / "predictions.jsonl"
        )
        if not predictions.is_file():
            raise FileNotFoundError(f"prediction file not found: {predictions}")
        exact_dataset = exact_dataset_dir / (
            f"{args.phase}-{job['source_dataset']}.json"
        )
        exact_rows = [rows[instance_id] for instance_id in job["instance_ids"]]
        exact_dataset.write_text(
            json.dumps(exact_rows, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        command = [
            str(args.swebench_python),
            "-m", "swebench.harness.run_evaluation",
            "--dataset_name", str(exact_dataset),
            "--predictions_path", str(predictions),
            "--max_workers", "1",
            "--timeout", str(args.timeout),
            "--run_id", job["evaluation_run"],
            "--instance_ids", *job["instance_ids"],
        ]
        print(f"SCORE {job['agent']} {job['source_dataset']}", flush=True)
        completed = subprocess.run(command, cwd=work_dir, check=False)
        if completed.returncode != 0:
            failures += 1
            print(f"FAILED evaluation job: {job['evaluation_run']}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
