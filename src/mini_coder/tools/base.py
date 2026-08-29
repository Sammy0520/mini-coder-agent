from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from collections.abc import Callable
from typing import Any

from .safety import WorkspacePolicy


class RiskLevel(str, Enum):
    READ = "read"
    WRITE = "write"
    EXECUTE = "execute"


@dataclass(slots=True)
class ToolContext:
    policy: WorkspacePolicy
    command_timeout_seconds: int = 60
    max_output_chars: int = 12_000
    cancellation_requested: Callable[[], bool] | None = None

    def is_cancelled(self) -> bool:
        return bool(self.cancellation_requested and self.cancellation_requested())


@dataclass(slots=True)
class ToolResult:
    ok: bool
    message: str
    data: dict[str, Any] = field(default_factory=dict)

    def to_model_content(self, max_chars: int) -> str:
        payload = json.dumps(
            {"ok": self.ok, "message": self.message, **self.data},
            ensure_ascii=False,
            default=str,
        )
        return truncate_text(payload, max_chars)


def truncate_text(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    removed = len(text) - limit
    marker = f"\n...[truncated {removed} characters]"
    keep = max(0, limit - len(marker))
    return text[:keep] + marker


class Tool(ABC):
    name: str
    description: str
    parameters: dict[str, Any]
    risk: RiskLevel = RiskLevel.READ

    def definition(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }

    @abstractmethod
    def execute(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        """Execute a locally implemented tool."""
