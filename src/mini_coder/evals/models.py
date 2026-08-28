from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from ..messages import ModelResponse


EvalDriver = Literal["agent", "resume", "undo_conflict"]


@dataclass(frozen=True, slots=True)
class EvalScenario:
    """A complete, resettable evaluation case.

    Files and scripted responses are immutable declarations. The runner copies
    the files into a fresh workspace for every attempt.
    """

    scenario_id: str
    description: str
    task: str
    files: dict[str, str]
    responses: tuple[ModelResponse | BaseException, ...]
    expected_result_status: str = "completed"
    expected_session_status: str = "completed_verified"
    expected_changed_paths: frozenset[str] = frozenset()
    expected_content: dict[str, tuple[str, ...]] = field(default_factory=dict)
    validation_command: tuple[str, ...] | None = None
    driver: EvalDriver = "agent"
    approval: Literal["auto", "deny"] = "auto"
    min_verification_runs: int = 0
    require_failed_then_passed: bool = False
    require_tool_failure: bool = False
    require_output_truncated: bool = False
    require_retry: bool = False
    max_tool_output_chars: int = 12_000
    live_supported: bool = True
    tags: tuple[str, ...] = ()


@dataclass(slots=True)
class EvalResult:
    scenario_id: str
    description: str
    passed: bool
    live: bool
    checks: dict[str, bool]
    result_status: str
    session_status: str | None
    stop_reason: str | None
    verification_status: str | None
    external_validation_passed: bool | None
    workspace_boundary_attempted: bool
    workspace_boundary_violated: bool
    unrelated_changes: list[str]
    changed_paths: list[str]
    model_calls: int
    tool_calls: int
    retries: int
    failed_tool_calls: int
    invalid_tool_calls: int
    duration_seconds: float
    additions: int
    deletions: int
    usage: dict[str, int]
    usage_available: bool
    session_id: str | None
    artifact_directory: str | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class EvalReport:
    schema_version: int
    mode: Literal["deterministic", "live"]
    started_at: str
    finished_at: str
    duration_seconds: float
    passed: int
    failed: int
    total: int
    results: list[EvalResult]

    @property
    def success(self) -> bool:
        return self.failed == 0 and self.total > 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "mode": self.mode,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_seconds": self.duration_seconds,
            "passed": self.passed,
            "failed": self.failed,
            "total": self.total,
            "success": self.success,
            "results": [item.to_dict() for item in self.results],
        }
