from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .agent import AgentRunner
from .changes import ChangeTracker
from .config import AgentConfig, ApprovalPolicy
from .exceptions import ConfigurationError, MiniCoderError, SessionError
from .model import OpenAICompatibleClient
from .session import (
    AgentSession,
    SessionStatus,
    SessionStore,
    TaskPhase,
    ToolExecutionStatus,
)
from .tools import create_default_registry
from .tools.base import Tool


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mini-coder",
        description="Run a small local coding agent without an agent framework.",
    )
    parser.add_argument("task", nargs="*", help="Programming task; prompted when omitted")
    parser.add_argument(
        "--workspace",
        help="Workspace the agent may access; resumed sessions default to their saved workspace",
    )
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
    parser.add_argument(
        "--resume",
        help="Resume a saved session by ID or JSON file path; do not also provide a task",
    )
    parser.add_argument(
        "--resolve-uncertain",
        action="append",
        default=[],
        metavar="EXECUTION_ID=completed|failed",
        help=(
            "After inspecting the workspace, resolve one uncertain tool execution; "
            "may be repeated and requires --resume"
        ),
    )
    parser.add_argument(
        "--show-changes",
        action="store_true",
        help="Show tracked Session changes and exit; requires --resume",
    )
    parser.add_argument(
        "--undo-last",
        action="store_true",
        help="Safely undo the last active tracked file change and exit; requires --resume",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    policy = ApprovalPolicy.AUTO if args.auto else ApprovalPolicy.SAFE
    config_path = args.config
    if config_path is None and Path("agent.toml").is_file():
        config_path = Path("agent.toml")

    try:
        resumed_session, session_store = _load_resume_session(args.resume, args.workspace)
        if args.resolve_uncertain and resumed_session is None:
            raise ConfigurationError("--resolve-uncertain requires --resume")
        if (args.show_changes or args.undo_last) and resumed_session is None:
            raise ConfigurationError("--show-changes and --undo-last require --resume")
        if args.show_changes and args.undo_last:
            raise ConfigurationError("Use only one of --show-changes or --undo-last")
        if args.resolve_uncertain and (args.show_changes or args.undo_last):
            raise ConfigurationError(
                "Do not combine --resolve-uncertain with --show-changes or --undo-last"
            )
        if resumed_session is not None and args.task:
            raise ConfigurationError("Do not provide a new task together with --resume")
        if resumed_session is not None and (args.show_changes or args.undo_last):
            event_callback = _event_handler(args.log)
            if args.undo_last:
                tracker = ChangeTracker(resumed_session.workspace)
                change, undo = tracker.undo_last(resumed_session.changes)
                resumed_session.undo_history.append(undo)
                resumed_session.set_phase(TaskPhase.IMPLEMENT)
                invalidated = resumed_session.invalidate_verification(
                    f"tracked change undone: {change.path}"
                )
                if resumed_session.status in {
                    SessionStatus.COMPLETED_VERIFIED,
                    SessionStatus.COMPLETED_UNVERIFIED,
                }:
                    resumed_session.set_status(
                        SessionStatus.INTERRUPTED,
                        stop_reason="change_undone",
                    )
                session_store.save(resumed_session)
                for verification in invalidated:
                    event_callback(
                        "verification_invalidated",
                        {
                            "session_id": resumed_session.session_id,
                            "verification_id": verification.verification_id,
                            "reason": verification.invalidation_reason,
                            "change_revision": resumed_session.change_revision,
                        },
                    )
                event_callback(
                    "change_undone",
                    {
                        "session_id": resumed_session.session_id,
                        "undo_id": undo.undo_id,
                        "change_id": change.change_id,
                        "path": change.path,
                        "restored_hash": undo.restored_hash,
                    },
                )
                print(f"Undid tracked change {change.change_id} for {change.path}.")
            else:
                _print_changes(resumed_session)
            return 0
        if resumed_session is None:
            task = " ".join(args.task).strip()
            if not task:
                try:
                    task = input("Programming task: ").strip()
                except EOFError:
                    task = ""
            workspace = args.workspace or "."
        else:
            task = ""
            workspace = resumed_session.workspace

        config = AgentConfig.from_env(
            workspace,
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
        if resumed_session is not None:
            _validate_resume_model(resumed_session, config)
        if session_store is None:
            session_store = SessionStore.for_workspace(config.workspace)
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
        if resumed_session is not None:
            _print_resume_summary(resumed_session)
        event_callback = _event_handler(args.log)
        if resumed_session is not None and args.resolve_uncertain:
            _resolve_uncertain_tools(
                resumed_session,
                args.resolve_uncertain,
                session_store,
                event_callback,
            )
        runner = AgentRunner(
            model=model,
            registry=create_default_registry(),
            config=config,
            approval_callback=_ask_approval,
            event_callback=event_callback,
            session_store=session_store,
        )
        result = runner.run(task, session=resumed_session)
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


def _load_resume_session(
    identifier: str | None,
    workspace: str | None,
) -> tuple[AgentSession | None, SessionStore | None]:
    if identifier is None:
        return None, None

    candidate = Path(identifier).expanduser()
    explicit_path = candidate.is_absolute() or candidate.parent != Path(".") or candidate.suffix == ".json"
    if explicit_path:
        path = candidate.resolve()
        store = SessionStore(path.parent)
        session = store.load(path)
    else:
        lookup_workspace = Path(workspace or ".").expanduser().resolve()
        store = SessionStore.for_workspace(lookup_workspace)
        session = store.load(identifier)

    saved_workspace = Path(session.workspace).expanduser().resolve()
    if workspace is not None and Path(workspace).expanduser().resolve() != saved_workspace:
        raise SessionError(
            f"--workspace does not match the saved session workspace: {saved_workspace}"
        )
    return session, store


def _validate_resume_model(session: AgentSession, config: AgentConfig) -> None:
    expected = session.model
    actual = {
        "provider": config.model_provider,
        "model": config.model,
        "wire_api": config.wire_api.value,
    }
    mismatches = [
        f"{name}: saved={expected.get(name)!r}, current={value!r}"
        for name, value in actual.items()
        if expected.get(name) is not None and expected.get(name) != value
    ]
    if mismatches:
        raise ConfigurationError(
            "Cannot resume with a different model/provider protocol: " + "; ".join(mismatches)
        )


def _print_resume_summary(session: AgentSession) -> None:
    pending = [
        item
        for item in session.tool_executions
        if item.status.value in {"requested", "approved", "running", "uncertain"}
    ]
    task_preview = session.task.replace("\n", " ")[:240]
    print(
        "Resume summary:\n"
        f"  Session: {session.session_id}\n"
        f"  Workspace: {session.workspace}\n"
        f"  Saved status: {session.status.value}\n"
        f"  Task phase: {session.phase.value}\n"
        f"  Verification: {session.verification_status.value} "
        f"({len(session.verification_records)} command(s))\n"
        f"  Last completed model step: {session.current_step}\n"
        f"  Pending/uncertain tools: {len(pending)}\n"
        f"  Tracked changes: {len(session.changes)} "
        f"({len([item for item in session.changes if item.undo_status == 'active'])} active)\n"
        f"  Task: {task_preview}"
    )
    for item in pending:
        arguments = json.dumps(item.arguments, ensure_ascii=False, default=str)
        print(
            f"  - {item.execution_id}: {item.name} [{item.status.value}] "
            f"{arguments[:200]}"
        )


def _print_changes(session: AgentSession) -> None:
    print(
        f"Session {session.session_id} tracked changes: {len(session.changes)}; "
        f"undo operations: {len(session.undo_history)}; "
        f"verification: {session.verification_status.value}"
    )
    if not session.changes:
        print("No tracked file changes.")
        return
    for index, change in enumerate(session.changes, start=1):
        truncated = " [truncated]" if change.diff_truncated else ""
        print(
            f"\n[{index}] {change.path} [{change.undo_status}] "
            f"+{change.additions}/-{change.deletions}{truncated}\n"
            f"change_id={change.change_id}\n"
            f"tool_execution_id={change.tool_execution_id}\n"
            f"before={change.before_hash or '<missing>'}\n"
            f"after={change.after_hash}\n"
            f"{change.unified_diff}"
        )


def _resolve_uncertain_tools(
    session: AgentSession,
    specifications: list[str],
    store: SessionStore,
    event_callback,
) -> None:
    resolutions = []
    seen: set[str] = set()
    for specification in specifications:
        execution_id, separator, action = specification.rpartition("=")
        if not separator or not execution_id or action not in {"completed", "failed"}:
            raise ConfigurationError(
                "--resolve-uncertain must use EXECUTION_ID=completed or "
                "EXECUTION_ID=failed"
            )
        if execution_id in seen:
            raise ConfigurationError(
                f"uncertain tool execution was specified more than once: {execution_id}"
            )
        seen.add(execution_id)
        record = session.find_tool_execution(execution_id)
        if record is None:
            raise SessionError(f"session has no tool execution {execution_id!r}")
        if record.status != ToolExecutionStatus.UNCERTAIN:
            raise SessionError(
                f"tool execution {execution_id!r} is {record.status.value}, not uncertain"
            )
        has_tool_request = any(
            message.get("role") == "assistant"
            and any(
                call.get("id") == record.tool_call_id
                for call in message.get("tool_calls", [])
                if isinstance(call, dict)
            )
            for message in session.messages
        )
        if not has_tool_request:
            raise SessionError(
                f"uncertain tool execution {execution_id!r} has no matching assistant request"
            )
        if any(
            message.get("role") == "tool"
            and message.get("tool_call_id") == record.tool_call_id
            for message in session.messages
        ):
            raise SessionError(
                f"uncertain tool execution {execution_id!r} already has a tool result"
            )
        resolutions.append((record, action))

    for record, action in resolutions:
        completed = action == "completed"
        content = (
            "User inspected the workspace and confirmed that this uncertain tool "
            f"execution {'completed' if completed else 'did not complete'} before recovery."
        )
        record.result_content = content
        record.ok = completed
        record.error = None if completed else "User confirmed execution did not complete"
        record.set_status(
            ToolExecutionStatus.COMPLETED if completed else ToolExecutionStatus.FAILED
        )
        session.messages.append(
            {
                "role": "tool",
                "tool_call_id": record.tool_call_id,
                "content": content,
            }
        )
    session.touch()
    store.save(session)
    for record, action in resolutions:
        event_callback(
            "uncertain_resolved",
            {
                "session_id": session.session_id,
                "execution_id": record.execution_id,
                "tool": record.name,
                "resolution": action,
            },
        )


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
        if name == "session_created":
            print(f"Session: {payload['session_id']}\nSaved at: {payload['path']}")
        elif name == "session_resumed":
            print(
                f"Resuming session {payload['session_id']} from step "
                f"{payload['current_step']}"
            )
        elif name == "model_request":
            print(f"\n[step {payload['step']}] asking model...")
        elif name == "tool_request":
            print(f"[tool] {payload['tool']} {json.dumps(payload['arguments'], ensure_ascii=False)}")
        elif name == "tool_result":
            state = "ok" if payload["ok"] else "error"
            preview = str(payload["content"]).replace("\n", " ")[:240]
            print(f"[{state}] {payload['tool']}: {preview}")
        elif name == "model_error":
            print(f"[model error] {payload['error']}", file=sys.stderr)
        elif name == "uncertain_resolved":
            print(
                f"Resolved uncertain tool {payload['execution_id']} as "
                f"{payload['resolution']}."
            )
        elif name == "change_preview":
            truncated = " [truncated]" if payload["diff_truncated"] else ""
            print(
                f"\nChange preview: {payload['path']} "
                f"+{payload['additions']}/-{payload['deletions']}{truncated}\n"
                f"before: {payload['before_hash'] or '<missing>'}\n"
                f"after:  {payload['after_hash']}\n"
                f"{payload['diff']}"
            )
        elif name == "change_applied":
            print(
                f"[change] {payload['path']}: "
                f"+{payload['additions']}/-{payload['deletions']}"
            )
        elif name == "change_undone":
            print(f"[undo] restored {payload['path']}")
        elif name == "phase_changed":
            print(f"[phase] {payload['previous']} -> {payload['phase']}")
        elif name == "verification_recorded":
            state = "passed" if payload["passed"] else "failed"
            print(
                f"[verification] {state}, exit {payload['exit_code']}, "
                f"{payload['duration_seconds']:.2f}s"
            )
        elif name == "verification_invalidated":
            print(f"[verification] stale: {payload['reason']}")
        elif name == "workspace_changed":
            print(
                "[workspace] changed outside the saved Session: "
                + ", ".join(payload["paths"])
            )
        if log_path is not None:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with log_path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps({"event": name, **payload}, ensure_ascii=False, default=str) + "\n")

    return handle


if __name__ == "__main__":
    raise SystemExit(main())
