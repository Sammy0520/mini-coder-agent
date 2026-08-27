from __future__ import annotations

import copy
import json
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import AgentConfig, ApprovalPolicy
from .context import ContextManager
from .exceptions import ModelError, SessionError
from .messages import ModelResponse, ToolCall
from .model import ModelClient
from .prompts import build_system_prompt
from .session import (
    AgentSession,
    SessionStatus,
    SessionStore,
    ToolExecutionRecord,
    ToolExecutionStatus,
)
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
    session_id: str | None = None


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
        session_store: SessionStore | None = None,
    ) -> None:
        self.model = model
        self.registry = registry
        self.config = config
        self.system_prompt = build_system_prompt() if system_prompt is None else system_prompt
        self.approval_callback = approval_callback
        self.event_callback = event_callback
        self.session_store = session_store
        self.context = ContextManager(config.max_context_chars)
        self.tool_context = ToolContext(
            policy=WorkspacePolicy(config.workspace),
            command_timeout_seconds=config.command_timeout_seconds,
            max_output_chars=config.max_tool_output_chars,
        )

    def run(
        self,
        task: str,
        *,
        session: AgentSession | None = None,
    ) -> AgentRunResult:
        task = task.strip()
        if session is None and not task:
            return AgentRunResult("invalid_task", "Task must not be empty.", 0)

        if session is None:
            messages: list[dict[str, Any]] = [
                {"role": "system", "content": self.system_prompt},
                {"role": "user", "content": task},
            ]
            usage: dict[str, int] = {}
            previous_signature: str | None = None
            repeated_count = 0
            if self.session_store is not None:
                session = AgentSession.create(
                    task=task,
                    workspace=self.config.workspace,
                    model=self._model_summary(),
                    messages=messages,
                )
                session.set_status(SessionStatus.RUNNING)
                self._persist(session, messages, usage)
                self._emit(
                    "session_created",
                    {
                        "session_id": session.session_id,
                        "path": str(self.session_store.path_for(session.session_id)),
                        "status": session.status.value,
                    },
                )
        else:
            self._validate_resume(session, task)
            task = session.task
            messages = copy.deepcopy(session.messages)
            usage = dict(session.total_usage)
            previous_signature = session.previous_call_signature
            repeated_count = session.repeated_call_count

            if session.status in {
                SessionStatus.COMPLETED_VERIFIED,
                SessionStatus.COMPLETED_UNVERIFIED,
            }:
                return AgentRunResult(
                    "completed",
                    session.final_text,
                    session.current_step,
                    messages,
                    usage,
                    session.session_id,
                )

            uncertain = self._mark_interrupted_tools_uncertain(session)
            if uncertain:
                names = ", ".join(f"{item.name} ({item.tool_call_id})" for item in uncertain)
                final = (
                    "Resume stopped because these tool calls may have produced side effects "
                    f"before the previous process ended: {names}. Inspect the workspace and "
                    "resolve them before continuing."
                )
                return self._finish(
                    "resume_attention",
                    final,
                    session.current_step,
                    messages,
                    usage,
                    session,
                    session_status=SessionStatus.INTERRUPTED,
                    stop_reason="uncertain_tool_execution",
                )

            session.set_status(SessionStatus.RUNNING)
            self._persist(session, messages, usage)
            self._emit(
                "session_resumed",
                {
                    "session_id": session.session_id,
                    "path": (
                        str(self.session_store.path_for(session.session_id))
                        if self.session_store is not None
                        else None
                    ),
                    "status": session.status.value,
                    "current_step": session.current_step,
                },
            )

        try:
            if session is not None:
                repeated_stop = self._resume_requested_tools(session, messages, usage)
                if repeated_stop:
                    return self._finish(
                        "repeated_call",
                        "Stopped because the model repeated the same tool call without making progress.",
                        session.current_step,
                        messages,
                        usage,
                        session,
                        session_status=SessionStatus.FAILED,
                        stop_reason="repeated_call",
                    )

            first_step = (session.current_step + 1) if session is not None else 1
            for step in range(first_step, self.config.max_steps + 1):
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
                if session is not None:
                    self._persist(session, messages, usage)
                try:
                    response = self.model.complete(prepared, self.registry.definitions())
                except ModelError as exc:
                    safe_error = self._redact_configured_api_key(str(exc))
                    self._emit("model_error", {"step": step, "error": safe_error})
                    return self._finish(
                        "model_error",
                        safe_error,
                        step,
                        messages,
                        usage,
                        session,
                        session_status=SessionStatus.FAILED,
                        stop_reason="model_error",
                        last_error=safe_error,
                    )

                self._accumulate_usage(usage, response)
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

                repeated_flags: list[bool] = []
                records: list[ToolExecutionRecord | None] = []
                for call in response.tool_calls:
                    signature = self._call_signature(call)
                    if signature == previous_signature:
                        repeated_count += 1
                    else:
                        previous_signature = signature
                        repeated_count = 1

                    record = self._new_tool_record(call, step, session)
                    records.append(record)
                    repeated = repeated_count >= self.config.repeated_call_limit
                    repeated_flags.append(repeated)
                    if record is not None and repeated:
                        record.error = "repeated_call_limit"

                if session is not None:
                    session.current_step = step
                    session.previous_call_signature = previous_signature
                    session.repeated_call_count = repeated_count
                    self._persist(session, messages, usage)

                if not response.tool_calls:
                    final = response.content.strip()
                    if final:
                        return self._finish(
                            "completed",
                            final,
                            step,
                            messages,
                            usage,
                            session,
                            session_status=SessionStatus.COMPLETED_UNVERIFIED,
                            stop_reason="model_completed",
                        )
                    return self._finish(
                        "empty_response",
                        "The model returned neither text nor tool calls.",
                        step,
                        messages,
                        usage,
                        session,
                        session_status=SessionStatus.FAILED,
                        stop_reason="empty_response",
                    )

                stop_for_repetition = False
                for call, record, repeated in zip(
                    response.tool_calls,
                    records,
                    repeated_flags,
                    strict=True,
                ):
                    if repeated:
                        result = ToolResult(
                            False,
                            f"Stopped: the same tool call was requested {repeated_count} "
                            "consecutive times.",
                        )
                        self._record_tool_result(
                            call,
                            record,
                            result,
                            messages,
                            usage,
                            session,
                            status=ToolExecutionStatus.FAILED,
                        )
                        stop_for_repetition = True
                    else:
                        self._process_tool_call(call, record, messages, usage, session)

                if stop_for_repetition:
                    return self._finish(
                        "repeated_call",
                        "Stopped because the model repeated the same tool call without making progress.",
                        step,
                        messages,
                        usage,
                        session,
                        session_status=SessionStatus.FAILED,
                        stop_reason="repeated_call",
                    )

            return self._finish(
                "max_steps",
                f"Stopped after reaching the {self.config.max_steps}-step limit.",
                self.config.max_steps,
                messages,
                usage,
                session,
                session_status=SessionStatus.FAILED,
                stop_reason="max_steps",
            )
        except KeyboardInterrupt:
            if session is not None:
                session.set_status(
                    SessionStatus.INTERRUPTED,
                    stop_reason="keyboard_interrupt",
                    last_error="Interrupted by user",
                )
                self._persist(session, messages, usage)
                self._emit(
                    "run_interrupted",
                    {
                        "session_id": session.session_id,
                        "session_status": session.status.value,
                        "step": session.current_step,
                        "stop_reason": session.stop_reason,
                    },
                )
            raise

    def _validate_resume(self, session: AgentSession, task: str) -> None:
        expected_workspace = Path(session.workspace).expanduser().resolve()
        if not expected_workspace.is_dir():
            raise SessionError(
                f"saved session workspace no longer exists or is not a directory: "
                f"{expected_workspace}"
            )
        if expected_workspace != self.config.workspace.resolve():
            raise SessionError(
                f"session workspace {expected_workspace} does not match configured workspace "
                f"{self.config.workspace.resolve()}"
            )
        if task and task != session.task:
            raise SessionError("a resumed session cannot be given a different task")
        if not session.messages:
            raise SessionError("session has no conversation messages to resume")
        if session.status == SessionStatus.CANCELLED:
            raise SessionError("cancelled sessions cannot be resumed")

    def _model_summary(self) -> dict[str, Any]:
        return {
            "provider": self.config.model_provider,
            "model": self.config.model,
            "wire_api": self.config.wire_api.value,
            "reasoning_effort": self.config.model_reasoning_effort,
            "verbosity": self.config.model_verbosity,
            "approval_policy": self.config.approval_policy.value,
            "max_steps": self.config.max_steps,
        }

    @staticmethod
    def _accumulate_usage(usage: dict[str, int], response: ModelResponse) -> None:
        for name, value in response.usage.items():
            usage[name] = usage.get(name, 0) + value

    def _new_tool_record(
        self,
        call: ToolCall,
        step: int,
        session: AgentSession | None,
    ) -> ToolExecutionRecord | None:
        if session is None:
            return None
        tool = self.registry.get(call.name)
        record = ToolExecutionRecord.create(
            execution_id=uuid.uuid4().hex,
            tool_call_id=call.id,
            step=step,
            name=call.name,
            arguments=call.arguments,
            raw_arguments=call.raw_arguments,
            risk=tool.risk.value if tool is not None else "unknown",
            parse_error=call.parse_error,
        )
        session.tool_executions.append(record)
        return record

    def _process_tool_call(
        self,
        call: ToolCall,
        record: ToolExecutionRecord | None,
        messages: list[dict[str, Any]],
        usage: dict[str, int],
        session: AgentSession | None,
        *,
        reuse_approval: bool = False,
    ) -> ToolResult:
        self._emit(
            "tool_request",
            {"tool": call.name, "arguments": call.arguments, "raw_arguments": call.raw_arguments},
        )
        if call.parse_error or call.arguments is None:
            result = ToolResult(False, f"Tool arguments were not valid JSON: {call.parse_error}")
            self._record_tool_result(
                call,
                record,
                result,
                messages,
                usage,
                session,
                status=ToolExecutionStatus.FAILED,
            )
            return result

        tool = self.registry.get(call.name)
        if tool is None:
            result = ToolResult(False, f"Unknown tool: {call.name}")
            self._record_tool_result(
                call,
                record,
                result,
                messages,
                usage,
                session,
                status=ToolExecutionStatus.FAILED,
            )
            return result

        approved = reuse_approval or tool.risk == RiskLevel.READ or (
            self.config.approval_policy == ApprovalPolicy.AUTO
        )
        if not approved:
            if session is not None:
                session.set_status(SessionStatus.WAITING_FOR_APPROVAL)
                self._persist(session, messages, usage)
            approved = self._ask_approval(tool, call.arguments)
            if session is not None:
                session.set_status(SessionStatus.RUNNING)

        if record is not None:
            record.approval_granted = approved

        if not approved:
            result = ToolResult(False, f"User denied {tool.risk.value} operation: {tool.name}")
            self._record_tool_result(
                call,
                record,
                result,
                messages,
                usage,
                session,
                status=ToolExecutionStatus.DENIED,
            )
            return result

        if record is not None:
            record.set_status(ToolExecutionStatus.APPROVED)
            if session is not None:
                self._persist(session, messages, usage)
            record.set_status(ToolExecutionStatus.RUNNING)
            if session is not None:
                self._persist(session, messages, usage)

        result = self.registry.execute(call.name, call.arguments, self.tool_context)
        self._record_tool_result(
            call,
            record,
            result,
            messages,
            usage,
            session,
            status=ToolExecutionStatus.COMPLETED if result.ok else ToolExecutionStatus.FAILED,
        )
        return result

    def _record_tool_result(
        self,
        call: ToolCall,
        record: ToolExecutionRecord | None,
        result: ToolResult,
        messages: list[dict[str, Any]],
        usage: dict[str, int],
        session: AgentSession | None,
        *,
        status: ToolExecutionStatus,
    ) -> None:
        content = result.to_model_content(self.config.max_tool_output_chars)
        messages.append(
            {
                "role": "tool",
                "tool_call_id": call.id,
                "content": content,
            }
        )
        if record is not None:
            record.result_content = content
            record.ok = result.ok
            record.error = None if result.ok else result.message
            record.set_status(status)
        if session is not None:
            self._persist(session, messages, usage)
        self._emit(
            "tool_result",
            {
                "step": record.step if record is not None else None,
                "tool": call.name,
                "ok": result.ok,
                "content": content,
            },
        )

    def _resume_requested_tools(
        self,
        session: AgentSession,
        messages: list[dict[str, Any]],
        usage: dict[str, int],
    ) -> bool:
        stop_for_repetition = False
        for record in session.tool_executions:
            if record.status not in {
                ToolExecutionStatus.REQUESTED,
                ToolExecutionStatus.APPROVED,
            }:
                continue
            repeated = record.error == "repeated_call_limit"
            call = ToolCall(
                id=record.tool_call_id,
                name=record.name,
                arguments=copy.deepcopy(record.arguments),
                raw_arguments=record.raw_arguments,
                parse_error=record.parse_error,
            )
            if repeated:
                self._record_tool_result(
                    call,
                    record,
                    ToolResult(False, "Stopped: repeated tool call limit was reached."),
                    messages,
                    usage,
                    session,
                    status=ToolExecutionStatus.FAILED,
                )
                stop_for_repetition = True
            else:
                self._process_tool_call(
                    call,
                    record,
                    messages,
                    usage,
                    session,
                    reuse_approval=record.status == ToolExecutionStatus.APPROVED,
                )
        return stop_for_repetition

    def _mark_interrupted_tools_uncertain(
        self,
        session: AgentSession,
    ) -> list[ToolExecutionRecord]:
        uncertain: list[ToolExecutionRecord] = []
        for record in session.tool_executions:
            if record.status == ToolExecutionStatus.RUNNING:
                record.set_status(ToolExecutionStatus.UNCERTAIN)
                uncertain.append(record)
            elif record.status == ToolExecutionStatus.UNCERTAIN:
                uncertain.append(record)
        if uncertain:
            if session.status in {SessionStatus.RUNNING, SessionStatus.WAITING_FOR_APPROVAL}:
                session.set_status(
                    SessionStatus.INTERRUPTED,
                    stop_reason="uncertain_tool_execution",
                )
            self._persist(session, session.messages, session.total_usage)
        return uncertain

    def _ask_approval(self, tool: Tool, arguments: dict[str, Any]) -> bool:
        if self.approval_callback is None:
            return False
        return bool(self.approval_callback(tool, arguments))

    def _persist(
        self,
        session: AgentSession,
        messages: list[dict[str, Any]],
        usage: dict[str, int],
    ) -> None:
        session.messages = copy.deepcopy(messages)
        session.total_usage = dict(usage)
        if self.session_store is not None:
            self.session_store.save(session)

    def _finish(
        self,
        status: str,
        final_text: str,
        steps: int,
        messages: list[dict[str, Any]],
        usage: dict[str, int],
        session: AgentSession | None,
        *,
        session_status: SessionStatus,
        stop_reason: str,
        last_error: str | None = None,
    ) -> AgentRunResult:
        if session is not None:
            session.set_status(
                session_status,
                stop_reason=stop_reason,
                final_text=final_text,
                last_error=last_error,
            )
            self._persist(session, messages, usage)
        self._emit(
            "run_finished",
            {
                "result_status": status,
                "session_id": session.session_id if session is not None else None,
                "session_status": session.status.value if session is not None else None,
                "steps": steps,
                "stop_reason": stop_reason,
            },
        )
        return AgentRunResult(
            status,
            final_text,
            steps,
            messages,
            usage,
            session.session_id if session is not None else None,
        )

    @staticmethod
    def _call_signature(call: ToolCall) -> str:
        arguments = call.arguments if call.arguments is not None else call.raw_arguments
        return call.name + ":" + json.dumps(
            arguments,
            sort_keys=True,
            ensure_ascii=False,
            default=str,
        )

    def _emit(self, name: str, payload: dict[str, Any]) -> None:
        if self.event_callback is not None:
            self.event_callback(name, payload)

    def _redact_configured_api_key(self, text: str) -> str:
        api_key = self.config.api_key
        if api_key:
            return text.replace(api_key, "[REDACTED]")
        return text
