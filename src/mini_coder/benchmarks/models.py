from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal


AgentKind = Literal["mini", "codex"]


@dataclass(frozen=True, slots=True)
class BenchmarkTask:
    task_id: str
    title: str
    category: str
    fixture: Path
    hidden: Path | None
    turns: tuple[str, ...]
    validation_command: tuple[str, ...]
    expected_changed_paths: frozenset[str]
    timeout_seconds: int = 900


@dataclass(frozen=True, slots=True)
class ComparisonConfig:
    model_provider: str
    base_url: str
    wire_api: str
    model: str
    reasoning_effort: str
    verbosity: str


@dataclass(slots=True)
class BenchmarkResult:
    task_id: str
    title: str
    category: str
    agent: AgentKind
    started_at: str
    finished_at: str
    passed: bool
    process_completed: bool
    validation_passed: bool
    changed_paths_match: bool
    return_codes: list[int]
    duration_seconds: float
    turns: int
    model_calls: int | None
    tool_calls: int | None
    usage: dict[str, int]
    cache: dict[str, Any]
    provider_models: list[str]
    provider_response_ids: list[str]
    changed_paths: list[str]
    expected_changed_paths: list[str]
    thread_or_session_id: str | None
    workspace: str
    artifacts: str
    error: str | None = None
    actual_cost: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class BenchmarkReport:
    schema_version: int
    started_at: str
    finished_at: str
    comparison: ComparisonConfig
    results: list[BenchmarkResult] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "comparison": asdict(self.comparison),
            "results": [item.to_dict() for item in self.results],
        }
