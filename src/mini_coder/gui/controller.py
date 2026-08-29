from __future__ import annotations

import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from ..agent import AgentRunResult, AgentRunner
from ..config import AgentConfig, ApprovalPolicy
from ..exceptions import MiniCoderError
from ..model import OpenAICompatibleClient
from ..redaction import redact_sensitive_text, redact_sensitive_value
from ..session import SessionStore
from ..tools import create_default_registry
from ..tools.base import Tool
from ..tools.command_risk import assess_command


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True, slots=True)
class RunRequest:
    task: str
    workspace: str
    title: str = ""
    session_id: str | None = None
    config_path: str | None = None
    auto: bool = False


class RunnerLike(Protocol):
    def run(self, task: str) -> AgentRunResult: ...


@dataclass(slots=True)
class _SessionBoundRunner:
    runner: AgentRunner
    session: Any | None = None

    def run(self, task: str) -> AgentRunResult:
        return self.runner.run(task, session=self.session)


RunnerFactory = Callable[
    [RunRequest, Callable[[str, dict[str, Any]], None], Callable[[Tool, dict[str, Any]], bool]],
    RunnerLike,
]


@dataclass(slots=True)
class PendingApproval:
    approval_id: str
    tool: str
    risk: str
    arguments: dict[str, Any]
    created_at: str
    resolved: threading.Event = field(default_factory=threading.Event, repr=False)
    approved: bool | None = None

    def public_dict(self) -> dict[str, Any]:
        return {
            "approval_id": self.approval_id,
            "tool": self.tool,
            "risk": self.risk,
            "arguments": self.arguments,
            "created_at": self.created_at,
        }


@dataclass(slots=True)
class RunRecord:
    run_id: str
    request: RunRequest
    status: str = "queued"
    created_at: str = field(default_factory=_utc_now)
    updated_at: str = field(default_factory=_utc_now)
    next_sequence: int = 1
    events: list[dict[str, Any]] = field(default_factory=list)
    pending_approval: PendingApproval | None = None
    result: AgentRunResult | None = None
    error: str | None = None


class RunController:
    """Thread-safe bridge between the synchronous AgentRunner and the web UI."""

    TERMINAL_STATUSES = {"completed", "failed", "cancelled"}

    def __init__(
        self,
        *,
        runner_factory: RunnerFactory | None = None,
        approval_timeout_seconds: float = 900.0,
        max_events_per_run: int = 2_000,
    ) -> None:
        if approval_timeout_seconds <= 0:
            raise ValueError("approval_timeout_seconds must be positive")
        if max_events_per_run < 100:
            raise ValueError("max_events_per_run must be at least 100")
        self._runner_factory = runner_factory or self._default_runner_factory
        self._approval_timeout_seconds = approval_timeout_seconds
        self._max_events_per_run = max_events_per_run
        self._condition = threading.Condition(threading.RLock())
        self._runs: dict[str, RunRecord] = {}

    def start(self, request: RunRequest) -> dict[str, Any]:
        task = request.task.strip()
        if not task:
            raise ValueError("task must not be empty")
        workspace = Path(request.workspace).expanduser().resolve()
        if not workspace.is_dir():
            raise ValueError(f"workspace is not a directory: {workspace}")
        config_path = self._resolve_config_path(request.config_path)
        session_id = request.session_id.strip() if request.session_id else None
        title = request.title.strip() or (task[:39].rstrip() + "…" if len(task) > 40 else task)
        if session_id:
            session = SessionStore.for_workspace(workspace).load(session_id)
            if Path(session.workspace).resolve() != workspace:
                raise ValueError("session workspace does not match the selected workspace")
            title = session.title
        if len(title) > 120:
            raise ValueError("session title must not exceed 120 characters")
        normalized = RunRequest(
            task=task,
            workspace=str(workspace),
            title=title,
            session_id=session_id,
            config_path=str(config_path) if config_path is not None else None,
            auto=request.auto,
        )
        record = RunRecord(run_id=uuid.uuid4().hex, request=normalized)
        with self._condition:
            if session_id and any(
                item.request.session_id == session_id
                and item.status not in self.TERMINAL_STATUSES
                for item in self._runs.values()
            ):
                raise ValueError("this session is already running")
            self._runs[record.run_id] = record
            self._append_event_locked(
                record,
                "controller_run_created",
                {
                    "workspace": normalized.workspace,
                    "title": normalized.title,
                    "approval_policy": "auto" if normalized.auto else "safe",
                    "session_id": normalized.session_id,
                },
            )
        thread = threading.Thread(
            target=self._run_worker,
            args=(record.run_id,),
            name=f"mini-coder-gui-{record.run_id[:8]}",
            daemon=True,
        )
        thread.start()
        return self.snapshot(record.run_id)

    def snapshot(self, run_id: str) -> dict[str, Any]:
        with self._condition:
            record = self._require_run_locked(run_id)
            return self._snapshot_locked(record)

    def events_after(self, run_id: str, sequence: int = 0) -> list[dict[str, Any]]:
        with self._condition:
            record = self._require_run_locked(run_id)
            return [dict(item) for item in record.events if item["sequence"] > sequence]

    def wait_for_events(
        self,
        run_id: str,
        sequence: int,
        timeout_seconds: float = 15.0,
    ) -> tuple[list[dict[str, Any]], bool]:
        with self._condition:
            record = self._require_run_locked(run_id)
            if not any(item["sequence"] > sequence for item in record.events):
                if record.status not in self.TERMINAL_STATUSES:
                    self._condition.wait(timeout_seconds)
            events = [dict(item) for item in record.events if item["sequence"] > sequence]
            terminal = record.status in self.TERMINAL_STATUSES
            return events, terminal

    def decide_approval(
        self,
        run_id: str,
        approval_id: str,
        approved: bool,
    ) -> dict[str, Any]:
        with self._condition:
            record = self._require_run_locked(run_id)
            pending = record.pending_approval
            if pending is None or pending.approval_id != approval_id:
                raise ValueError("approval is not pending")
            if pending.resolved.is_set():
                raise ValueError("approval has already been resolved")
            pending.approved = bool(approved)
            pending.resolved.set()
            record.updated_at = _utc_now()
            self._condition.notify_all()
            return self._snapshot_locked(record)

    def _run_worker(self, run_id: str) -> None:
        with self._condition:
            record = self._require_run_locked(run_id)
            record.status = "running"
            record.updated_at = _utc_now()
            self._condition.notify_all()

        def event_callback(name: str, payload: dict[str, Any]) -> None:
            safe_value = redact_sensitive_value(payload)
            safe_payload = safe_value if isinstance(safe_value, dict) else {}
            with self._condition:
                active = self._require_run_locked(run_id)
                self._append_event_locked(active, name, safe_payload)

        def approval_callback(tool: Tool, arguments: dict[str, Any]) -> bool:
            risk = tool.risk.value
            if tool.name == "run_command":
                risk = assess_command(str(arguments.get("command", ""))).level.value
            safe_value = redact_sensitive_value(arguments)
            safe_arguments = safe_value if isinstance(safe_value, dict) else {}
            pending = PendingApproval(
                approval_id=uuid.uuid4().hex,
                tool=tool.name,
                risk=risk,
                arguments=safe_arguments,
                created_at=_utc_now(),
            )
            with self._condition:
                active = self._require_run_locked(run_id)
                active.pending_approval = pending
                active.status = "waiting_for_approval"
                active.updated_at = _utc_now()
                self._append_event_locked(
                    active,
                    "approval_required",
                    pending.public_dict(),
                )
            resolved = pending.resolved.wait(self._approval_timeout_seconds)
            approved = bool(resolved and pending.approved)
            with self._condition:
                active = self._require_run_locked(run_id)
                if active.pending_approval is pending:
                    active.pending_approval = None
                active.status = "running"
                active.updated_at = _utc_now()
                self._append_event_locked(
                    active,
                    "approval_resolved" if resolved else "approval_expired",
                    {
                        "approval_id": pending.approval_id,
                        "tool": pending.tool,
                        "approved": approved,
                    },
                )
            return approved

        try:
            record = self._runs[run_id]
            runner = self._runner_factory(record.request, event_callback, approval_callback)
            result = runner.run(record.request.task)
            with self._condition:
                active = self._require_run_locked(run_id)
                active.result = result
                active.status = "completed" if result.status == "completed" else "failed"
                active.updated_at = _utc_now()
                self._append_event_locked(
                    active,
                    "controller_run_finished",
                    {
                        "result_status": result.status,
                        "final_text": result.final_text,
                        "steps": result.steps,
                        "session_id": result.session_id,
                        "total_usage": result.total_usage,
                    },
                )
        except MiniCoderError as exc:
            self._fail_run(run_id, str(exc))
        except Exception as exc:  # keep a worker failure from taking down the local server
            self._fail_run(run_id, f"Unexpected GUI worker error: {exc}")

    def _fail_run(self, run_id: str, error: str) -> None:
        safe_error = redact_sensitive_text(error)
        with self._condition:
            active = self._require_run_locked(run_id)
            active.status = "failed"
            active.error = safe_error
            active.updated_at = _utc_now()
            self._append_event_locked(
                active,
                "controller_run_failed",
                {"error": safe_error},
            )

    def _append_event_locked(
        self,
        record: RunRecord,
        name: str,
        payload: dict[str, Any],
    ) -> None:
        record.events.append(
            {
                "sequence": record.next_sequence,
                "event": name,
                "timestamp": _utc_now(),
                "payload": payload,
            }
        )
        record.next_sequence += 1
        if len(record.events) > self._max_events_per_run:
            overflow = len(record.events) - self._max_events_per_run
            del record.events[:overflow]
        record.updated_at = _utc_now()
        self._condition.notify_all()

    def _snapshot_locked(self, record: RunRecord) -> dict[str, Any]:
        result = record.result
        return {
            "run_id": record.run_id,
            "status": record.status,
            "task": record.request.task,
            "title": record.request.title,
            "workspace": record.request.workspace,
            "session_id": record.request.session_id,
            "created_at": record.created_at,
            "updated_at": record.updated_at,
            "latest_sequence": record.next_sequence - 1,
            "pending_approval": (
                record.pending_approval.public_dict()
                if record.pending_approval is not None
                else None
            ),
            "result": (
                {
                    "status": result.status,
                    "final_text": result.final_text,
                    "steps": result.steps,
                    "total_usage": result.total_usage,
                    "session_id": result.session_id,
                }
                if result is not None
                else None
            ),
            "error": record.error,
        }

    def _require_run_locked(self, run_id: str) -> RunRecord:
        try:
            return self._runs[run_id]
        except KeyError as exc:
            raise KeyError(f"unknown run: {run_id}") from exc

    @staticmethod
    def _resolve_config_path(config_path: str | None) -> Path | None:
        if config_path:
            path = Path(config_path).expanduser().resolve()
            if not path.is_file():
                raise ValueError(f"config file does not exist: {path}")
            return path
        candidate = Path.cwd() / "agent.toml"
        return candidate.resolve() if candidate.is_file() else None

    @staticmethod
    def _default_runner_factory(
        request: RunRequest,
        event_callback: Callable[[str, dict[str, Any]], None],
        approval_callback: Callable[[Tool, dict[str, Any]], bool],
    ) -> RunnerLike:
        policy = ApprovalPolicy.AUTO if request.auto else ApprovalPolicy.SAFE
        config = AgentConfig.from_env(
            request.workspace,
            approval_policy=policy,
            config_path=request.config_path,
        )
        config.validate_for_model()
        model = OpenAICompatibleClient(
            api_key=config.api_key or "not-required",
            base_url=config.base_url,
            model=config.model or "",
            wire_api=config.wire_api,
            reasoning_effort=config.model_reasoning_effort,
            verbosity=config.model_verbosity,
            timeout_seconds=config.model_timeout_seconds,
        )
        runner = AgentRunner(
            model=model,
            registry=create_default_registry(),
            config=config,
            approval_callback=approval_callback,
            event_callback=event_callback,
            session_store=SessionStore.for_workspace(config.workspace),
            session_title=request.title,
        )
        session = (
            SessionStore.for_workspace(config.workspace).load(request.session_id)
            if request.session_id
            else None
        )
        return _SessionBoundRunner(runner, session)
