from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from .runner import BenchmarkRunner


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mini-coder-bench",
        description="Compare Mini Coder and Codex on isolated hidden-test tasks.",
    )
    parser.add_argument("--manifest", type=Path, default=Path("benchmarks/manifest.json"))
    parser.add_argument("--config", type=Path, default=Path("agent.toml"))
    parser.add_argument("--output", type=Path)
    parser.add_argument("--task", action="append", default=[])
    parser.add_argument("--agent", choices=["mini", "codex", "both"], default="both")
    parser.add_argument("--codex-executable", default="codex")
    parser.add_argument("--list", action="store_true")
    parser.add_argument(
        "--live",
        action="store_true",
        help="Required acknowledgement because runs consume the configured aicode007 quota.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    agents = ("mini", "codex") if args.agent == "both" else (args.agent,)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output = args.output or Path("benchmark-results") / stamp
    runner = BenchmarkRunner(
        manifest=args.manifest,
        output_directory=output,
        mini_config=args.config,
        agents=agents,
        selected_tasks=tuple(args.task),
        codex_executable=args.codex_executable,
    )
    try:
        comparison, tasks = runner.load()
        if args.list:
            print(
                json.dumps(
                    {
                        "comparison": {
                            "provider": comparison.model_provider,
                            "base_url": comparison.base_url,
                            "wire_api": comparison.wire_api,
                            "model": comparison.model,
                            "reasoning_effort": comparison.reasoning_effort,
                            "verbosity": comparison.verbosity,
                        },
                        "tasks": [
                            {
                                "id": item.task_id,
                                "category": item.category,
                                "turns": len(item.turns),
                                "title": item.title,
                            }
                            for item in tasks
                        ],
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0
        if not args.live:
            print("Refusing to spend API quota without explicit --live.", file=sys.stderr)
            return 2
        report = runner.run()
    except (OSError, ValueError, json.JSONDecodeError, KeyError) as exc:
        print(f"Benchmark configuration/runtime error: {exc}", file=sys.stderr)
        return 2
    passed = sum(item.passed for item in report.results)
    print(f"Benchmark result: {passed}/{len(report.results)} passed")
    print("Report: " + str((runner.output_directory / "summary.md").resolve()))
    return 0 if passed == len(report.results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
