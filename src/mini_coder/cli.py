from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .agent import AgentRunner
from .config import AgentConfig, ApprovalPolicy
from .exceptions import MiniCoderError
from .model import OpenAICompatibleClient
from .tools import create_default_registry
from .tools.base import Tool


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mini-coder",
        description="Run a small local coding agent without an agent framework.",
    )
    parser.add_argument("task", nargs="*", help="Programming task; prompted when omitted")
    parser.add_argument("--workspace", default=".", help="Workspace the agent may access")
    parser.add_argument(
        "--config",
        type=Path,
        help="Provider TOML file; defaults to ./agent.toml when present",
    )
    parser.add_argument("--model", help="Override CODING_AGENT_MODEL")
    parser.add_argument("--base-url", help="Override CODING_AGENT_BASE_URL")
    parser.add_argument(
        "--wire-api",
        choices=["responses", "chat_completions"],
        help="Override provider wire_api",
    )
    parser.add_argument(
        "--reasoning-effort",
        choices=["none", "low", "medium", "high", "xhigh", "max"],
    )
    parser.add_argument("--verbosity", choices=["low", "medium", "high"])
    parser.add_argument("--max-steps", type=int, help="Maximum model turns")
    parser.add_argument(
        "--auto",
        action="store_true",
        help="Auto-approve writes and commands; use only in a disposable workspace",
    )
    parser.add_argument("--log", type=Path, help="Optional JSONL event log path")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    task = " ".join(args.task).strip()
    if not task:
        try:
            task = input("Programming task: ").strip()
        except EOFError:
            task = ""
    policy = ApprovalPolicy.AUTO if args.auto else ApprovalPolicy.SAFE
    config_path = args.config
    if config_path is None and Path("agent.toml").is_file():
        config_path = Path("agent.toml")

    try:
        config = AgentConfig.from_env(
            args.workspace,
            approval_policy=policy,
            model=args.model,
            base_url=args.base_url,
            config_path=config_path,
            wire_api=args.wire_api,
            reasoning_effort=args.reasoning_effort,
            verbosity=args.verbosity,
            max_steps=args.max_steps,
        )
        config.validate_for_model()
        model = OpenAICompatibleClient(
            api_key=config.api_key or "not-required",
            base_url=config.base_url,
            model=config.model or "",
            wire_api=config.wire_api,
            reasoning_effort=config.model_reasoning_effort,
            verbosity=config.model_verbosity,
        )
        provider_label = config.model_provider or "environment/default"
        print(
            f"Provider: {provider_label} | model: {config.model} | "
            f"wire API: {config.wire_api.value}"
        )
        event_callback = _event_handler(args.log)
        runner = AgentRunner(
            model=model,
            registry=create_default_registry(),
            config=config,
            approval_callback=_ask_approval,
            event_callback=event_callback,
        )
        result = runner.run(task)
    except MiniCoderError as exc:
        print(f"Configuration/runtime error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\nInterrupted by user.", file=sys.stderr)
        return 130

    print("\n" + result.final_text)
    if result.total_usage:
        print("\nUsage: " + json.dumps(result.total_usage, ensure_ascii=False))
    return 0 if result.status == "completed" else 1


def _ask_approval(tool: Tool, arguments: dict[str, Any]) -> bool:
    rendered = json.dumps(arguments, ensure_ascii=False, default=str)
    print(f"\nApproval required [{tool.risk.value}] {tool.name}: {rendered}")
    try:
        answer = input("Allow? [y/N] ").strip().casefold()
    except EOFError:
        return False
    return answer in {"y", "yes"}


def _event_handler(log_path: Path | None):
    if log_path is not None:
        log_path = log_path.expanduser().resolve()

    def handle(name: str, payload: dict[str, Any]) -> None:
        if name == "model_request":
            print(f"\n[step {payload['step']}] asking model...")
        elif name == "tool_request":
            print(f"[tool] {payload['tool']} {json.dumps(payload['arguments'], ensure_ascii=False)}")
        elif name == "tool_result":
            state = "ok" if payload["ok"] else "error"
            preview = str(payload["content"]).replace("\n", " ")[:240]
            print(f"[{state}] {payload['tool']}: {preview}")
        elif name == "model_error":
            print(f"[model error] {payload['error']}", file=sys.stderr)
        if log_path is not None:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with log_path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps({"event": name, **payload}, ensure_ascii=False, default=str) + "\n")

    return handle


if __name__ == "__main__":
    raise SystemExit(main())
