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
DEFAULT_SWEBENCH_SOURCE = Path("/home/sammy/minicoder-eval/swe-bench-v4.1.0")
EXPECTED_SWEBENCH_REVISION = "726c5461e2ef52d83cf1ea2107870a8bb3328d57"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _swebench_environment(source: Path) -> dict[str, str]:
    environment = os.environ.copy()
    existing = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = (
        f"{source}{os.pathsep}{existing}" if existing else str(source)
    )
    return environment


def _verify_swebench_source(
    python: Path, source: Path, environment: dict[str, str]
) -> None:
    completed = subprocess.run(
        [
            str(python),
            "-c",
            "import pathlib, swebench; print(pathlib.Path(swebench.__file__).resolve())",
        ],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"cannot load official SWE-bench source: {completed.stderr.strip()}")
    loaded = Path(completed.stdout.strip())
    try:
        loaded.relative_to(source.resolve())
    except ValueError as exc:
        raise RuntimeError(
            f"SWE-bench source mismatch: expected {source}, loaded {loaded}"
        ) from exc
    revision = subprocess.run(
        ["git", "-C", str(source), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    if (
        revision.returncode != 0
        or revision.stdout.strip() != EXPECTED_SWEBENCH_REVISION
    ):
        raise RuntimeError(
            "official SWE-bench source must be pinned to "
            f"{EXPECTED_SWEBENCH_REVISION}; got {revision.stdout.strip()!r}"
        )


def _prediction_model_name(path: Path) -> str:
    names = {
        str(json.loads(line).get("model_name_or_path") or "").strip()
        for line in path.read_text(encoding="utf-8-sig").splitlines()
        if line.strip()
    }
    names.discard("")
    if len(names) != 1:
        raise RuntimeError(f"prediction file must contain one model name: {path}")
    return names.pop()


def _official_report_path(work_dir: Path, run_id: str, model_name: str) -> Path:
    return work_dir / f"{model_name.replace('/', '__')}.{run_id}.json"


def _load_official_report(
    work_dir: Path, run_id: str, predictions: Path
) -> dict[str, Any]:
    report_path = _official_report_path(
        work_dir, run_id, _prediction_model_name(predictions)
    )
    if not report_path.is_file():
        raise RuntimeError(f"official SWE-bench report not found: {report_path}")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    if report.get("incomplete_ids") or report.get("error_ids"):
        raise RuntimeError(f"official SWE-bench report is incomplete: {report_path}")
    return report


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
    parser.add_argument(
        "--swebench-source", type=Path, default=DEFAULT_SWEBENCH_SOURCE
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

    environment = _swebench_environment(args.swebench_source)
    _verify_swebench_source(
        args.swebench_python, args.swebench_source, environment
    )

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
        completed = subprocess.run(
            command, cwd=work_dir, env=environment, check=False
        )
        if completed.returncode != 0:
            failures += 1
            print(f"FAILED evaluation job: {job['evaluation_run']}")
            continue
        try:
            report = _load_official_report(
                work_dir, job["evaluation_run"], predictions
            )
        except RuntimeError as exc:
            failures += 1
            print(f"FAILED evaluation report: {exc}")
            continue
        print(
            f"RESULT {job['agent']} {job['source_dataset']}: "
            f"resolved={report.get('resolved_instances', 0)} "
            f"unresolved={report.get('unresolved_instances', 0)}",
            flush=True,
        )
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
