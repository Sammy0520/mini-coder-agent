from __future__ import annotations

import argparse
import json
import os
import subprocess
import time
from pathlib import Path


def sweagent_image(instance_id: str) -> str:
    transformed = instance_id.replace("__", "_1776_").lower()
    return f"swebench/sweb.eval.x86_64.{transformed}:latest"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Pull preregistered SWE-bench images sequentially."
    )
    parser.add_argument(
        "--manifest", type=Path, default=Path(__file__).with_name("manifest.json")
    )
    parser.add_argument("--phase", choices=["phase1", "phase2"], default="phase1")
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Maximum number of missing images to pull; 0 means all",
    )
    parser.add_argument("--retries", type=int, default=2)
    parser.add_argument("--dry-run", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    rows = sorted(
        (row for row in manifest["instances"] if row["phase"] == args.phase),
        key=lambda row: row["pair_order"],
    )
    missing: list[str] = []
    for row in rows:
        image = sweagent_image(row["instance_id"])
        exists = subprocess.run(
            ["docker", "image", "inspect", image],
            capture_output=True,
            check=False,
        ).returncode == 0
        print(f"{'HAVE' if exists else 'NEED'} {row['instance_id']} -> {image}")
        if not exists:
            missing.append(image)
    if args.dry_run:
        return 0
    selected = missing[: args.limit] if args.limit > 0 else missing
    environment = os.environ.copy()
    for image in selected:
        for attempt in range(args.retries + 1):
            print(f"PULL {image} attempt={attempt + 1}", flush=True)
            completed = subprocess.run(
                ["docker", "pull", image], env=environment, check=False
            )
            if completed.returncode == 0:
                break
            if attempt >= args.retries:
                return completed.returncode
            time.sleep(min(2 ** attempt, 5))
    print(f"Downloaded {len(selected)} image(s); {len(missing) - len(selected)} still missing.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
