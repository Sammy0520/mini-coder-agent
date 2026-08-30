from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


DATASET_REPOSITORY = "TokenRhythm/Claw-SWE-Bench"
DATASET_REVISION = "ca9da7416154a31015f43df71dcf742c6725b312"
LITE_IDS_SHA256 = "09738eeb71e7fc4b2f2511da963c5cbd47b503a9cccba30cc561703d7003766f"
LITE_PARQUET_SHA256 = "40fd4e1f9ac40c11c38ac68113b9b5b2026ae916a11d8ade39b40afd4adf0412"
SELECTION_SEED = "7725d67d9825b931a2c91b832eb2fff3d3995d2d"
SELECTION_NAMESPACE = "mini-coder-agent-claw-v1"
LANGUAGES = ("Java", "Go", "Rust", "JS/TS", "C/C++", "Ruby", "PHP", "Python")


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


def load_official_lite_ids(path: Path) -> list[dict[str, str]]:
    if _file_sha256(path) != LITE_IDS_SHA256:
        raise PreregistrationError(
            "lite80_ids.json does not match the preregistered official revision"
        )
    data = json.loads(path.read_text(encoding="utf-8"))
    rows = data.get("instances") if isinstance(data, dict) else None
    if not isinstance(rows, list) or len(rows) != 80:
        raise PreregistrationError("official Lite manifest must contain exactly 80 instances")

    normalized: list[dict[str, str]] = []
    for row in rows:
        if not isinstance(row, dict):
            raise PreregistrationError("Lite manifest contains a non-object instance")
        selected = {
            key: row.get(key)
            for key in ("instance_id", "language", "repo", "source_dataset")
        }
        if not all(isinstance(value, str) and value for value in selected.values()):
            raise PreregistrationError("Lite manifest contains incomplete public metadata")
        normalized.append(selected)  # type: ignore[arg-type]

    if len({row["instance_id"] for row in normalized}) != 80:
        raise PreregistrationError("Lite manifest contains duplicate instance IDs")
    counts = Counter(row["language"] for row in normalized)
    if counts != Counter({language: 10 for language in LANGUAGES}):
        raise PreregistrationError(f"unexpected Lite language distribution: {dict(counts)}")
    return normalized


def build_manifest(rows: list[dict[str, str]], *, created_at: str) -> dict[str, Any]:
    by_language: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        by_language[row["language"]].append(row)

    selected: list[dict[str, Any]] = []
    for language in LANGUAGES:
        ranked = sorted(
            by_language[language],
            key=lambda row: _sha256_text(
                f"{SELECTION_NAMESPACE}|select|{SELECTION_SEED}|{row['instance_id']}"
            ),
        )
        for rank, row in enumerate(ranked[:2], start=1):
            item = dict(row)
            item["selection_rank_within_language"] = rank
            item["phase"] = f"phase{rank}"
            item["selection_hash"] = _sha256_text(
                f"{SELECTION_NAMESPACE}|select|{SELECTION_SEED}|{row['instance_id']}"
            )
            item["order_hash"] = _sha256_text(
                f"{SELECTION_NAMESPACE}|order|{SELECTION_SEED}|{row['instance_id']}"
            )
            selected.append(item)

    for phase in ("phase1", "phase2"):
        phase_rows = sorted(
            (row for row in selected if row["phase"] == phase),
            key=lambda row: row["order_hash"],
        )
        for pair_order, row in enumerate(phase_rows, start=1):
            row["pair_order"] = pair_order
            row["first_agent"] = "mini" if pair_order <= 4 else "codex"

    selected.sort(key=lambda row: (row["phase"], row["pair_order"]))
    return {
        "schema_version": 1,
        "created_at": created_at,
        "status": "preregistered_before_model_runs",
        "dataset": {
            "repository": DATASET_REPOSITORY,
            "revision": DATASET_REVISION,
            "config": "lite",
            "split": "test",
            "lite80_ids_sha256": LITE_IDS_SHA256,
            "lite_test_parquet_sha256": LITE_PARQUET_SHA256,
            "population_size": 80,
        },
        "selection": {
            "namespace": SELECTION_NAMESPACE,
            "seed_public_git_commit": SELECTION_SEED,
            "rule": (
                "For each of the eight official languages, sort the ten Lite instance IDs "
                "by SHA-256(namespace|select|seed|instance_id) and select the first two. "
                "Rank 1 is phase1 and rank 2 is phase2. Within each phase, order by "
                "SHA-256(namespace|order|seed|instance_id); Mini Coder runs first for the "
                "first four pairs and Codex first for the remaining four."
            ),
            "selected_size": 16,
            "phase1_size": 8,
            "phase2_size": 8,
        },
        "instances": selected,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Generate the immutable Claw-SWE-Bench Lite pilot manifest."
    )
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--created-at", default="2026-08-31T00:00:00+08:00")
    parser.add_argument("--check", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    manifest = build_manifest(load_official_lite_ids(args.source), created_at=args.created_at)
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
