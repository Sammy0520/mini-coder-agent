from __future__ import annotations

import copy
import re
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import PurePosixPath
from typing import Any

from ..exceptions import MiniCoderError


class SubagentError(MiniCoderError):
    pass


class SubagentRole(str, Enum):
    SCOUT = "scout"
    IMPLEMENTER = "implementer"


class SubagentStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    PATCH_PENDING = "patch_pending"
    PATCH_APPLIED = "patch_applied"
    CONFLICTED = "conflicted"
    SCOPE_VIOLATION = "scope_violation"


_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


def _clean_relative_pattern(value: str) -> str:
    normalized = value.strip().replace("\\", "/").lstrip("./")
    if not normalized or normalized.startswith("/") or ":" in normalized:
        raise SubagentError(f"allowed path must be workspace-relative: {value!r}")
    if ".." in PurePosixPath(normalized).parts:
        raise SubagentError(f"allowed path cannot traverse upward: {value!r}")
    return normalized


@dataclass(frozen=True, slots=True)
class SubagentSpec:
    agent_id: str
    role: SubagentRole
    task: str
    label: str = ""
    allowed_paths: tuple[str, ...] = ()
    acceptance: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not _ID_PATTERN.fullmatch(self.agent_id):
            raise SubagentError(
                "agent_id must contain only letters, digits, underscores, or hyphens"
            )
        if not self.task.strip():
            raise SubagentError("Subagent task must not be empty")
        if len(self.task) > 4_000:
            raise SubagentError("Subagent task must not exceed 4000 characters")
        if self.role == SubagentRole.IMPLEMENTER and not self.allowed_paths:
            raise SubagentError("Implementer requires at least one allowed path")
        if len(self.allowed_paths) > 20:
            raise SubagentError("Subagent cannot own more than 20 path patterns")
        for pattern in self.allowed_paths:
            _clean_relative_pattern(pattern)

    @classmethod
    def from_dict(cls, data: Any) -> "SubagentSpec":
        if not isinstance(data, dict):
            raise SubagentError("Each Subagent task must be an object")
        raw_id = data.get("agent_id") or f"worker_{uuid.uuid4().hex[:8]}"
        raw_role = data.get("role")
        try:
            role = SubagentRole(str(raw_role))
        except ValueError as exc:
            raise SubagentError("Subagent role must be scout or implementer") from exc
        raw_paths = data.get("allowed_paths") or []
        raw_acceptance = data.get("acceptance") or []
        if not isinstance(raw_paths, list) or not all(
            isinstance(item, str) for item in raw_paths
        ):
            raise SubagentError("allowed_paths must be a list of strings")
        if not isinstance(raw_acceptance, list) or not all(
            isinstance(item, str) for item in raw_acceptance
        ):
            raise SubagentError("acceptance must be a list of strings")
        return cls(
            agent_id=str(raw_id),
            role=role,
            task=str(data.get("task") or "").strip(),
            label=str(data.get("label") or "").strip()[:80],
            allowed_paths=tuple(_clean_relative_pattern(item) for item in raw_paths),
            acceptance=tuple(item.strip() for item in raw_acceptance if item.strip()),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "role": self.role.value,
            "task": self.task,
            "label": self.label or self.agent_id,
            "allowed_paths": list(self.allowed_paths),
            "acceptance": list(self.acceptance),
        }

    def render_task(self) -> str:
        lines = [
            f"Assigned role: {self.role.value}",
            f"Subtask: {self.task}",
        ]
        if self.allowed_paths:
            lines.append("Authorized paths:\n- " + "\n- ".join(self.allowed_paths))
        if self.acceptance:
            lines.append("Acceptance checks:\n- " + "\n- ".join(self.acceptance))
        if self.role == SubagentRole.SCOUT:
            lines.append(
                "Return a concise evidence report with candidate files, relevant symbols "
                "or line ranges, the recommended next action, confidence, and unknowns."
            )
        else:
            lines.append(
                "Complete only this bounded implementation. Modify no path outside the "
                "authorized patterns. Run the smallest relevant local verification and "
                "finish with a concise change and verification summary."
            )
        return "\n\n".join(lines)


@dataclass(frozen=True, slots=True)
class PatchFile:
    path: str
    before_hash: str | None
    after_hash: str
    after_text: str
    encoding: str
    newline: str
    unified_diff: str
    additions: int
    deletions: int

    def to_dict(self, *, include_content: bool = False) -> dict[str, Any]:
        result = {
            "path": self.path,
            "before_hash": self.before_hash,
            "after_hash": self.after_hash,
            "encoding": self.encoding,
            "newline": self.newline,
            "unified_diff": self.unified_diff,
            "additions": self.additions,
            "deletions": self.deletions,
        }
        if include_content:
            result["after_text"] = self.after_text
        return result


@dataclass(slots=True)
class PatchBundle:
    bundle_id: str
    agent_id: str
    files: list[PatchFile]
    verification: list[dict[str, Any]] = field(default_factory=list)
    status: SubagentStatus = SubagentStatus.PATCH_PENDING

    def to_dict(self, *, include_content: bool = False) -> dict[str, Any]:
        return {
            "bundle_id": self.bundle_id,
            "agent_id": self.agent_id,
            "status": self.status.value,
            "file_count": len(self.files),
            "additions": sum(item.additions for item in self.files),
            "deletions": sum(item.deletions for item in self.files),
            "files": [
                item.to_dict(include_content=include_content) for item in self.files
            ],
            "verification": copy.deepcopy(self.verification),
        }


@dataclass(slots=True)
class WorkerOutcome:
    status: str
    final_text: str
    usage: dict[str, int] = field(default_factory=dict)
    model_calls: int = 0
    tool_calls: int = 0
    verification: list[dict[str, Any]] = field(default_factory=list)
    context_metrics: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class SubagentResult:
    spec: SubagentSpec
    status: SubagentStatus
    final_text: str = ""
    usage: dict[str, int] = field(default_factory=dict)
    model_calls: int = 0
    tool_calls: int = 0
    bundle_id: str | None = None
    error: str | None = None
    context_metrics: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.spec.to_dict(),
            "status": self.status.value,
            "summary": self.final_text[:2_000],
            "usage": dict(self.usage),
            "model_calls": self.model_calls,
            "tool_calls": self.tool_calls,
            "bundle_id": self.bundle_id,
            "error": self.error[:2_000] if self.error else None,
            "context_metrics": copy.deepcopy(self.context_metrics),
        }
