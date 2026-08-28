from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

from ..exceptions import MiniCoderError
from ..redaction import redact_sensitive_text
from .runner import EvalRunner
from .scenarios import all_scenarios, get_scenarios


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mini-coder-eval",
        description="Run isolated deterministic or explicitly enabled live coding-agent evals.",
    )
    parser.add_argument(
        "--scenario",
        action="append",
        default=[],
        help="Scenario ID to run; repeat the option. Deterministic mode defaults to all.",
    )
    parser.add_argument("--list", action="store_true", help="List scenarios and exit")
    parser.add_argument(
        "--live",
        action="store_true",
        help="Use the configured real provider; requires at least one explicit --scenario",
    )
    parser.add_argument(
        "--config",
        type=Path,
        help="Provider TOML for live mode; defaults to ./agent.toml when present",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Report directory; defaults to eval-results/<UTC timestamp>",
    )
    parser.add_argument(
        "--keep-workspaces",
        action="store_true",
        help="Preserve every isolated workspace; failed cases are always preserved",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.list:
        for scenario in all_scenarios():
            live = "live" if scenario.live_supported else "deterministic-only"
            print(f"{scenario.scenario_id:24} {live:18} {scenario.description}")
        return 0
    if args.live and not args.scenario:
        print(
            "Live evals require an explicit --scenario so a real provider is never used by accident.",
            file=sys.stderr,
        )
        return 2

    config_path = args.config
    if args.live and config_path is None and Path("agent.toml").is_file():
        config_path = Path("agent.toml")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output = args.output or Path("eval-results") / stamp
    try:
        scenarios = get_scenarios(args.scenario)
        runner = EvalRunner(
            output_directory=output,
            live=args.live,
            config_path=config_path,
            keep_workspaces=args.keep_workspaces,
        )
        report = runner.run(scenarios)
    except (MiniCoderError, OSError, ValueError) as exc:
        print(f"Eval configuration/runtime error: {redact_sensitive_text(str(exc))}", file=sys.stderr)
        return 2

    print(f"\nEval result: {report.passed}/{report.total} passed ({report.mode})")
    for item in report.results:
        marker = "PASS" if item.passed else "FAIL"
        print(
            f"[{marker}] {item.scenario_id}: status={item.session_status or item.result_status}, "
            f"tools={item.tool_calls}, retries={item.retries}, duration={item.duration_seconds:.2f}s"
        )
        if not item.passed:
            failed_checks = [name for name, passed in item.checks.items() if not passed]
            print("       failed checks: " + ", ".join(failed_checks))
            if item.artifact_directory:
                print("       artifacts: " + item.artifact_directory)
    print("Report: " + str((runner.output_directory / "summary.md").resolve()))
    return 0 if report.success else 1


if __name__ == "__main__":
    raise SystemExit(main())
