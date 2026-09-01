from __future__ import annotations

from typing import Any

from ..tools.base import RiskLevel, Tool, ToolContext, ToolResult
from .coordinator import ParallelSubagentCoordinator
from .models import SubagentError


class DelegateSubagentsTool(Tool):
    name = "delegate_subagents"
    description = (
        "Run one or two bounded Subagents concurrently. Use only when the task has "
        "independent substantial slices or genuinely separate investigation hypotheses. "
        "Scout is read-only. Implementer writes only to an isolated copy and returns a "
        "pending patch; it does not change the real workspace. Do not use for a simple, "
        "already-located change."
    )
    risk = RiskLevel.READ
    parameters = {
        "type": "object",
        "properties": {
            "tasks": {
                "type": "array",
                "minItems": 1,
                "maxItems": 2,
                "items": {
                    "type": "object",
                    "properties": {
                        "agent_id": {"type": "string"},
                        "label": {"type": "string"},
                        "role": {"type": "string", "enum": ["scout", "implementer"]},
                        "task": {"type": "string"},
                        "allowed_paths": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": (
                                "Workspace-relative files or directory patterns owned by "
                                "this Implementer. A bare directory such as backend includes "
                                "its descendants; backend/** is also accepted."
                            ),
                        },
                        "acceptance": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                    },
                    "required": ["agent_id", "role", "task"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["tasks"],
        "additionalProperties": False,
    }

    def __init__(self, coordinator: ParallelSubagentCoordinator) -> None:
        self.coordinator = coordinator

    def execute(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        if context.is_cancelled():
            return ToolResult(False, "Subagent delegation was cancelled")
        try:
            result = self.coordinator.delegate(list(arguments.get("tasks") or []))
        except SubagentError as exc:
            return ToolResult(False, str(exc), {"error_code": "subagent_error"})
        return ToolResult(
            True,
            "Parallel Subagents finished. Use their concise reports and apply any pending "
            "bundle only when it satisfies the parent task.",
            result,
        )


class ApplySubagentPatchesTool(Tool):
    name = "apply_subagent_patches"
    description = (
        "Apply one or more non-overlapping pending Subagent patch bundles to the real "
        "workspace as one tracked, conflict-checked batch. This is the only step that "
        "writes Subagent changes to the user's project and therefore requires approval."
    )
    risk = RiskLevel.WRITE
    parameters = {
        "type": "object",
        "properties": {
            "bundle_ids": {
                "type": "array",
                "minItems": 1,
                "maxItems": 2,
                "items": {"type": "string"},
            }
        },
        "required": ["bundle_ids"],
        "additionalProperties": False,
    }

    def __init__(self, coordinator: ParallelSubagentCoordinator) -> None:
        self.coordinator = coordinator

    def execute(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        if context.is_cancelled():
            return ToolResult(False, "Subagent patch application was cancelled")
        try:
            result = self.coordinator.apply_bundles(
                [str(item) for item in arguments.get("bundle_ids") or []]
            )
        except SubagentError as exc:
            return ToolResult(False, str(exc), {"error_code": "subagent_patch_error"})
        return ToolResult(
            True,
            f"Applied {result['file_count']} Subagent-produced file change(s).",
            result,
        )
