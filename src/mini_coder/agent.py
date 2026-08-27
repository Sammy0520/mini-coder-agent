from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .config import AgentConfig, ApprovalPolicy
from .context import ContextManager
from .exceptions import ModelError
from .messages import ToolCall
from .model import ModelClient
from .prompts import build_system_prompt
from .tools.base import RiskLevel, Tool, ToolContext, ToolResult
from .tools.registry import ToolRegistry
from .tools.safety import WorkspacePolicy

EventCallback = Callable[[str, dict[str, Any]], None]
ApprovalCallback = Callable[[Tool, dict[str, Any]], bool]


@dataclass(slots=True)
class AgentRunResult:
    status: str
    final_text: str
    steps: int
    messages: list[dict[str, Any]] = field(default_factory=list)
    total_usage: dict[str, int] = field(default_factory=dict)


class AgentRunner:
    def __init__(
        self,
        *,
        model: ModelClient,
        registry: ToolRegistry,
        config: AgentConfig,
        system_prompt: str | None = None,
        approval_callback: ApprovalCallback | None = None,
        event_callback: EventCallback | None = None,
    ) -> None:
        self.model = model
        self.registry = registry
        self.config = config
        self.system_prompt = build_system_prompt() if system_prompt is None else system_prompt
        self.approval_callback = approval_callback
        self.event_callback = event_callback
        self.context = ContextManager(config.max_context_chars)
        self.tool_context = ToolContext(
            policy=WorkspacePolicy(config.workspace),
            command_timeout_seconds=config.command_timeout_seconds,
            max_output_chars=config.max_tool_output_chars,
        )

    def run(self, task: str) -> AgentRunResult:
        task = task.strip()
        if not task:
            return AgentRunResult("invalid_task", "Task must not be empty.", 0)

        messages: list[dict[str, Any]] = [
            {"role": "system", "content": self.system_prompt},
            {"role": "user", "content": task},
        ]
        usage: dict[str, int] = {}
        previous_signature: str | None = None
        repeated_count = 0

        for step in range(1, self.config.max_steps + 1):
            prepared = self.context.prepare(messages)
            self._emit(
                "model_request",
                {
                    "step": step,
                    "history_messages": len(messages),
                    "sent_messages": len(prepared),
                    "estimated_chars": self.context.estimate_chars(prepared),
                },
            )
            try:
                response = self.model.complete(prepared, self.registry.definitions())
            except ModelError as exc:
                self._emit("model_error", {"step": step, "error": str(exc)})
                return AgentRunResult("model_error", str(exc), step, messages, usage)

            for name, value in response.usage.items():
                usage[name] = usage.get(name, 0) + value
            messages.append(response.as_assistant_message())
            self._emit(
                "model_response",
                {
                    "step": step,
                    "content": response.content,
                    "tool_calls": [call.name for call in response.tool_calls],
                    "finish_reason": response.finish_reason,
                },
            )

            if not response.tool_calls:
                final = response.content.strip()
                if final:
                    return AgentRunResult("completed", final, step, messages, usage)
                return AgentRunResult(
                    "empty_response",
                    "The model returned neither text nor tool calls.",
                    step,
                    messages,
                    usage,
                )

            stop_for_repetition = False
            for call in response.tool_calls:
                signature = self._call_signature(call)
                if signature == previous_signature:
                    repeated_count += 1
                else:
                    previous_signature = signature
                    repeated_count = 1

                if repeated_count >= self.config.repeated_call_limit:
                    result = ToolResult(
                        False,
                        f"Stopped: the same tool call was requested {repeated_count} consecutive times.",
                    )
                    stop_for_repetition = True
                else:
                    result = self._execute_call(call)

                content = result.to_model_content(self.config.max_tool_output_chars)
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.id,
                        "content": content,
                    }
                )
                self._emit(
                    "tool_result",
                    {
                        "step": step,
                        "tool": call.name,
                        "ok": result.ok,
                        "content": content,
                    },
                )

            if stop_for_repetition:
                return AgentRunResult(
                    "repeated_call",
                    "Stopped because the model repeated the same tool call without making progress.",
                    step,
                    messages,
                    usage,
                )

        return AgentRunResult(
            "max_steps",
            f"Stopped after reaching the {self.config.max_steps}-step limit.",
            self.config.max_steps,
            messages,
            usage,
        )

    def _execute_call(self, call: ToolCall) -> ToolResult:
        self._emit(
            "tool_request",
            {"tool": call.name, "arguments": call.arguments, "raw_arguments": call.raw_arguments},
        )
        if call.parse_error or call.arguments is None:
            return ToolResult(False, f"Tool arguments were not valid JSON: {call.parse_error}")

        tool = self.registry.get(call.name)
        if tool is None:
            return ToolResult(False, f"Unknown tool: {call.name}")
        if not self._approved(tool, call.arguments):
            return ToolResult(False, f"User denied {tool.risk.value} operation: {tool.name}")
        return self.registry.execute(call.name, call.arguments, self.tool_context)

    def _approved(self, tool: Tool, arguments: dict[str, Any]) -> bool:
        if tool.risk == RiskLevel.READ or self.config.approval_policy == ApprovalPolicy.AUTO:
            return True
        if self.approval_callback is None:
            return False
        return bool(self.approval_callback(tool, arguments))

    @staticmethod
    def _call_signature(call: ToolCall) -> str:
        arguments = call.arguments if call.arguments is not None else call.raw_arguments
        return call.name + ":" + json.dumps(arguments, sort_keys=True, ensure_ascii=False, default=str)

    def _emit(self, name: str, payload: dict[str, Any]) -> None:
        if self.event_callback is not None:
            self.event_callback(name, payload)
