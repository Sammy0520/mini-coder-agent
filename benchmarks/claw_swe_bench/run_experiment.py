from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any


EXPECTED_PARQUET_SHA256 = "a45b1fe4e2f0c8390b2b2938ac83e92ed5979000856808f3679c07812e9e6dcd"
EXPECTED_CLAW_REVISION = "fcece5f4c0817430ce953b52c80c931a40cd9b83"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_key(auth_file: Path | None) -> None:
    if os.environ.get("OPENAI_API_KEY"):
        return
    if auth_file is None or not auth_file.is_file():
        return
    data = json.loads(auth_file.read_text(encoding="utf-8-sig"))
    key = data.get("OPENAI_API_KEY") or data.get("CODING_AGENT_API_KEY")
    if isinstance(key, str) and key.strip():
        os.environ["OPENAI_API_KEY"] = key.strip()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the preregistered SWE-bench Verified Easy pilot sequentially."
    )
    parser.add_argument("--claw-root", type=Path, required=True)
    parser.add_argument("--parquet", type=Path, required=True)
    parser.add_argument(
        "--manifest",
        type=Path,
        default=Path(__file__).with_name("manifest.json"),
    )
    parser.add_argument("--auth-file", type=Path)
    parser.add_argument("--phase", choices=["phase1", "phase2", "all"], default="phase1")
    parser.add_argument(
        "--pair",
        type=int,
        choices=range(1, 9),
        help="Run only one preregistered pair within the selected phase",
    )
    parser.add_argument("--agent", choices=["mini", "codex", "both"], default="both")
    parser.add_argument("--model", default="gpt-5.6-sol")
    parser.add_argument("--base-url", default="https://api.aicode007.com")
    parser.add_argument("--reasoning-effort", default="xhigh")
    parser.add_argument("--verbosity", default="high")
    parser.add_argument("--timeout", type=int, default=1800)
    parser.add_argument("--max-turns", type=int, default=300)
    parser.add_argument("--run-prefix", default="minicoder-swe-verified-easy-v1")
    parser.add_argument(
        "--skip-provider-preflight",
        action="store_true",
        help="Skip the minimal billing/auth model request (not recommended for formal runs)",
    )
    parser.add_argument("--provider-preflight-timeout", type=float, default=45.0)
    parser.add_argument(
        "--allow-unrestricted-agent-network",
        action="store_true",
        help="Disable the formal-run egress allowlist (results must be marked non-comparable)",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--preflight",
        action="store_true",
        help="Check runtimes, Docker, framework revision and selected images without model calls",
    )
    return parser


def _annotate_attempt(
    metadata_path: Path,
    *,
    outcome_class: str,
    detail: str | None = None,
) -> None:
    if not metadata_path.is_file():
        return
    data = json.loads(metadata_path.read_text(encoding="utf-8"))
    data["outcome_class"] = outcome_class
    if detail:
        data["outcome_detail"] = detail
    metadata_path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def _purge_infrastructure_attempt(metadata_path: Path, instance_id: str) -> None:
    run_directory = metadata_path.parent.parent
    shutil.rmtree(metadata_path.parent)
    for name in ("state.jsonl", "predictions.jsonl"):
        path = run_directory / name
        if not path.is_file():
            continue
        kept: list[str] = []
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            if not line.strip():
                continue
            item = json.loads(line)
            if str(item.get("instance_id") or "") != instance_id:
                kept.append(json.dumps(item, ensure_ascii=False))
        path.write_text(
            "".join(line + "\n" for line in kept), encoding="utf-8"
        )


def _schedule(
    manifest: dict[str, Any],
    phase: str,
    agent: str,
    pair: int | None = None,
) -> list[dict[str, Any]]:
    rows = [
        row
        for row in manifest["instances"]
        if (phase == "all" or row["phase"] == phase)
        and (pair is None or row["pair_order"] == pair)
    ]
    rows.sort(key=lambda row: (row["phase"], row["pair_order"]))
    schedule: list[dict[str, Any]] = []
    for row in rows:
        if agent == "both":
            order = [row["first_agent"], "codex" if row["first_agent"] == "mini" else "mini"]
        else:
            order = [agent]
        for position, selected_agent in enumerate(order, start=1):
            schedule.append({**row, "agent": selected_agent, "within_pair": position})
    return schedule


def _print_schedule(schedule: list[dict[str, Any]]) -> None:
    print("Sequential preregistered schedule (no model calls in --dry-run):")
    for index, row in enumerate(schedule, start=1):
        print(
            f"{index:02d}. {row['phase']} pair {row['pair_order']} "
            f"{row['agent']:<5} {row['language']:<6} {row['instance_id']}"
        )


def _preflight(args: argparse.Namespace, schedule: list[dict[str, Any]]) -> int:
    claw_root = args.claw_root.resolve()
    revision = subprocess.run(
        ["git", "-C", str(claw_root), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    actual_revision = revision.stdout.strip()
    if revision.returncode != 0 or actual_revision != EXPECTED_CLAW_REVISION:
        raise RuntimeError(
            f"Claw framework must be pinned to {EXPECTED_CLAW_REVISION}; got {actual_revision!r}"
        )
    docker = subprocess.run(
        ["docker", "info"], capture_output=True, text=True, check=False
    )
    if docker.returncode != 0:
        raise RuntimeError("Docker engine is unavailable")
    sys.path.insert(0, str(claw_root))
    from claw_swebench.config import instance_id_to_image, instance_id_to_image_sweagent
    from benchmarks.claw_swe_bench.adapters import CodexAdapter, MiniCoderAdapter

    MiniCoderAdapter(args.model, args.timeout, args.max_turns)
    CodexAdapter(args.model, args.timeout, args.max_turns)
    missing: list[str] = []
    for instance_id in dict.fromkeys(row["instance_id"] for row in schedule):
        candidates = (instance_id_to_image(instance_id), instance_id_to_image_sweagent(instance_id))
        if not any(
            subprocess.run(
                ["docker", "image", "inspect", candidate],
                capture_output=True,
                check=False,
            ).returncode
            == 0
            for candidate in candidates
        ):
            missing.append(instance_id)
    print(f"OK Claw framework: {actual_revision}")
    print("OK Docker engine and both Linux agent runtimes")
    if missing:
        print(f"MISSING {len(missing)} selected SWE-bench images:")
        for instance_id in missing:
            print(f"  - {instance_id}")
        return 2
    print(f"OK selected SWE-bench images: {len(set(row['instance_id'] for row in schedule))}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    if manifest.get("status") != "preregistered_before_model_runs":
        raise RuntimeError("manifest is not marked as preregistered")
    if _sha256(args.parquet) != EXPECTED_PARQUET_SHA256:
        raise RuntimeError("Verified parquet does not match the preregistered dataset revision")
    if args.phase == "all" and args.pair is not None:
        raise ValueError("--pair requires --phase phase1 or --phase phase2")
    schedule = _schedule(manifest, args.phase, args.agent, args.pair)
    _print_schedule(schedule)
    if args.preflight:
        return _preflight(args, schedule)
    if args.dry_run:
        return 0

    _load_key(args.auth_file)
    if not os.environ.get("OPENAI_API_KEY"):
        raise RuntimeError("set OPENAI_API_KEY or pass --auth-file before a model run")
    if not args.skip_provider_preflight:
        from benchmarks.claw_swe_bench.support import (
            ProviderPreflightError,
            provider_preflight,
        )

        print("CHECK provider billing/auth access", flush=True)
        try:
            provider_preflight(
                api_key=os.environ["OPENAI_API_KEY"],
                base_url=args.base_url,
                model=args.model,
                timeout=args.provider_preflight_timeout,
            )
        except ProviderPreflightError as exc:
            print(f"INFRASTRUCTURE_ERROR provider_preflight {exc.category}: {exc}")
            return 50
        print("OK provider billing/auth access", flush=True)
    claw_root = args.claw_root.resolve()
    if args.allow_unrestricted_agent_network:
        print(
            "WARNING unrestricted agent network: results are not valid for the formal comparison",
            flush=True,
        )
    else:
        from benchmarks.claw_swe_bench.network_guard import ensure_network_guard

        try:
            network, proxy = ensure_network_guard(args.base_url)
        except (OSError, RuntimeError, subprocess.SubprocessError, ValueError) as exc:
            print(f"INFRASTRUCTURE_ERROR network_guard: {exc}")
            return 50
        os.environ["CLAW_AGENT_NETWORK"] = network
        os.environ["CLAW_RESTRICTED_PROXY_URL"] = proxy
        print(f"OK restricted agent egress: {args.base_url}", flush=True)
    sys.path.insert(0, str(claw_root))
    from datasets import load_dataset
    from claw_swebench.orchestrator import run_one_instance
    from benchmarks.claw_swe_bench.adapters import CodexAdapter, MiniCoderAdapter

    dataset = load_dataset(
        "parquet",
        data_files=str(args.parquet.resolve()),
        split="train",
    )
    instances = {str(row["instance_id"]): dict(row) for row in dataset}
    missing = {row["instance_id"] for row in schedule} - set(instances)
    if missing:
        raise RuntimeError(f"selected instances missing from pinned parquet: {sorted(missing)}")

    adapters = {
        "mini": MiniCoderAdapter(
            args.model,
            args.timeout,
            args.max_turns,
            base_url=args.base_url,
            reasoning_effort=args.reasoning_effort,
            verbosity=args.verbosity,
        ),
        "codex": CodexAdapter(
            args.model,
            args.timeout,
            args.max_turns,
            base_url=args.base_url,
            reasoning_effort=args.reasoning_effort,
            verbosity=args.verbosity,
        ),
    }
    for row in schedule:
        selected_agent = row["agent"]
        run_id = f"{args.run_prefix}-{row['phase']}-{selected_agent}"
        artifact = claw_root / "artifacts" / run_id / row["instance_id"] / "metadata.json"
        if artifact.is_file():
            existing = json.loads(artifact.read_text(encoding="utf-8"))
            outcome_class = existing.get("outcome_class")
            if outcome_class == "infrastructure_error":
                print(
                    f"RETRY infrastructure attempt: {selected_agent} "
                    f"{row['instance_id']}"
                )
                _purge_infrastructure_attempt(artifact, row["instance_id"])
            elif outcome_class == "integrity_violation":
                print(
                    f"INTEGRITY_ERROR existing attempt requires a new run prefix: "
                    f"{selected_agent} {row['instance_id']}"
                )
                return 51
            else:
                print(f"SKIP existing attempt: {selected_agent} {row['instance_id']}")
                continue
        print(f"RUN {selected_agent} {row['instance_id']} (strictly sequential)", flush=True)
        adapter = adapters[selected_agent]
        record = run_one_instance(
            instance=instances[row["instance_id"]],
            adapter=adapter,
            model_name=args.model,
            run_id=run_id,
            setup_gitignore=row["source_dataset"] == "multilingual",
        )
        print(
            f"DONE {selected_agent} {row['instance_id']}: "
            f"state={record.state.value} empty={record.patch_empty} "
            f"seconds={record.duration_seconds}",
            flush=True,
        )
        if adapter.last_infrastructure_error:
            _annotate_attempt(
                artifact,
                outcome_class="infrastructure_error",
                detail=adapter.last_infrastructure_error,
            )
            print(
                f"INFRASTRUCTURE_ERROR {selected_agent} {row['instance_id']}: "
                f"{adapter.last_infrastructure_error}",
                flush=True,
            )
            return 50
        if adapter.last_integrity_violations:
            _annotate_attempt(
                artifact,
                outcome_class="integrity_violation",
                detail="; ".join(adapter.last_integrity_violations),
            )
            print(
                f"INTEGRITY_ERROR {selected_agent} {row['instance_id']}: "
                "external-network command detected",
                flush=True,
            )
            for violation in adapter.last_integrity_violations:
                print(f"  - {violation}")
            return 51
        _annotate_attempt(
            artifact,
            outcome_class=(
                "candidate_patch" if not record.patch_empty else "agent_failure"
            ),
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
