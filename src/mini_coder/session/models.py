from __future__ import annotations

import copy
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from ..changes.models import ChangeRecord, PreparedChange, UndoRecord
from ..exceptions import SessionError
from ..verification import (
    TaskPhase,
    VerificationRecord,
    VerificationStatus,
    VerificationTracker,
)

CURRENT_SESSION_SCHEMA = 5
_SESSION_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_SAFE_MODEL_FIELDS = {
    "provider",
    "model",
    "wire_api",
    "reasoning_effort",
    "verbosity",
    "approval_policy",
    "max_steps",
    "max_seconds",
    "max_model_calls",
    "max_tool_calls",
    "max_tool_output_chars",
    "max_total_tool_output_chars",
    "max_total_tokens",
}


class SessionStatus(str, Enum):
    CREATED = "created"
    RUNNING = "running"
    WAITING_FOR_APPROVAL = "waiting_for_approval"
    INTERRUPTED = "interrupted"
    FAILED = "failed"
    COMPLETED_VERIFIED = "completed_verified"
    COMPLETED_UNVERIFIED = "completed_unverified"
    DENIED = "denied"
    CANCELLED = "cancelled"


class ToolExecutionStatus(str, Enum):
    REQUESTED = "requested"
    APPROVED = "approved"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    DENIED = "denied"
    UNCERTAIN = "uncertain"


_TOOL_EXECUTION_TRANSITIONS = {
    ToolExecutionStatus.REQUESTED: {
        ToolExecutionStatus.APPROVED,
        ToolExecutionStatus.COMPLETED,
        ToolExecutionStatus.FAILED,
        ToolExecutionStatus.DENIED,
    },
    ToolExecutionStatus.APPROVED: {
        ToolExecutionStatus.RUNNING,
        ToolExecutionStatus.FAILED,
        ToolExecutionStatus.DENIED,
    },
    ToolExecutionStatus.RUNNING: {
        ToolExecutionStatus.COMPLETED,
        ToolExecutionStatus.FAILED,
        ToolExecutionStatus.UNCERTAIN,
    },
    ToolExecutionStatus.COMPLETED: set(),
    ToolExecutionStatus.FAILED: set(),
    ToolExecutionStatus.DENIED: set(),
    ToolExecutionStatus.UNCERTAIN: {
        ToolExecutionStatus.COMPLETED,
        ToolExecutionStatus.FAILED,
    },
}


_SESSION_TRANSITIONS = {
    SessionStatus.CREATED: {
        SessionStatus.RUNNING,
        SessionStatus.FAILED,
        SessionStatus.CANCELLED,
    },
    SessionStatus.RUNNING: {
        SessionStatus.WAITING_FOR_APPROVAL,
        SessionStatus.INTERRUPTED,
        SessionStatus.FAILED,
        SessionStatus.COMPLETED_VERIFIED,
        SessionStatus.COMPLETED_UNVERIFIED,
        SessionStatus.DENIED,
        SessionStatus.CANCELLED,
    },
    SessionStatus.WAITING_FOR_APPROVAL: {
        SessionStatus.RUNNING,
        SessionStatus.INTERRUPTED,
        SessionStatus.FAILED,
        SessionStatus.DENIED,
        SessionStatus.CANCELLED,
    },
    SessionStatus.INTERRUPTED: {
        SessionStatus.RUNNING,
        SessionStatus.FAILED,
        SessionStatus.CANCELLED,
    },
    SessionStatus.FAILED: {
        SessionStatus.RUNNING,
        SessionStatus.CANCELLED,
    },
    SessionStatus.COMPLETED_VERIFIED: {
        SessionStatus.RUNNING,
        SessionStatus.INTERRUPTED,
        SessionStatus.CANCELLED,
    },
    SessionStatus.COMPLETED_UNVERIFIED: {
        SessionStatus.RUNNING,
        SessionStatus.INTERRUPTED,
        SessionStatus.CANCELLED,
    },
    SessionStatus.DENIED: {
        SessionStatus.RUNNING,
        SessionStatus.CANCELLED,
    },
    SessionStatus.CANCELLED: set(),
}


TERMINAL_TOOL_STATUSES = {
    ToolExecutionStatus.COMPLETED,
    ToolExecutionStatus.FAILED,
    ToolExecutionStatus.DENIED,
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def validate_session_id(value: str) -> str:
    if not isinstance(value, str) or not _SESSION_ID_PATTERN.fullmatch(value):
        raise SessionError(
            "session_id must start with an ASCII letter or digit and contain only "
            "letters, digits, underscores, or hyphens"
        )
    return value


@dataclass(slots=True)
class ToolExecutionRecord:
    execution_id: str
    tool_call_id: str
    step: int
    name: str
    arguments: dict[str, Any] | None
    raw_arguments: str
    risk: str
    parse_error: str | None = None
    approval_granted: bool | None = None
    status: ToolExecutionStatus = ToolExecutionStatus.REQUESTED
    result_content: str | None = None
    ok: bool | None = None
    error: str | None = None
    prepared_change: PreparedChange | None = None
    change_id: str | None = None
    exit_code: int | None = None
    timed_out: bool | None = None
    output_truncated: bool | None = None
    duration_seconds: float | None = None
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        validate_session_id(self.execution_id)
        if not self.tool_call_id:
            raise SessionError("tool_call_id must not be empty")
        if self.step < 1:
            raise SessionError("tool execution step must be positive")
        if not self.name:
            raise SessionError("tool execution name must not be empty")
        if not isinstance(self.raw_arguments, str):
            raise SessionError("tool execution raw_arguments must be a string")
        if (
            self.prepared_change is not None
            and self.prepared_change.tool_execution_id != self.execution_id
        ):
            raise SessionError("prepared change does not belong to its tool execution")
        if self.change_id is not None:
            validate_session_id(self.change_id)

    @classmethod
    def create(
        cls,
        *,
        execution_id: str,
        tool_call_id: str,
        step: int,
        name: str,
        arguments: dict[str, Any] | None,
        raw_arguments: str,
        risk: str,
        parse_error: str | None = None,
    ) -> "ToolExecutionRecord":
        return cls(
            execution_id=execution_id,
            tool_call_id=tool_call_id,
            step=step,
            name=name,
            arguments=copy.deepcopy(arguments),
            raw_arguments=raw_arguments,
            risk=risk,
            parse_error=parse_error,
        )

    def set_status(self, status: ToolExecutionStatus) -> None:
        if status != self.status and status not in _TOOL_EXECUTION_TRANSITIONS[self.status]:
            raise SessionError(
                f"invalid tool execution status transition: "
                f"{self.status.value} -> {status.value}"
            )
        self.status = status
        self.updated_at = utc_now()

    def to_dict(self) -> dict[str, Any]:
        return {
            "execution_id": self.execution_id,
            "tool_call_id": self.tool_call_id,
            "step": self.step,
            "name": self.name,
            "arguments": copy.deepcopy(self.arguments),
            "raw_arguments": self.raw_arguments,
            "risk": self.risk,
            "parse_error": self.parse_error,
            "approval_granted": self.approval_granted,
            "status": self.status.value,
            "result_content": self.result_content,
            "ok": self.ok,
            "error": self.error,
            "prepared_change": (
                self.prepared_change.to_dict() if self.prepared_change is not None else None
            ),
            "change_id": self.change_id,
            "exit_code": self.exit_code,
            "timed_out": self.timed_out,
            "output_truncated": self.output_truncated,
            "duration_seconds": self.duration_seconds,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: Any) -> "ToolExecutionRecord":
        if not isinstance(data, dict):
            raise SessionError("tool execution must be a JSON object")
        arguments = data.get("arguments")
        if arguments is not None and not isinstance(arguments, dict):
            raise SessionError("tool execution arguments must be an object or null")
        try:
            status = ToolExecutionStatus(data.get("status"))
        except ValueError as exc:
            raise SessionError(f"unsupported tool execution status: {data.get('status')!r}") from exc
        return cls(
            execution_id=_required_string(data, "execution_id"),
            tool_call_id=_required_string(data, "tool_call_id"),
            step=_required_int(data, "step", minimum=1),
            name=_required_string(data, "name"),
            arguments=copy.deepcopy(arguments),
            raw_arguments=_required_string(data, "raw_arguments", allow_empty=True),
            risk=_required_string(data, "risk"),
            parse_error=_optional_string(data, "parse_error"),
            approval_granted=_optional_bool(data, "approval_granted"),
            status=status,
            result_content=_optional_string(data, "result_content"),
            ok=_optional_bool(data, "ok"),
            error=_optional_string(data, "error"),
            prepared_change=(
                PreparedChange.from_dict(data["prepared_change"])
                if data.get("prepared_change") is not None
                else None
            ),
            change_id=_optional_string(data, "change_id"),
            exit_code=_optional_int(data, "exit_code"),
            timed_out=_optional_bool(data, "timed_out"),
            output_truncated=_optional_bool(data, "output_truncated"),
            duration_seconds=_optional_number(data, "duration_seconds", minimum=0),
            created_at=_required_string(data, "created_at"),
            updated_at=_required_string(data, "updated_at"),
        )


@dataclass(slots=True)
class AgentSession:
    session_id: str
    task: str
    workspace: str
    created_at: str
    updated_at: str
    status: SessionStatus = SessionStatus.CREATED
    schema_version: int = CURRENT_SESSION_SCHEMA
    current_step: int = 0
    messages: list[dict[str, Any]] = field(default_factory=list)
    total_usage: dict[str, int] = field(default_factory=dict)
    model: dict[str, Any] = field(default_factory=dict)
    tool_executions: list[ToolExecutionRecord] = field(default_factory=list)
    changes: list[ChangeRecord] = field(default_factory=list)
    undo_history: list[UndoRecord] = field(default_factory=list)
    phase: TaskPhase = TaskPhase.ANALYZE
    verification_status: VerificationStatus = VerificationStatus.NOT_REQUIRED
    verification_records: list[VerificationRecord] = field(default_factory=list)
    change_revision: int = 0
    run_duration_seconds: float = 0.0
    retry_count: int = 0
    model_call_count: int = 0
    usage_missing_count: int = 0
    tool_output_chars: int = 0
    workspace_baseline: dict[str, Any] = field(default_factory=dict)
    failed_tool_call_count: int = 0
    invalid_tool_call_count: int = 0
    repeated_read_hint_count: int = 0
    stop_reason: str | None = None
    final_text: str = ""
    last_error: str | None = None
    previous_call_signature: str | None = None
    repeated_call_count: int = 0

    def __post_init__(self) -> None:
        validate_session_id(self.session_id)
        if self.schema_version != CURRENT_SESSION_SCHEMA:
            raise SessionError(
                f"unsupported session schema version {self.schema_version}; "
                f"expected {CURRENT_SESSION_SCHEMA}"
            )
        if not self.task.strip():
            raise SessionError("session task must not be empty")
        if not self.workspace:
            raise SessionError("session workspace must not be empty")
        if self.current_step < 0:
            raise SessionError("session current_step must not be negative")
        if self.repeated_call_count < 0:
            raise SessionError("session repeated_call_count must not be negative")
        if self.change_revision < 0:
            raise SessionError("session change_revision must not be negative")
        if self.run_duration_seconds < 0:
            raise SessionError("session run_duration_seconds must not be negative")
        if self.retry_count < 0:
            raise SessionError("session retry_count must not be negative")
        if self.model_call_count < 0:
            raise SessionError("session model_call_count must not be negative")
        if self.usage_missing_count < 0:
            raise SessionError("session usage_missing_count must not be negative")
        if self.usage_missing_count > self.model_call_count:
            raise SessionError("usage_missing_count cannot exceed model_call_count")
        if self.tool_output_chars < 0:
            raise SessionError("session tool_output_chars must not be negative")
        for name, value in {
            "failed_tool_call_count": self.failed_tool_call_count,
            "invalid_tool_call_count": self.invalid_tool_call_count,
            "repeated_read_hint_count": self.repeated_read_hint_count,
        }.items():
            if value < 0:
                raise SessionError(f"session {name} must not be negative")
        if not isinstance(self.workspace_baseline, dict):
            raise SessionError("session workspace_baseline must be an object")
        if not isinstance(self.messages, list) or not all(
            isinstance(item, dict) for item in self.messages
        ):
            raise SessionError("session messages must be a list of objects")
        if not isinstance(self.total_usage, dict) or not all(
            isinstance(name, str)
            and isinstance(value, int)
            and not isinstance(value, bool)
            and value >= 0
            for name, value in self.total_usage.items()
        ):
            raise SessionError("session total_usage must contain non-negative integers")
        if not all(isinstance(item, ChangeRecord) for item in self.changes):
            raise SessionError("session changes must contain ChangeRecord values")
        if not all(isinstance(item, UndoRecord) for item in self.undo_history):
            raise SessionError("session undo_history must contain UndoRecord values")
        if not all(isinstance(item, VerificationRecord) for item in self.verification_records):
            raise SessionError(
                "session verification_records must contain VerificationRecord values"
            )
        _validate_model_summary(self.model)

    @classmethod
    def create(
        cls,
        *,
        task: str,
        workspace: str | Path,
        model: dict[str, Any] | None = None,
        messages: list[dict[str, Any]] | None = None,
        workspace_baseline: dict[str, Any] | None = None,
    ) -> "AgentSession":
        now = utc_now()
        return cls(
            session_id=uuid.uuid4().hex,
            task=task.strip(),
            workspace=str(Path(workspace).expanduser().resolve()),
            created_at=now,
            updated_at=now,
            model=copy.deepcopy(model or {}),
            messages=copy.deepcopy(messages or []),
            workspace_baseline=copy.deepcopy(workspace_baseline or {}),
        )

    def set_status(
        self,
        status: SessionStatus,
        *,
        stop_reason: str | None = None,
        final_text: str | None = None,
        last_error: str | None = None,
    ) -> None:
        if (
            status == SessionStatus.COMPLETED_VERIFIED
            and self.refresh_verification_status() != VerificationStatus.PASSED
        ):
            raise SessionError(
                "completed_verified requires a current successful verification record"
            )
        if status != self.status and status not in _SESSION_TRANSITIONS[self.status]:
            raise SessionError(
                f"invalid session status transition: {self.status.value} -> {status.value}"
            )
        self.status = status
        self.stop_reason = stop_reason
        if final_text is not None:
            self.final_text = final_text
        self.last_error = last_error
        self.updated_at = utc_now()

    def find_tool_execution(self, execution_id: str) -> ToolExecutionRecord | None:
        return next(
            (item for item in self.tool_executions if item.execution_id == execution_id),
            None,
        )

    def touch(self) -> None:
        self.updated_at = utc_now()

    def set_phase(self, phase: TaskPhase) -> None:
        self.phase = phase
        self.touch()

    def invalidate_verification(self, reason: str) -> list[VerificationRecord]:
        self.change_revision += 1
        invalidated = VerificationTracker.invalidate(
            self.verification_records,
            reason=reason,
        )
        self.refresh_verification_status()
        self.touch()
        return invalidated

    def refresh_verification_status(self) -> VerificationStatus:
        self.verification_status = VerificationTracker.evaluate(
            self.verification_records,
            change_revision=self.change_revision,
            had_file_modification=bool(self.changes or self.undo_history),
        )
        return self.verification_status

    def to_dict(self) -> dict[str, Any]:
        _validate_model_summary(self.model)
        return {
            "schema_version": self.schema_version,
            "session_id": self.session_id,
            "task": self.task,
            "workspace": self.workspace,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "status": self.status.value,
            "current_step": self.current_step,
            "messages": copy.deepcopy(self.messages),
            "total_usage": dict(self.total_usage),
            "model": copy.deepcopy(self.model),
            "tool_executions": [item.to_dict() for item in self.tool_executions],
            "changes": [item.to_dict() for item in self.changes],
            "undo_history": [item.to_dict() for item in self.undo_history],
            "phase": self.phase.value,
            "verification_status": self.verification_status.value,
            "verification_records": [
                item.to_dict() for item in self.verification_records
            ],
            "change_revision": self.change_revision,
            "run_duration_seconds": self.run_duration_seconds,
            "retry_count": self.retry_count,
            "model_call_count": self.model_call_count,
            "usage_missing_count": self.usage_missing_count,
            "tool_output_chars": self.tool_output_chars,
            "workspace_baseline": copy.deepcopy(self.workspace_baseline),
            "failed_tool_call_count": self.failed_tool_call_count,
            "invalid_tool_call_count": self.invalid_tool_call_count,
            "repeated_read_hint_count": self.repeated_read_hint_count,
            "stop_reason": self.stop_reason,
            "final_text": self.final_text,
            "last_error": self.last_error,
            "previous_call_signature": self.previous_call_signature,
            "repeated_call_count": self.repeated_call_count,
        }

    @classmethod
    def from_dict(cls, data: Any) -> "AgentSession":
        if not isinstance(data, dict):
            raise SessionError("session root must be a JSON object")
        data = _migrate_session_data(data)
        version = _required_int(data, "schema_version", minimum=1)
        if version != CURRENT_SESSION_SCHEMA:
            raise SessionError(
                f"unsupported session schema version {version}; "
                f"expected {CURRENT_SESSION_SCHEMA}"
            )
        try:
            status = SessionStatus(data.get("status"))
        except ValueError as exc:
            raise SessionError(f"unsupported session status: {data.get('status')!r}") from exc
        messages = data.get("messages")
        if not isinstance(messages, list) or not all(isinstance(item, dict) for item in messages):
            raise SessionError("session messages must be a list of objects")
        usage = data.get("total_usage")
        if not isinstance(usage, dict):
            raise SessionError("session total_usage must be an object")
        model = data.get("model")
        if not isinstance(model, dict):
            raise SessionError("session model must be an object")
        executions = data.get("tool_executions")
        if not isinstance(executions, list):
            raise SessionError("session tool_executions must be a list")
        changes = data.get("changes")
        if not isinstance(changes, list):
            raise SessionError("session changes must be a list")
        undo_history = data.get("undo_history")
        if not isinstance(undo_history, list):
            raise SessionError("session undo_history must be a list")
        verification_records = data.get("verification_records")
        if not isinstance(verification_records, list):
            raise SessionError("session verification_records must be a list")
        workspace_baseline = data.get("workspace_baseline")
        if not isinstance(workspace_baseline, dict):
            raise SessionError("session workspace_baseline must be an object")
        try:
            phase = TaskPhase(data.get("phase"))
            verification_status = VerificationStatus(data.get("verification_status"))
            parsed_verifications = [
                VerificationRecord.from_dict(item) for item in verification_records
            ]
        except ValueError as exc:
            raise SessionError(f"invalid verification state: {exc}") from exc
        session = cls(
            schema_version=version,
            session_id=_required_string(data, "session_id"),
            task=_required_string(data, "task"),
            workspace=_required_string(data, "workspace"),
            created_at=_required_string(data, "created_at"),
            updated_at=_required_string(data, "updated_at"),
            status=status,
            current_step=_required_int(data, "current_step", minimum=0),
            messages=copy.deepcopy(messages),
            total_usage=dict(usage),
            model=copy.deepcopy(model),
            tool_executions=[ToolExecutionRecord.from_dict(item) for item in executions],
            changes=[ChangeRecord.from_dict(item) for item in changes],
            undo_history=[UndoRecord.from_dict(item) for item in undo_history],
            phase=phase,
            verification_status=verification_status,
            verification_records=parsed_verifications,
            change_revision=_required_int(data, "change_revision", minimum=0),
            run_duration_seconds=_required_number(
                data,
                "run_duration_seconds",
                minimum=0,
            ),
            retry_count=_required_int(data, "retry_count", minimum=0),
            model_call_count=_required_int(data, "model_call_count", minimum=0),
            usage_missing_count=_required_int(data, "usage_missing_count", minimum=0),
            tool_output_chars=_required_int(data, "tool_output_chars", minimum=0),
            workspace_baseline=copy.deepcopy(workspace_baseline),
            failed_tool_call_count=_required_int(
                data,
                "failed_tool_call_count",
                minimum=0,
            ),
            invalid_tool_call_count=_required_int(
                data,
                "invalid_tool_call_count",
                minimum=0,
            ),
            repeated_read_hint_count=_required_int(
                data,
                "repeated_read_hint_count",
                minimum=0,
            ),
            stop_reason=_optional_string(data, "stop_reason"),
            final_text=_required_string(data, "final_text", allow_empty=True),
            last_error=_optional_string(data, "last_error"),
            previous_call_signature=_optional_string(data, "previous_call_signature"),
            repeated_call_count=_required_int(data, "repeated_call_count", minimum=0),
        )
        derived_verification = VerificationTracker.evaluate(
            session.verification_records,
            change_revision=session.change_revision,
            had_file_modification=bool(session.changes or session.undo_history),
        )
        if session.verification_status != derived_verification:
            raise SessionError(
                "saved verification_status is inconsistent with verification records"
            )
        if (
            session.status == SessionStatus.COMPLETED_VERIFIED
            and derived_verification != VerificationStatus.PASSED
        ):
            raise SessionError(
                "completed_verified session has no current successful verification"
            )
        return session


def _migrate_session_data(data: dict[str, Any]) -> dict[str, Any]:
    version = data.get("schema_version")
    if version not in {1, 2, 3, 4}:
        return data
    migrated = copy.deepcopy(data)
    if version == 1:
        migrated["schema_version"] = 2
        migrated.setdefault("changes", [])
        migrated.setdefault("undo_history", [])
        for execution in migrated.get("tool_executions", []):
            if isinstance(execution, dict):
                execution.setdefault("prepared_change", None)
                execution.setdefault("change_id", None)
        version = 2
    if version == 2:
        changes = migrated.get("changes", [])
        undo_history = migrated.get("undo_history", [])
        status = migrated.get("status")
        migrated["schema_version"] = 3
        migrated.setdefault(
            "phase",
            "summarize" if str(status).startswith("completed_") else "analyze",
        )
        migrated.setdefault("verification_status", "unverified" if changes else "not_required")
        migrated.setdefault("verification_records", [])
        migrated.setdefault(
            "change_revision",
            len(changes) + len(undo_history),
        )
        migrated.setdefault("run_duration_seconds", 0.0)
        migrated.setdefault("retry_count", 0)
        version = 3
    if version == 3:
        migrated["schema_version"] = 4
        inferred_model_calls = sum(
            1
            for message in migrated.get("messages", [])
            if isinstance(message, dict) and message.get("role") == "assistant"
        )
        migrated.setdefault("model_call_count", inferred_model_calls)
        previous_usage = migrated.get("total_usage")
        has_exact_total = (
            isinstance(previous_usage, dict)
            and isinstance(previous_usage.get("total_tokens"), int)
            and not isinstance(previous_usage.get("total_tokens"), bool)
        )
        migrated.setdefault(
            "usage_missing_count",
            0 if has_exact_total else inferred_model_calls,
        )
        for execution in migrated.get("tool_executions", []):
            if isinstance(execution, dict):
                execution.setdefault("exit_code", None)
                execution.setdefault("timed_out", None)
                execution.setdefault("output_truncated", None)
                execution.setdefault("duration_seconds", None)
        migrated.setdefault(
            "tool_output_chars",
            sum(
                len(str(execution.get("result_content") or ""))
                for execution in migrated.get("tool_executions", [])
                if isinstance(execution, dict)
            ),
        )
        version = 4
    if version == 4:
        migrated["schema_version"] = 5
        migrated.setdefault("workspace_baseline", {})
        migrated.setdefault(
            "failed_tool_call_count",
            sum(
                1
                for execution in migrated.get("tool_executions", [])
                if isinstance(execution, dict) and execution.get("status") == "failed"
            ),
        )
        migrated.setdefault("invalid_tool_call_count", 0)
        migrated.setdefault("repeated_read_hint_count", 0)
    return migrated


def _validate_model_summary(model: Any) -> None:
    if not isinstance(model, dict):
        raise SessionError("session model must be an object")
    unsafe = sorted(set(model) - _SAFE_MODEL_FIELDS)
    if unsafe:
        raise SessionError(
            "session model contains unsupported or sensitive field(s): " + ", ".join(unsafe)
        )


def _required_string(data: dict[str, Any], name: str, *, allow_empty: bool = False) -> str:
    value = data.get(name)
    if not isinstance(value, str) or (not allow_empty and not value):
        suffix = "a string" if allow_empty else "a non-empty string"
        raise SessionError(f"session field {name!r} must be {suffix}")
    return value


def _optional_string(data: dict[str, Any], name: str) -> str | None:
    value = data.get(name)
    if value is None:
        return None
    if not isinstance(value, str):
        raise SessionError(f"session field {name!r} must be a string or null")
    return value


def _required_int(data: dict[str, Any], name: str, *, minimum: int) -> int:
    value = data.get(name)
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise SessionError(f"session field {name!r} must be an integer >= {minimum}")
    return value


def _required_number(data: dict[str, Any], name: str, *, minimum: float) -> float:
    value = data.get(name)
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or value < minimum
    ):
        raise SessionError(f"session field {name!r} must be a number >= {minimum}")
    return float(value)


def _optional_bool(data: dict[str, Any], name: str) -> bool | None:
    value = data.get(name)
    if value is None:
        return None
    if not isinstance(value, bool):
        raise SessionError(f"session field {name!r} must be a boolean or null")
    return value


def _optional_int(data: dict[str, Any], name: str) -> int | None:
    value = data.get(name)
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool):
        raise SessionError(f"session field {name!r} must be an integer or null")
    return value


def _optional_number(
    data: dict[str, Any],
    name: str,
    *,
    minimum: float,
) -> float | None:
    value = data.get(name)
    if value is None:
        return None
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or value < minimum
    ):
        raise SessionError(f"session field {name!r} must be a number >= {minimum} or null")
    return float(value)
