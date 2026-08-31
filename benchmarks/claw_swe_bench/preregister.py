from __future__ import annotations

import argparse
import hashlib
import json
from collections import defaultdict
from pathlib import Path
from typing import Any


DATASET_REPOSITORY = "princeton-nlp/SWE-bench_Verified"
DATASET_REVISION = "c104f840cc67f8b6eec6f759ebc8b2693d585d4a"
VERIFIED_PARQUET_SHA256 = "a45b1fe4e2f0c8390b2b2938ac83e92ed5979000856808f3679c07812e9e6dcd"
EASY_DIFFICULTY = "<15 min fix"
EASY_POPULATION_SIZE = 194
SELECTION_SEED = "7725d67d9825b931a2c91b832eb2fff3d3995d2d"
SELECTION_NAMESPACE = "mini-coder-agent-swe-bench-verified-easy-v1"
PHASE_SIZE = 8


class PreregistrationError(ValueError):
    pass


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_verified_easy(path: Path) -> list[dict[str, str]]:
    if _file_sha256(path) != VERIFIED_PARQUET_SHA256:
        raise PreregistrationError(
            "Verified parquet does not match the preregistered official revision"
        )
    from datasets import load_dataset

    dataset = load_dataset("parquet", data_files=str(path.resolve()), split="train")
    rows = [
        {
            "instance_id": str(row["instance_id"]),
            "repo": str(row["repo"]),
            "language": "Python",
            "difficulty": str(row["difficulty"]),
            "source_dataset": "verified-easy",
        }
        for row in dataset
        if row.get("difficulty") == EASY_DIFFICULTY
    ]
    if len(rows) != EASY_POPULATION_SIZE:
        raise PreregistrationError(
            f"expected {EASY_POPULATION_SIZE} official Easy instances, got {len(rows)}"
        )
    if len({row["instance_id"] for row in rows}) != len(rows):
        raise PreregistrationError("Verified Easy population contains duplicate IDs")
    return rows


def build_manifest(rows: list[dict[str, str]], *, created_at: str) -> dict[str, Any]:
    by_repo: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        if row.get("difficulty") != EASY_DIFFICULTY:
            raise PreregistrationError("selection input contains a non-Easy task")
        by_repo[row["repo"]].append(row)
    if len(by_repo) < PHASE_SIZE:
        raise PreregistrationError(
            f"selection requires at least {PHASE_SIZE} repositories"
        )

    selected: list[dict[str, Any]] = []
    selected_ids: set[str] = set()
    for phase in ("phase1", "phase2"):
        ranked_repos = sorted(
            by_repo,
            key=lambda repo: _sha256_text(
                f"{SELECTION_NAMESPACE}|repo|{phase}|{SELECTION_SEED}|{repo}"
            ),
        )
        phase_rows: list[dict[str, Any]] = []
        for repo in ranked_repos:
            candidates = sorted(
                (
                    row
                    for row in by_repo[repo]
                    if row["instance_id"] not in selected_ids
                ),
                key=lambda row: _sha256_text(
                    f"{SELECTION_NAMESPACE}|select|{phase}|{SELECTION_SEED}|"
                    f"{row['instance_id']}"
                ),
            )
            if not candidates:
                continue
            row = dict(candidates[0])
            row["phase"] = phase
            row["selection_hash"] = _sha256_text(
                f"{SELECTION_NAMESPACE}|select|{phase}|{SELECTION_SEED}|"
                f"{row['instance_id']}"
            )
            row["order_hash"] = _sha256_text(
                f"{SELECTION_NAMESPACE}|order|{phase}|{SELECTION_SEED}|"
                f"{row['instance_id']}"
            )
            phase_rows.append(row)
            selected_ids.add(row["instance_id"])
            if len(phase_rows) == PHASE_SIZE:
                break
        if len(phase_rows) != PHASE_SIZE:
            raise PreregistrationError(f"could not select {PHASE_SIZE} tasks for {phase}")
        phase_rows.sort(key=lambda row: row["order_hash"])
        for pair_order, row in enumerate(phase_rows, start=1):
            row["pair_order"] = pair_order
            row["first_agent"] = "mini" if pair_order <= PHASE_SIZE // 2 else "codex"
        selected.extend(phase_rows)

    return {
        "schema_version": 2,
        "created_at": created_at,
        "status": "preregistered_before_model_runs",
        "dataset": {
            "repository": DATASET_REPOSITORY,
            "revision": DATASET_REVISION,
            "config": "default",
            "split": "test",
            "parquet_sha256": VERIFIED_PARQUET_SHA256,
            "difficulty": EASY_DIFFICULTY,
            "population_size": EASY_POPULATION_SIZE,
        },
        "selection": {
            "namespace": SELECTION_NAMESPACE,
            "seed_public_git_commit": SELECTION_SEED,
            "rule": (
                "For each phase, rank repositories by SHA-256(namespace|repo|phase|seed|repo), "
                "then select one previously unused Easy task from each of the first eight "
                "repositories using SHA-256(namespace|select|phase|seed|instance_id). Order "
                "the eight pairs by an independent SHA-256 order hash; Mini Coder runs first "
                "for the first four pairs and Codex first for the remaining four."
            ),
            "selected_size": PHASE_SIZE * 2,
            "phase1_size": PHASE_SIZE,
            "phase2_size": PHASE_SIZE,
        },
        "instances": selected,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Generate the immutable SWE-bench Verified Easy manifest."
    )
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--created-at", default="2026-08-31T00:00:00+08:00")
    parser.add_argument("--check", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    manifest = build_manifest(load_verified_easy(args.source), created_at=args.created_at)
    rendered = json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
    if args.check:
        if not args.output.is_file() or args.output.read_text(encoding="utf-8") != rendered:
            raise PreregistrationError("tracked manifest does not match deterministic output")
        print(f"Verified {args.output} ({len(manifest['instances'])} selected instances)")
        return 0
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(rendered, encoding="utf-8")
    print(f"Wrote {args.output} ({len(manifest['instances'])} selected instances)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
