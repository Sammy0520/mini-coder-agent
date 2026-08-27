from __future__ import annotations

import re
from typing import Any

from .models import VerificationRecord, VerificationStatus


_VERIFICATION_PATTERNS = (
    r"(?:^|\s)(?:python(?:\.exe)?\s+-m\s+)?(?:unittest|pytest)(?:\s|$)",
    r"(?:^|\s)(?:py\.test|tox|nox|vitest|jest)(?:\s|$)",
    r"(?:^|\s)(?:npm|pnpm|yarn|bun)\s+(?:test|run\s+(?:test|build|lint|check|typecheck))(?:\s|$)",
    r"(?:^|\s)cargo\s+(?:test|check|clippy)(?:\s|$)",
    r"(?:^|\s)go\s+test(?:\s|$)",
    r"(?:^|\s)dotnet\s+(?:test|build)(?:\s|$)",
    r"(?:^|\s)(?:mvn|mvnw|gradle|gradlew)(?:\.cmd|\.bat)?\s+.*(?:test|verify|check|build)(?:\s|$)",
    r"(?:^|\s)make\s+(?:test|check|lint)(?:\s|$)",
    r"(?:^|\s)(?:ruff\s+check|mypy|pyright|eslint|tsc)(?:\s|$)",
)


class VerificationTracker:
    """Derive verification state from commands and workspace change revisions."""

    @staticmethod
    def is_verification_command(arguments: dict[str, Any]) -> bool:
        purpose = arguments.get("purpose")
        if purpose == "verify":
            return True
        if purpose in {"inspect", "other"}:
            return False
        command = str(arguments.get("command", "")).strip().casefold()
        return any(re.search(pattern, command) for pattern in _VERIFICATION_PATTERNS)

    @staticmethod
    def record(
        *,
        tool_execution_id: str,
        arguments: dict[str, Any],
        result_data: dict[str, Any],
        result_ok: bool,
        change_revision: int,
        summary_limit: int = 800,
    ) -> VerificationRecord:
        return VerificationRecord.create(
            tool_execution_id=tool_execution_id,
            command=str(arguments.get("command", "")),
            cwd=str(arguments.get("cwd", ".")),
            exit_code=result_data.get("exit_code"),
            duration_seconds=float(result_data.get("duration_seconds", 0.0)),
            stdout_summary=_summary(result_data.get("stdout", ""), summary_limit),
            stderr_summary=_summary(result_data.get("stderr", ""), summary_limit),
            change_revision=change_revision,
            passed=bool(result_ok and result_data.get("exit_code") == 0),
            timed_out=bool(result_data.get("timed_out", False)),
        )

    @staticmethod
    def invalidate(
        records: list[VerificationRecord],
        *,
        reason: str,
    ) -> list[VerificationRecord]:
        invalidated = []
        for record in records:
            if record.is_current:
                record.invalidate(reason)
                invalidated.append(record)
        return invalidated

    @staticmethod
    def evaluate(
        records: list[VerificationRecord],
        *,
        change_revision: int,
        had_file_modification: bool,
    ) -> VerificationStatus:
        current = [
            record
            for record in records
            if record.is_current and record.change_revision == change_revision
        ]
        if current:
            return VerificationStatus.PASSED if current[-1].passed else VerificationStatus.FAILED
        if records:
            return VerificationStatus.STALE
        if had_file_modification:
            return VerificationStatus.UNVERIFIED
        return VerificationStatus.NOT_REQUIRED


def _summary(value: Any, limit: int) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 18)] + "...[truncated]"
