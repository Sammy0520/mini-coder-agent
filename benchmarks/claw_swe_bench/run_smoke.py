from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from benchmarks.claw_swe_bench.run_experiment import _load_key, _sha256


SMOKE_INSTANCE_ID = "django__django-11790"
EXPECTED_PARQUET_SHA256 = "40fd4e1f9ac40c11c38ac68113b9b5b2026ae916a11d8ade39b40afd4adf0412"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run an excluded Claw-SWE-Bench smoke instance sequentially."
    )
    parser.add_argument("--claw-root", type=Path, required=True)
    parser.add_argument("--parquet", type=Path, required=True)
    parser.add_argument("--auth-file", type=Path)
    parser.add_argument("--agent", choices=["mini", "codex", "both"], default="both")
    parser.add_argument("--model", default="gpt-5.6-sol")
    parser.add_argument("--base-url", default="https://api.aicode007.com")
    parser.add_argument("--reasoning-effort", default="xhigh")
    parser.add_argument("--verbosity", default="high")
    parser.add_argument("--timeout", type=int, default=1200)
    parser.add_argument("--max-turns", type=int, default=14)
    parser.add_argument("--run-prefix", default="minicoder-claw-smoke-v1")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if _sha256(args.parquet) != EXPECTED_PARQUET_SHA256:
        raise RuntimeError("Lite parquet does not match the pinned dataset revision")
    _load_key(args.auth_file)
    if not os.environ.get("OPENAI_API_KEY"):
        raise RuntimeError("set OPENAI_API_KEY or pass --auth-file before a model run")

    claw_root = args.claw_root.resolve()
    sys.path.insert(0, str(claw_root))
    from datasets import load_dataset
    from claw_swebench.orchestrator import run_one_instance
    from benchmarks.claw_swe_bench.adapters import CodexAdapter, MiniCoderAdapter

    dataset = load_dataset(
        "parquet", data_files=str(args.parquet.resolve()), split="train"
    )
    matching = [dict(row) for row in dataset if row["instance_id"] == SMOKE_INSTANCE_ID]
    if len(matching) != 1:
        raise RuntimeError(f"smoke instance not found exactly once: {SMOKE_INSTANCE_ID}")
    instance = matching[0]
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
    order = ("mini", "codex") if args.agent == "both" else (args.agent,)
    summaries: list[dict[str, object]] = []
    for selected_agent in order:
        run_id = f"{args.run_prefix}-{selected_agent}"
        artifact = claw_root / "artifacts" / run_id / SMOKE_INSTANCE_ID / "metadata.json"
        if artifact.is_file():
            print(f"SKIP existing smoke attempt: {selected_agent}", flush=True)
            metadata = json.loads(artifact.read_text(encoding="utf-8"))
        else:
            print(f"SMOKE RUN {selected_agent} {SMOKE_INSTANCE_ID}", flush=True)
            record = run_one_instance(
                instance=instance,
                adapter=adapters[selected_agent],
                model_name=args.model,
                run_id=run_id,
                setup_gitignore=False,
            )
            print(
                f"SMOKE DONE {selected_agent}: state={record.state.value} "
                f"empty={record.patch_empty} seconds={record.duration_seconds}",
                flush=True,
            )
            metadata = json.loads(artifact.read_text(encoding="utf-8"))
        summary = {
            "agent": selected_agent,
            "state": metadata.get("state"),
            "patch_empty": metadata.get("patch_empty"),
            "duration_seconds": metadata.get("duration_seconds"),
            "usage": (metadata.get("agent") or {}).get("usage", {}),
            "agent_success": (metadata.get("agent") or {}).get("success"),
        }
        summaries.append(summary)
        if (
            summary["patch_empty"]
            or summary["state"] != "patch_collected"
            or summary["agent_success"] is not True
        ):
            print("Smoke stopped before the next agent because this attempt was unhealthy.")
            print(json.dumps(summaries, ensure_ascii=False, indent=2))
            return 2
    print(json.dumps(summaries, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
