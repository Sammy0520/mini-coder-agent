from __future__ import annotations

import copy
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from ..exceptions import SessionError

CURRENT_SESSION_SCHEMA = 1
_SESSION_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_SAFE_MODEL_FIELDS = {
    "provider",
    "model",
    "wire_api",
    "reasoning_effort",
    "verbosity",
    "approval_policy",
    "max_steps",
}


class SessionStatus(str, Enum):
    CREATED = "created"
    RUNNING = "running"
    WAITING_FOR_APPROVAL = "waiting_for_approval"
    INTERRUPTED = "interrupted"
    FAILED = "failed"
    COMPLETED_VERIFIED = "completed_verified"
    COMPLETED_UNVERIFIED = "completed_unverified"
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
        SessionStatus.CANCELLED,
    },
    SessionStatus.WAITING_FOR_APPROVAL: {
        SessionStatus.RUNNING,
        SessionStatus.INTERRUPTED,
        SessionStatus.FAILED,
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
    SessionStatus.COMPLETED_VERIFIED: set(),
    SessionStatus.COMPLETED_UNVERIFIED: set(),
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
        _validate_model_summary(self.model)

    @classmethod
    def create(
        cls,
        *,
        task: str,
        workspace: str | Path,
        model: dict[str, Any] | None = None,
        messages: list[dict[str, Any]] | None = None,
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
        )

    def set_status(
        self,
        status: SessionStatus,
        *,
        stop_reason: str | None = None,
        final_text: str | None = None,
        last_error: str | None = None,
    ) -> None:
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
        return cls(
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
            stop_reason=_optional_string(data, "stop_reason"),
            final_text=_required_string(data, "final_text", allow_empty=True),
            last_error=_optional_string(data, "last_error"),
            previous_call_signature=_optional_string(data, "previous_call_signature"),
            repeated_call_count=_required_int(data, "repeated_call_count", minimum=0),
        )


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


def _optional_bool(data: dict[str, Any], name: str) -> bool | None:
    value = data.get(name)
    if value is None:
        return None
    if not isinstance(value, bool):
        raise SessionError(f"session field {name!r} must be a boolean or null")
    return value
