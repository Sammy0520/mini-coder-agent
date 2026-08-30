from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class TaskPhase(str, Enum):
    ANALYZE = "analyze"
    IMPLEMENT = "implement"
    VERIFY = "verify"
    SUMMARIZE = "summarize"


class VerificationStatus(str, Enum):
    NOT_REQUIRED = "not_required"
    UNVERIFIED = "unverified"
    PASSED = "passed"
    FAILED = "failed"
    STALE = "stale"


@dataclass(slots=True)
class VerificationRecord:
    verification_id: str
    tool_execution_id: str
    command: str
    cwd: str
    exit_code: int | None
    duration_seconds: float
    stdout_summary: str
    stderr_summary: str
    change_revision: int
    passed: bool
    timed_out: bool = False
    expected_exit_codes: tuple[int, ...] = (0,)
    expectation_met: bool = False
    scope_paths: tuple[str, ...] = ()
    scope_domains: tuple[str, ...] = ("all",)
    created_at: str = field(default_factory=utc_now)
    invalidated_at: str | None = None
    invalidation_reason: str | None = None

    @classmethod
    def create(
        cls,
        *,
        tool_execution_id: str,
        command: str,
        cwd: str,
        exit_code: int | None,
        duration_seconds: float,
        stdout_summary: str,
        stderr_summary: str,
        change_revision: int,
        passed: bool,
        timed_out: bool,
        expected_exit_codes: tuple[int, ...] = (0,),
        expectation_met: bool | None = None,
        scope_paths: tuple[str, ...] = (),
        scope_domains: tuple[str, ...] = ("all",),
    ) -> "VerificationRecord":
        return cls(
            verification_id=uuid.uuid4().hex,
            tool_execution_id=tool_execution_id,
            command=command,
            cwd=cwd,
            exit_code=exit_code,
            duration_seconds=duration_seconds,
            stdout_summary=stdout_summary,
            stderr_summary=stderr_summary,
            change_revision=change_revision,
            passed=passed,
            timed_out=timed_out,
            expected_exit_codes=expected_exit_codes,
            expectation_met=passed if expectation_met is None else expectation_met,
            scope_paths=scope_paths,
            scope_domains=scope_domains,
        )

    @property
    def is_current(self) -> bool:
        return self.invalidated_at is None

    def invalidate(self, reason: str) -> None:
        if self.invalidated_at is None:
            self.invalidated_at = utc_now()
            self.invalidation_reason = reason

    def to_dict(self) -> dict[str, Any]:
        return {
            "verification_id": self.verification_id,
            "tool_execution_id": self.tool_execution_id,
            "command": self.command,
            "cwd": self.cwd,
            "exit_code": self.exit_code,
            "duration_seconds": self.duration_seconds,
            "stdout_summary": self.stdout_summary,
            "stderr_summary": self.stderr_summary,
            "change_revision": self.change_revision,
            "passed": self.passed,
            "timed_out": self.timed_out,
            "expected_exit_codes": list(self.expected_exit_codes),
            "expectation_met": self.expectation_met,
            "scope_paths": list(self.scope_paths),
            "scope_domains": list(self.scope_domains),
            "created_at": self.created_at,
            "invalidated_at": self.invalidated_at,
            "invalidation_reason": self.invalidation_reason,
        }

    @classmethod
    def from_dict(cls, data: Any) -> "VerificationRecord":
        if not isinstance(data, dict):
            raise ValueError("verification record must be an object")
        required_strings = (
            "verification_id",
            "tool_execution_id",
            "command",
            "cwd",
            "stdout_summary",
            "stderr_summary",
            "created_at",
        )
        for name in required_strings:
            if not isinstance(data.get(name), str):
                raise ValueError(f"verification field {name!r} must be a string")
        exit_code = data.get("exit_code")
        if exit_code is not None and (
            not isinstance(exit_code, int) or isinstance(exit_code, bool)
        ):
            raise ValueError("verification exit_code must be an integer or null")
        duration = data.get("duration_seconds")
        if not isinstance(duration, (int, float)) or isinstance(duration, bool) or duration < 0:
            raise ValueError("verification duration_seconds must be non-negative")
        revision = data.get("change_revision")
        if not isinstance(revision, int) or isinstance(revision, bool) or revision < 0:
            raise ValueError("verification change_revision must be non-negative")
        passed = data.get("passed")
        timed_out = data.get("timed_out")
        if not isinstance(passed, bool) or not isinstance(timed_out, bool):
            raise ValueError("verification passed and timed_out must be booleans")
        raw_expected = data.get("expected_exit_codes", [0])
        if (
            not isinstance(raw_expected, list)
            or not raw_expected
            or any(not isinstance(item, int) or isinstance(item, bool) for item in raw_expected)
        ):
            raise ValueError("verification expected_exit_codes must be a non-empty integer list")
        expectation_met = data.get("expectation_met", passed)
        if not isinstance(expectation_met, bool):
            raise ValueError("verification expectation_met must be a boolean")
        scope_paths = data.get("scope_paths", [])
        scope_domains = data.get("scope_domains", ["all"])
        if not isinstance(scope_paths, list) or not all(
            isinstance(item, str) and item for item in scope_paths
        ):
            raise ValueError("verification scope_paths must be a string list")
        if not isinstance(scope_domains, list) or not scope_domains or not all(
            isinstance(item, str) and item for item in scope_domains
        ):
            raise ValueError("verification scope_domains must be a non-empty string list")
        invalidated_at = data.get("invalidated_at")
        invalidation_reason = data.get("invalidation_reason")
        if invalidated_at is not None and not isinstance(invalidated_at, str):
            raise ValueError("verification invalidated_at must be a string or null")
        if invalidation_reason is not None and not isinstance(invalidation_reason, str):
            raise ValueError("verification invalidation_reason must be a string or null")
        return cls(
            verification_id=data["verification_id"],
            tool_execution_id=data["tool_execution_id"],
            command=data["command"],
            cwd=data["cwd"],
            exit_code=exit_code,
            duration_seconds=float(duration),
            stdout_summary=data["stdout_summary"],
            stderr_summary=data["stderr_summary"],
            change_revision=revision,
            passed=passed,
            timed_out=timed_out,
            expected_exit_codes=tuple(raw_expected),
            expectation_met=expectation_met,
            scope_paths=tuple(scope_paths),
            scope_domains=tuple(scope_domains),
            created_at=data["created_at"],
            invalidated_at=invalidated_at,
            invalidation_reason=invalidation_reason,
        )
