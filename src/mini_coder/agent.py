from __future__ import annotations

import copy
import json
import random
import time
import uuid
import warnings
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .changes import ChangeTracker, PreparedChange
from .config import AgentConfig, ApprovalPolicy
from .context import ContextManager
from .exceptions import (
    ChangeError,
    ModelError,
    ModelErrorCategory,
    SessionError,
    ToolError,
)
from .messages import ModelResponse, ToolCall
from .model import ModelClient
from .prompts import build_system_prompt
from .redaction import redact_sensitive_text, redact_sensitive_value
from .session import (
    AgentSession,
    SessionStatus,
    SessionStore,
    ToolExecutionRecord,
    ToolExecutionStatus,
    TaskPhase,
    VerificationStatus,
)
from .tools.base import RiskLevel, Tool, ToolContext, ToolResult
from .tools.command_risk import CommandRisk, assess_command
from .tools.diagnostics import tool_error_data
from .workspace import (
    capture_git_snapshot,
    compare_git_snapshots,
    inspect_workspace,
    render_workspace_overview,
)
from .tools.registry import ToolRegistry
from .tools.safety import WorkspacePolicy
from .verification import VerificationTracker

EventCallback = Callable[[str, dict[str, Any]], None]
ApprovalCallback = Callable[[Tool, dict[str, Any]], bool]
BatchApprovalCallback = Callable[[list[tuple[Tool, dict[str, Any]]]], bool]
CancellationCallback = Callable[[], bool]


class _RunBudgetExceeded(Exception):
    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


class _RunCancelled(Exception):
    pass


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
        batch_approval_callback: BatchApprovalCallback | None = None,
        event_callback: EventCallback | None = None,
        cancellation_callback: CancellationCallback | None = None,
        session_store: SessionStore | None = None,
        session_title: str | None = None,
    ) -> None:
        self.model = model
        self.registry = registry
        self.config = config
        self.system_prompt = build_system_prompt() if system_prompt is None else system_prompt
        self.approval_callback = approval_callback
        self.batch_approval_callback = batch_approval_callback
        self.event_callback = event_callback
        self.cancellation_callback = cancellation_callback
        self.session_store = session_store
        self.session_title = session_title
        self.context = ContextManager(
            config.max_context_chars,
            max_tokens=config.max_context_tokens,
        )
        self.change_tracker = ChangeTracker(config.workspace)
        self.verification_tracker = VerificationTracker()
        self._run_started_at = 0.0
        self._run_id = ""
        self._active_session_id: str | None = None
        self._current_step = 0
        self._last_model_duration = 0.0
        self._continuing_turn = False
        self._run_model_call_start = 0
        self._run_tool_call_start = 0
        self._run_tool_output_start = 0
        self._run_total_token_start = 0
        self.tool_context = ToolContext(
            policy=WorkspacePolicy(config.workspace),
            command_timeout_seconds=config.command_timeout_seconds,
            max_output_chars=config.max_tool_output_chars,
            cancellation_requested=cancellation_callback,
        )

    def run(
        self,
        task: str,
        *,
        session: AgentSession | None = None,
    ) -> AgentRunResult:
        self._run_started_at = time.monotonic()
        self._run_id = uuid.uuid4().hex
        self._current_step = 0
        task = redact_sensitive_text(task.strip(), secrets=(self.config.api_key,))
        if session is None and not task:
            return AgentRunResult("invalid_task", "Task must not be empty.", 0)

        if session is None:
            workspace_overview = inspect_workspace(
                self.config.workspace,
                self.tool_context.policy,
            )
            messages: list[dict[str, Any]] = [
                {"role": "system", "content": self.system_prompt},
                {
                    "role": "developer",
                    "content": render_workspace_overview(workspace_overview),
                },
                {"role": "user", "content": task},
            ]
            usage: dict[str, int] = {}
            previous_signature: str | None = None
            repeated_count = 0
            if self.session_store is not None:
                session = AgentSession.create(
                    task=task,
                    title=self.session_title,
                    workspace=self.config.workspace,
                    model=self._model_summary(),
                    messages=messages,
                    workspace_baseline=workspace_overview,
                )
                session.set_status(SessionStatus.RUNNING)
                self._active_session_id = session.session_id
                self._persist(session, messages, usage)
                self._emit(
                    "session_created",
                    {
                        "session_id": session.session_id,
                        "path": str(self.session_store.path_for(session.session_id)),
                        "status": session.status.value,
                    },
                )
            self._emit(
                "workspace_overview_generated",
                {
                    "manifests": workspace_overview.get("manifests", []),
                    "test_paths": workspace_overview.get("test_paths", []),
                    "entry_points": workspace_overview.get("entry_points", []),
                    "verification_candidates": workspace_overview.get(
                        "verification_candidates",
                        [],
                    ),
                    "instruction_files": workspace_overview.get(
                        "instruction_files",
                        [],
                    ),
                    "scan": workspace_overview.get("scan", {}),
                },
            )
        else:
            self._active_session_id = session.session_id
            self._validate_resume(session, task)
            self._continuing_turn = bool(task.strip()) and session.status in {
                SessionStatus.COMPLETED_VERIFIED,
                SessionStatus.COMPLETED_UNVERIFIED,
                SessionStatus.FAILED,
                SessionStatus.DENIED,
                SessionStatus.INTERRUPTED,
            }
            if self._continuing_turn:
                session.start_follow_up(task)
            task = session.task
            messages = copy.deepcopy(session.messages)
            usage = dict(session.total_usage)
            previous_signature = session.previous_call_signature
            repeated_count = session.repeated_call_count

            self._reconcile_workspace_state(session, messages, usage)

            if not self._continuing_turn and session.status in {
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
                "session_turn_started" if self._continuing_turn else "session_resumed",
                {
                    "session_id": session.session_id,
                    "path": (
                        str(self.session_store.path_for(session.session_id))
                        if self.session_store is not None
                        else None
                    ),
                    "status": session.status.value,
                    "current_step": session.current_step,
                    "turn": session.turn_count,
                },
            )

        if session is not None and self._continuing_turn:
            self._run_model_call_start = session.model_call_count
            self._run_tool_call_start = len(session.tool_executions)
            self._run_tool_output_start = session.tool_output_chars
            self._run_total_token_start = int(session.total_usage.get("total_tokens", 0))

        self._emit(
            "run_started",
            {
                "session_id": session.session_id if session is not None else None,
                "resumed": session is not None and session.current_step > 0,
                "status": session.status.value if session is not None else "running",
            },
        )

        try:
            self._check_cancelled()
            if session is not None:
                budget_reason = self._budget_reason(session, before_model=False)
                if budget_reason is not None:
                    return self._finish_budget(
                        budget_reason,
                        session.current_step,
                        messages,
                        usage,
                        session,
                    )
                repeated_stop, resumed_budget_reason = self._resume_requested_tools(
                    session,
                    messages,
                    usage,
                )
                if resumed_budget_reason is not None:
                    return self._finish_budget(
                        resumed_budget_reason,
                        session.current_step,
                        messages,
                        usage,
                        session,
                    )
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
                self._current_step = step
                self._check_cancelled()
                if session is not None:
                    budget_reason = self._budget_reason(session, before_model=True)
                    if budget_reason is not None:
                        return self._finish_budget(
                            budget_reason,
                            step,
                            messages,
                            usage,
                            session,
                        )
                prepared = self.context.prepare(
                    messages,
                    memory=session.working_memory if session is not None else None,
                    follow_up=self._continuing_turn,
                )
                context_was_compacted = self._continuing_turn or len(prepared) < len(messages)
                if context_was_compacted:
                    self._emit(
                        "context_compacted",
                        {
                            "step": step,
                            "history_messages": len(messages),
                            "sent_messages": len(prepared),
                            "estimated_chars": self.context.estimate_chars(prepared),
                            "estimated_tokens": self.context.estimate_tokens(prepared),
                            "turn": session.turn_count if session is not None else 1,
                        },
                    )
                if session is not None:
                    self._persist(session, messages, usage)
                try:
                    response = self._complete_with_retry(
                        prepared,
                        step=step,
                        session=session,
                        messages=messages,
                        usage=usage,
                    )
                except _RunBudgetExceeded as exc:
                    return self._finish_budget(
                        exc.reason,
                        step,
                        messages,
                        usage,
                        session,
                        detail=str(exc),
                    )
                except ModelError as exc:
                    safe_error = redact_sensitive_text(
                        str(exc),
                        secrets=(self.config.api_key,),
                    )
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

                self._check_cancelled()

                response = self._redact_model_response(response)
                self._accumulate_usage(usage, response)
                messages.append(response.as_assistant_message())
                self._emit(
                    "model_response_received",
                    {
                        "step": step,
                        "content": response.content,
                        "tool_calls": [call.name for call in response.tool_calls],
                        "finish_reason": response.finish_reason,
                        "duration_seconds": self._last_model_duration,
                        "usage": dict(response.usage),
                        "estimated_input_tokens": self.context.estimate_tokens(prepared),
                        "turn": session.turn_count if session is not None else 1,
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

                if session is not None and (
                    len(session.tool_executions) - self._run_tool_call_start
                    > self.config.max_tool_calls
                ):
                    return self._finish_budget(
                        "max_tool_calls",
                        step,
                        messages,
                        usage,
                        session,
                    )

                # A model response can itself consume the remaining time or token
                # budget. Keep its final answer when no tools are pending, but do
                # not begin a new local side effect after that budget is exhausted.
                if session is not None and response.tool_calls:
                    budget_reason = self._budget_reason(session, before_model=False)
                    if budget_reason is not None:
                        return self._finish_budget(
                            budget_reason,
                            step,
                            messages,
                            usage,
                            session,
                        )

                if not response.tool_calls:
                    final = response.content.strip()
                    if final:
                        outcome = self._completion_outcome(session)
                        if session is not None:
                            self._set_phase(session, TaskPhase.SUMMARIZE)
                        return self._finish(
                            outcome[0],
                            final,
                            step,
                            messages,
                            usage,
                            session,
                            session_status=outcome[1],
                            stop_reason=outcome[2],
                            last_error=outcome[3],
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
                batch_decisions: dict[str, bool] = {}
                batch_candidates: list[tuple[Tool, dict[str, Any]]] = []
                batch_call_ids: list[str] = []
                for call, repeated in zip(
                    response.tool_calls,
                    repeated_flags,
                    strict=True,
                ):
                    if repeated or call.arguments is None or call.parse_error:
                        continue
                    try:
                        self.registry.validate_arguments(call.name, call.arguments)
                    except ToolError:
                        continue
                    tool = self.registry.get(call.name)
                    if tool is None or not self._requires_interactive_approval(
                        tool,
                        call.arguments,
                    ):
                        continue
                    batch_candidates.append((tool, call.arguments))
                    batch_call_ids.append(call.id)
                if len(batch_candidates) > 1 and self.batch_approval_callback is not None:
                    if session is not None:
                        session.set_status(SessionStatus.WAITING_FOR_APPROVAL)
                        self._persist(session, messages, usage)
                    approved_batch = self._ask_batch_approval(batch_candidates)
                    if session is not None:
                        session.set_status(SessionStatus.RUNNING)
                    self._check_cancelled()
                    batch_decisions.update(
                        {call_id: approved_batch for call_id in batch_call_ids}
                    )
                for call, record, repeated in zip(
                    response.tool_calls,
                    records,
                    repeated_flags,
                    strict=True,
                ):
                    self._check_cancelled()
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
                        self._process_tool_call(
                            call,
                            record,
                            messages,
                            usage,
                            session,
                            approval_decision=batch_decisions.get(call.id),
                        )
                        self._check_cancelled()
                    if session is not None:
                        budget_reason = self._budget_reason(session, before_model=False)
                        if budget_reason is not None:
                            return self._finish_budget(
                                budget_reason,
                                step,
                                messages,
                                usage,
                                session,
                            )

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
                "budget_exceeded",
                self._budget_message("max_steps"),
                self.config.max_steps,
                messages,
                usage,
                session,
                session_status=SessionStatus.INTERRUPTED,
                stop_reason="max_steps",
                last_error=self._budget_message("max_steps"),
            )
        except _RunCancelled:
            return self._finish(
                "cancelled",
                "任务已按你的要求停止。当前进度已经保存，可以稍后继续。",
                self._current_step,
                messages,
                usage,
                session,
                session_status=SessionStatus.INTERRUPTED,
                stop_reason="user_cancelled",
            )
        except KeyboardInterrupt:
            if session is not None:
                session.run_duration_seconds += self._current_run_duration()
                session.set_status(
                    SessionStatus.INTERRUPTED,
                    stop_reason="keyboard_interrupt",
                    last_error="Interrupted by user",
                )
                self._persist(session, messages, usage)
                self._emit(
                    "run_cancelled",
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
        if (
            task
            and task != session.task
            and session.status
            not in {
                SessionStatus.COMPLETED_VERIFIED,
                SessionStatus.COMPLETED_UNVERIFIED,
                SessionStatus.FAILED,
                SessionStatus.DENIED,
                SessionStatus.INTERRUPTED,
            }
        ):
            raise SessionError("an unfinished session cannot be given a different task")
        if not session.messages:
            raise SessionError("session has no conversation messages to resume")
        if session.status == SessionStatus.CANCELLED:
            raise SessionError("cancelled sessions cannot be resumed")

    def _reconcile_workspace_state(
        self,
        session: AgentSession,
        messages: list[dict[str, Any]],
        usage: dict[str, int],
    ) -> None:
        latest_by_path = {}
        for change in session.changes:
            if change.undo_status == "active":
                latest_by_path[change.path] = change
        mismatched = []
        for path, change in latest_by_path.items():
            try:
                if self.change_tracker.current_hash(path) != change.after_hash:
                    mismatched.append(path)
            except ChangeError:
                mismatched.append(path)
        if not mismatched:
            return

        rendered_paths = ", ".join(sorted(mismatched))
        invalidated = session.invalidate_verification(
            f"workspace changed outside the session: {rendered_paths}"
        )
        self._set_phase(session, TaskPhase.IMPLEMENT)
        messages.append(
            {
                "role": "system",
                "content": (
                    "Local runtime notice: files changed outside this saved Session after "
                    f"the last tracked result ({rendered_paths}). Earlier verification is "
                    "stale. Re-inspect the current files and run relevant verification "
                    "before claiming completion."
                ),
            }
        )
        if session.status in {
            SessionStatus.COMPLETED_VERIFIED,
            SessionStatus.COMPLETED_UNVERIFIED,
        }:
            session.set_status(
                SessionStatus.INTERRUPTED,
                stop_reason="external_workspace_change",
            )
        self._persist(session, messages, usage)
        for verification in invalidated:
            self._emit(
                "verification_invalidated",
                {
                    "session_id": session.session_id,
                    "verification_id": verification.verification_id,
                    "reason": verification.invalidation_reason,
                    "change_revision": session.change_revision,
                },
            )
        self._emit(
            "workspace_changed",
            {
                "session_id": session.session_id,
                "paths": sorted(mismatched),
                "change_revision": session.change_revision,
            },
        )

    def _model_summary(self) -> dict[str, Any]:
        return {
            "provider": self.config.model_provider,
            "model": self.config.model,
            "wire_api": self.config.wire_api.value,
            "reasoning_effort": self.config.model_reasoning_effort,
            "verbosity": self.config.model_verbosity,
            "approval_policy": self.config.approval_policy.value,
            "max_steps": self.config.max_steps,
            "max_seconds": self.config.max_seconds,
            "max_model_calls": self.config.max_model_calls,
            "max_tool_calls": self.config.max_tool_calls,
            "max_tool_output_chars": self.config.max_tool_output_chars,
            "max_total_tool_output_chars": self.config.max_total_tool_output_chars,
            "max_total_tokens": self.config.max_total_tokens,
            "max_context_tokens": self.config.max_context_tokens,
        }

    @staticmethod
    def _accumulate_usage(usage: dict[str, int], response: ModelResponse) -> None:
        for name, value in response.usage.items():
            usage[name] = usage.get(name, 0) + value

    def _complete_with_retry(
        self,
        prepared: list[dict[str, Any]],
        *,
        step: int,
        session: AgentSession | None,
        messages: list[dict[str, Any]],
        usage: dict[str, int],
    ) -> ModelResponse:
        retry_index = 0
        tool_definitions = self.registry.definitions()
        tool_schema_chars = len(
            json.dumps(tool_definitions, ensure_ascii=False, default=str)
        )
        while True:
            self._check_cancelled()
            if session is not None:
                budget_reason = self._budget_reason(session, before_model=True)
                if budget_reason is not None:
                    raise _RunBudgetExceeded(
                        budget_reason,
                        self._budget_message(budget_reason),
                    )
                session.model_call_count += 1
                self._persist(session, messages, usage)
            request_started = time.monotonic()
            self._emit(
                "model_request_started",
                {
                    "step": step,
                    "attempt": retry_index + 1,
                    "history_messages": len(messages),
                    "sent_messages": len(prepared),
                    "estimated_chars": self.context.estimate_chars(prepared),
                    "estimated_tokens": self.context.estimate_tokens(prepared),
                    "tool_schema_chars": tool_schema_chars,
                },
            )
            try:
                response = self.model.complete(prepared, tool_definitions)
            except ModelError as exc:
                self._check_cancelled()
                if session is not None:
                    session.usage_missing_count += 1
                    session.model_call_records.append(
                        {
                            "call": session.model_call_count,
                            "turn": session.turn_count,
                            "step": step,
                            "attempt": retry_index + 1,
                            "history_messages": len(messages),
                            "sent_messages": len(prepared),
                            "estimated_chars": self.context.estimate_chars(prepared),
                            "estimated_tokens": self.context.estimate_tokens(prepared),
                            "tool_schema_chars": tool_schema_chars,
                            "compacted": self._continuing_turn or len(prepared) < len(messages),
                            "duration_seconds": time.monotonic() - request_started,
                            "usage": {},
                            "error_category": exc.category.value,
                        }
                    )
                    self._persist(session, messages, usage)
                max_retries = self.config.max_model_retries
                if exc.category == ModelErrorCategory.RESPONSE_PARSE:
                    max_retries = min(max_retries, 1)
                if not exc.retryable or retry_index >= max_retries:
                    raise
                delay = self._retry_delay(exc, retry_index)
                if session is not None:
                    remaining = self.config.max_seconds - self._elapsed_total(session)
                    if remaining <= delay:
                        raise _RunBudgetExceeded(
                            "max_seconds",
                            "Run time budget would be exhausted before the next retry.",
                        ) from exc
                    session.retry_count += 1
                    self._persist(session, messages, usage)
                retry_index += 1
                self._emit(
                    "retry_scheduled",
                    {
                        "step": step,
                        "retry": retry_index,
                        "max_retries": max_retries,
                        "category": exc.category.value,
                        "status_code": exc.status_code,
                        "delay_seconds": delay,
                        "retry_after_seconds": exc.retry_after_seconds,
                        "request_duration_seconds": time.monotonic() - request_started,
                    },
                )
                self._wait_for_retry(delay)
                continue
            self._check_cancelled()
            self._last_model_duration = time.monotonic() - request_started
            if session is not None:
                if not isinstance(response.usage.get("total_tokens"), int):
                    session.usage_missing_count += 1
                session.model_call_records.append(
                    {
                        "call": session.model_call_count,
                        "turn": session.turn_count,
                        "step": step,
                        "attempt": retry_index + 1,
                        "history_messages": len(messages),
                        "sent_messages": len(prepared),
                        "estimated_chars": self.context.estimate_chars(prepared),
                        "estimated_tokens": self.context.estimate_tokens(prepared),
                        "tool_schema_chars": tool_schema_chars,
                        "compacted": self._continuing_turn or len(prepared) < len(messages),
                        "duration_seconds": self._last_model_duration,
                        "usage": dict(response.usage),
                    }
                )
                self._persist(session, messages, usage)
            return response

    def _retry_delay(self, error: ModelError, retry_index: int) -> float:
        exponential = self.config.retry_base_seconds * (2**retry_index)
        jitter = random.uniform(0.0, max(0.0, exponential * 0.25))
        calculated = exponential + jitter
        if error.retry_after_seconds is not None:
            calculated = max(calculated, error.retry_after_seconds)
        return min(calculated, self.config.retry_max_seconds)

    def _budget_reason(
        self,
        session: AgentSession,
        *,
        before_model: bool,
    ) -> str | None:
        if self._elapsed_total(session) >= self.config.max_seconds:
            return "max_seconds"
        if (
            before_model
            and session.model_call_count - self._run_model_call_start
            >= self.config.max_model_calls
        ):
            return "max_model_calls"
        if len(session.tool_executions) - self._run_tool_call_start > self.config.max_tool_calls:
            return "max_tool_calls"
        if (
            session.tool_output_chars - self._run_tool_output_start
            >= self.config.max_total_tool_output_chars
        ):
            return "max_total_tool_output"
        reported_total = session.total_usage.get("total_tokens")
        if (
            isinstance(reported_total, int)
            and reported_total - self._run_total_token_start >= self.config.max_total_tokens
        ):
            return "max_total_tokens"
        return None

    def _finish_budget(
        self,
        reason: str,
        steps: int,
        messages: list[dict[str, Any]],
        usage: dict[str, int],
        session: AgentSession | None,
        *,
        detail: str | None = None,
    ) -> AgentRunResult:
        message = detail or self._budget_message(reason)
        return self._finish(
            "budget_exceeded",
            message,
            steps,
            messages,
            usage,
            session,
            session_status=SessionStatus.INTERRUPTED,
            stop_reason=reason,
            last_error=message,
        )

    def _budget_message(self, reason: str) -> str:
        messages = {
            "max_steps": (
                f"Stopped after reaching the {self.config.max_steps}-step budget. "
                "Resume with a higher limit to continue."
            ),
            "max_seconds": (
                f"Stopped after reaching the {self.config.max_seconds}-second cumulative "
                "run-time budget. Resume with a higher limit to continue."
            ),
            "max_model_calls": (
                f"Stopped after reaching the {self.config.max_model_calls}-request model "
                "call budget. Resume with a higher limit to continue."
            ),
            "max_tool_calls": (
                f"Stopped because the model requested more than "
                f"{self.config.max_tool_calls} tool calls. Resume with a higher limit "
                "to continue pending calls."
            ),
            "max_total_tool_output": (
                f"Stopped after tools returned {self.config.max_total_tool_output_chars} "
                "or more cumulative characters."
            ),
            "max_total_tokens": (
                f"Stopped after provider-reported usage reached the "
                f"{self.config.max_total_tokens}-token budget."
            ),
        }
        return messages.get(reason, f"Stopped because run budget {reason!r} was reached.")

    def _elapsed_total(self, session: AgentSession) -> float:
        current = (
            max(0.0, time.monotonic() - self._run_started_at)
            if self._run_started_at > 0
            else 0.0
        )
        return current if self._continuing_turn else session.run_duration_seconds + current

    def _redact_model_response(self, response: ModelResponse) -> ModelResponse:
        secrets = (self.config.api_key,)
        calls = []
        for call in response.tool_calls:
            arguments = redact_sensitive_value(call.arguments, secrets=secrets)
            raw_arguments = redact_sensitive_text(call.raw_arguments, secrets=secrets)
            calls.append(
                ToolCall(
                    id=call.id,
                    name=call.name,
                    arguments=arguments if isinstance(arguments, dict) else None,
                    raw_arguments=raw_arguments,
                    parse_error=redact_sensitive_text(
                        call.parse_error,
                        secrets=secrets,
                    )
                    if call.parse_error
                    else None,
                )
            )
        provider_items = redact_sensitive_value(response.provider_items, secrets=secrets)
        return ModelResponse(
            content=redact_sensitive_text(response.content, secrets=secrets),
            tool_calls=calls,
            finish_reason=response.finish_reason,
            usage=dict(response.usage),
            provider_items=provider_items if isinstance(provider_items, list) else [],
        )

    def _new_tool_record(
        self,
        call: ToolCall,
        step: int,
        session: AgentSession | None,
    ) -> ToolExecutionRecord | None:
        if session is None:
            return None
        tool = self.registry.get(call.name)
        dynamic_risk = None
        if call.name == "run_command" and call.arguments is not None:
            dynamic_risk = assess_command(str(call.arguments.get("command", "")))
        record = ToolExecutionRecord.create(
            execution_id=uuid.uuid4().hex,
            tool_call_id=call.id,
            step=step,
            name=call.name,
            arguments=call.arguments,
            raw_arguments=call.raw_arguments,
            risk=(
                dynamic_risk.level.value
                if dynamic_risk is not None
                else (tool.risk.value if tool is not None else "unknown")
            ),
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
        approval_decision: bool | None = None,
    ) -> ToolResult:
        command_assessment = (
            assess_command(str(call.arguments.get("command", "")))
            if call.name == "run_command" and call.arguments is not None
            else None
        )
        self._emit(
            "tool_call_requested",
            {
                "tool": call.name,
                "arguments": call.arguments,
                "raw_arguments": call.raw_arguments,
                "command_risk": (
                    command_assessment.level.value if command_assessment is not None else None
                ),
                "expected_side_effects": (
                    command_assessment.expected_side_effects
                    if command_assessment is not None
                    else None
                ),
            },
        )
        if call.parse_error or call.arguments is None:
            message = f"Tool arguments were not valid JSON: {call.parse_error}"
            result = ToolResult(
                False,
                message,
                {
                    "error_code": "invalid_json_arguments",
                    "suggestion": "Retry once with one valid JSON object matching the schema.",
                },
            )
            if session is not None:
                session.invalid_tool_call_count += 1
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

        try:
            self.registry.validate_arguments(call.name, call.arguments)
        except ToolError as exc:
            message = str(exc)
            result = ToolResult(False, message, tool_error_data(message, tool=call.name))
            if session is not None:
                session.invalid_tool_call_count += 1
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

        prepared_change: PreparedChange | None = None
        if call.name in {"write_file", "edit_file"}:
            try:
                if record is not None and record.prepared_change is not None:
                    prepared_change = record.prepared_change
                else:
                    prepared_change = self.change_tracker.prepare(
                        call.name,
                        call.arguments,
                        record.execution_id if record is not None else uuid.uuid4().hex,
                    )
                    if record is not None:
                        record.prepared_change = prepared_change
                        if session is not None:
                            self._persist(session, messages, usage)
                self._emit_change_preview(prepared_change)
            except ChangeError as exc:
                message = str(exc)
                result = ToolResult(
                    False,
                    message,
                    tool_error_data(message, tool=call.name),
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
                return result

        tool = self.registry.get(call.name)
        if tool is None:
            message = f"Unknown tool: {call.name}"
            result = ToolResult(
                False,
                message,
                tool_error_data(message, tool=call.name),
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
            return result

        if approval_decision is None:
            approved = reuse_approval or tool.risk == RiskLevel.READ
            approval_automatic = approved
            if command_assessment is not None and command_assessment.level == CommandRisk.READ_ONLY:
                approved = True
                approval_automatic = True
            elif (
                self.config.approval_policy == ApprovalPolicy.AUTO
                and (
                    command_assessment is None
                    or command_assessment.auto_approvable
                )
            ):
                approved = True
                approval_automatic = True
        else:
            approved = approval_decision
            approval_automatic = False
        if not approved and approval_decision is None:
            if session is not None:
                session.set_status(SessionStatus.WAITING_FOR_APPROVAL)
                self._persist(session, messages, usage)
            approved = self._ask_approval(tool, call.arguments)
            approval_automatic = False
            if session is not None:
                session.set_status(SessionStatus.RUNNING)
            self._check_cancelled()

        if record is not None:
            record.approval_granted = approved

        if not approved:
            self._emit(
                "tool_call_denied",
                {
                    "tool": tool.name,
                    "command_risk": (
                        command_assessment.level.value
                        if command_assessment is not None
                        else tool.risk.value
                    ),
                },
            )
            result = ToolResult(
                False,
                f"User denied "
                f"{command_assessment.level.value if command_assessment else tool.risk.value} "
                f"operation: {tool.name}",
            )
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

        self._emit(
            "tool_call_approved",
            {
                "tool": tool.name,
                "command_risk": (
                    command_assessment.level.value
                    if command_assessment is not None
                    else tool.risk.value
                ),
                "automatic": approval_automatic,
            },
        )

        if record is not None:
            record.set_status(ToolExecutionStatus.APPROVED)
            if session is not None:
                self._persist(session, messages, usage)
            record.set_status(ToolExecutionStatus.RUNNING)
            if session is not None:
                self._persist(session, messages, usage)

        if prepared_change is not None:
            try:
                self._check_cancelled()
                change = self.change_tracker.apply(prepared_change)
                if session is not None:
                    session.changes.append(change)
                    self._set_phase(session, TaskPhase.IMPLEMENT)
                    invalidated = session.invalidate_verification(
                        f"file changed: {change.path}"
                    )
                    for verification in invalidated:
                        self._emit(
                            "verification_invalidated",
                            {
                                "session_id": session.session_id,
                                "verification_id": verification.verification_id,
                                "reason": verification.invalidation_reason,
                                "change_revision": session.change_revision,
                            },
                        )
                if record is not None:
                    record.change_id = change.change_id
                    record.prepared_change = None
                result = ToolResult(
                    True,
                    (
                        f"Tracked {prepared_change.tool_name} change for "
                        f"{prepared_change.path}"
                    ),
                    {
                        "change_id": change.change_id,
                        "path": change.path,
                        "additions": change.additions,
                        "deletions": change.deletions,
                        "diff_truncated": change.diff_truncated,
                        "before_hash": change.before_hash,
                        "after_hash": change.after_hash,
                    },
                )
                self._emit(
                    "change_applied",
                    {
                        "session_id": session.session_id if session is not None else None,
                        "change_id": change.change_id,
                        "tool_execution_id": change.tool_execution_id,
                        "path": change.path,
                        "additions": change.additions,
                        "deletions": change.deletions,
                    },
                )
            except ChangeError as exc:
                result = ToolResult(False, str(exc))
        else:
            result = self.registry.execute(call.name, call.arguments, self.tool_context)
        if result.ok and call.name == "read_file" and session is not None:
            hint = self._repeated_read_hint(call, record, result, session)
            if hint is not None:
                result.data["efficiency_hint"] = hint
                session.repeated_read_hint_count += 1
                self._emit(
                    "tool_efficiency_hint",
                    {"tool": call.name, "path": call.arguments.get("path"), "hint": hint},
                )
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

    def _check_cancelled(self) -> None:
        if self.cancellation_callback is not None and self.cancellation_callback():
            raise _RunCancelled

    def _wait_for_retry(self, delay: float) -> None:
        if self.cancellation_callback is None:
            time.sleep(delay)
            return
        deadline = time.monotonic() + max(0.0, delay)
        while True:
            self._check_cancelled()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            time.sleep(min(0.1, remaining))

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
        safe_data = redact_sensitive_value(
            result.data,
            secrets=(self.config.api_key,),
        )
        result = ToolResult(
            result.ok,
            redact_sensitive_text(result.message, secrets=(self.config.api_key,)),
            safe_data if isinstance(safe_data, dict) else {},
        )
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
            if call.name == "run_command":
                exit_code = result.data.get("exit_code")
                record.exit_code = (
                    exit_code
                    if isinstance(exit_code, int) and not isinstance(exit_code, bool)
                    else None
                )
                record.timed_out = bool(result.data.get("timed_out", False))
                record.output_truncated = bool(
                    result.data.get("output_truncated", False)
                )
                duration = result.data.get("duration_seconds")
                record.duration_seconds = (
                    float(duration)
                    if isinstance(duration, (int, float)) and not isinstance(duration, bool)
                    else None
                )
            record.set_status(status)
        if (
            session is not None
            and record is not None
            and call.name == "run_command"
            and call.arguments is not None
            and status != ToolExecutionStatus.DENIED
            and ("exit_code" in result.data or result.data.get("timed_out") is True)
            and self.verification_tracker.is_verification_command(call.arguments)
        ):
            verification = self.verification_tracker.record(
                tool_execution_id=record.execution_id,
                arguments=call.arguments,
                result_data=result.data,
                result_ok=result.ok,
                change_revision=session.change_revision,
            )
            session.verification_records.append(verification)
            session.refresh_verification_status()
            self._set_phase(session, TaskPhase.VERIFY)
            self._emit(
                "verification_completed",
                {
                    "session_id": session.session_id,
                    "verification_id": verification.verification_id,
                    "command": verification.command,
                    "exit_code": verification.exit_code,
                    "duration_seconds": verification.duration_seconds,
                    "passed": verification.passed,
                    "change_revision": verification.change_revision,
                    "verification_status": session.verification_status.value,
                },
            )
        if session is not None:
            if status == ToolExecutionStatus.FAILED:
                session.failed_tool_call_count += 1
            session.tool_output_chars += len(content)
            self._persist(session, messages, usage)
        self._emit(
            "tool_call_completed",
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
    ) -> tuple[bool, str | None]:
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
            budget_reason = self._budget_reason(session, before_model=False)
            if budget_reason is not None:
                return stop_for_repetition, budget_reason
        return stop_for_repetition, None

    @staticmethod
    def _repeated_read_hint(
        call: ToolCall,
        current_record: ToolExecutionRecord | None,
        result: ToolResult,
        session: AgentSession,
    ) -> str | None:
        if call.arguments is None:
            return None
        path = str(call.arguments.get("path", ""))
        start = int(result.data.get("start_line", call.arguments.get("start_line", 1)))
        end = int(result.data.get("end_line", start - 1))
        if end < start:
            return None
        length = end - start + 1
        for previous in reversed(session.tool_executions):
            if previous is current_record or previous.name != "read_file" or not previous.ok:
                continue
            arguments = previous.arguments or {}
            if str(arguments.get("path", "")) != path:
                continue
            try:
                previous_data = json.loads(previous.result_content or "{}")
            except json.JSONDecodeError:
                previous_data = {}
            previous_start = int(
                previous_data.get("start_line", arguments.get("start_line", 1))
            )
            previous_end = int(
                previous_data.get(
                    "end_line",
                    previous_start + min(max(int(arguments.get("max_lines", 400)), 1), 1000) - 1,
                )
            )
            overlap = max(
                0,
                min(end, previous_end) - max(start, previous_start) + 1,
            )
            if overlap >= max(1, length // 2):
                return (
                    f"This request overlaps {overlap} requested line(s) with an earlier "
                    f"read of {path}; use next_start_line or a narrower unseen range."
                )
        return None

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

    def _ask_batch_approval(
        self,
        items: list[tuple[Tool, dict[str, Any]]],
    ) -> bool:
        if self.batch_approval_callback is None:
            return False
        return bool(self.batch_approval_callback(items))

    def _requires_interactive_approval(
        self,
        tool: Tool,
        arguments: dict[str, Any],
    ) -> bool:
        if tool.risk == RiskLevel.READ:
            return False
        command_assessment = (
            assess_command(str(arguments.get("command", "")))
            if tool.name == "run_command"
            else None
        )
        if command_assessment is not None and command_assessment.level == CommandRisk.READ_ONLY:
            return False
        if self.config.approval_policy == ApprovalPolicy.AUTO and (
            command_assessment is None or command_assessment.auto_approvable
        ):
            return False
        return True

    def _persist(
        self,
        session: AgentSession,
        messages: list[dict[str, Any]],
        usage: dict[str, int],
    ) -> None:
        safe_messages = redact_sensitive_value(
            messages,
            secrets=(self.config.api_key,),
        )
        session.messages = copy.deepcopy(
            safe_messages if isinstance(safe_messages, list) else messages
        )
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
            turn_reply = final_text.strip()
            session.run_duration_seconds += self._current_run_duration()
            session.refresh_verification_status()
            final_text = self._with_local_report(
                final_text,
                session,
                session_status=session_status,
                stop_reason=stop_reason,
                messages=messages,
                usage=usage,
                last_error=last_error,
            )
            session.set_status(
                session_status,
                stop_reason=stop_reason,
                final_text=final_text,
                last_error=last_error,
            )
            session.finish_turn(turn_reply)
            self._refresh_working_memory(session, turn_reply)
            self._persist(session, messages, usage)
        if session_status in {
            SessionStatus.COMPLETED_VERIFIED,
            SessionStatus.COMPLETED_UNVERIFIED,
        }:
            final_event = "run_completed"
        elif session_status in {SessionStatus.INTERRUPTED, SessionStatus.CANCELLED}:
            final_event = "run_cancelled"
        else:
            final_event = "run_failed"
        self._emit(
            final_event,
            {
                "result_status": status,
                "session_id": session.session_id if session is not None else None,
                "session_status": session.status.value if session is not None else None,
                "steps": steps,
                "stop_reason": stop_reason,
                "active_changes": (
                    len([item for item in session.changes if item.undo_status == "active"])
                    if session is not None
                    else 0
                ),
                "phase": session.phase.value if session is not None else None,
                "verification_status": (
                    session.verification_status.value if session is not None else None
                ),
                "duration_seconds": (
                    session.run_duration_seconds if session is not None else None
                ),
                "model_calls": session.model_call_count if session is not None else None,
                "tool_calls": len(session.tool_executions) if session is not None else None,
                "retries": session.retry_count if session is not None else None,
                "total_usage": dict(usage),
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
    def _refresh_working_memory(session: AgentSession, turn_reply: str) -> None:
        active_changes = [
            item.path for item in session.changes if item.undo_status == "active"
        ]
        relevant_files: list[str] = []
        for execution in session.tool_executions:
            arguments = execution.arguments or {}
            for name in ("path", "directory"):
                value = arguments.get(name)
                if isinstance(value, str) and value and value not in relevant_files:
                    relevant_files.append(value)
        latest_verification = next(
            (
                {
                    "command": item.command,
                    "status": "passed" if item.passed else "failed",
                    "exit_code": item.exit_code,
                }
                for item in reversed(session.verification_records)
                if item.is_current
            ),
            None,
        )
        requests = [
            str(item.get("content") or "")[:800]
            for item in session.conversation
            if item.get("role") == "user"
        ][-5:]
        session.working_memory = {
            "scope": "this session only",
            "turn": session.turn_count,
            "current_goal": session.task[:1_200],
            "recent_user_requests": requests,
            "active_changed_files": list(dict.fromkeys(active_changes))[-30:],
            "relevant_files": relevant_files[-30:],
            "verification": latest_verification,
            "last_outcome": turn_reply[:1_200],
            "session_status": session.status.value,
            "unresolved_reason": session.last_error or session.stop_reason,
        }

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
        if self.event_callback is None:
            return
        safe_payload = redact_sensitive_value(
            payload,
            secrets=(self.config.api_key,),
        )
        event_payload = dict(safe_payload if isinstance(safe_payload, dict) else {})
        event_payload.setdefault("event_schema_version", 1)
        event_payload.setdefault(
            "timestamp",
            datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        )
        event_payload.setdefault("run_id", self._run_id)
        event_payload.setdefault("session_id", self._active_session_id)
        event_payload.setdefault("step", self._current_step or None)
        event_payload.setdefault(
            "run_duration_seconds",
            max(0.0, time.monotonic() - self._run_started_at)
            if self._run_started_at > 0
            else 0.0,
        )
        try:
            self.event_callback(name, event_payload)
        except Exception as exc:
            safe_error = redact_sensitive_text(str(exc), secrets=(self.config.api_key,))
            warnings.warn(
                f"Event callback failed for {name}: {safe_error}",
                RuntimeWarning,
                stacklevel=2,
            )

    def _emit_change_preview(self, prepared: PreparedChange) -> None:
        self._emit(
            "change_preview",
            {
                "path": prepared.path,
                "tool_execution_id": prepared.tool_execution_id,
                "tool": prepared.tool_name,
                "before_hash": prepared.before_hash,
                "after_hash": prepared.after_hash,
                "additions": prepared.additions,
                "deletions": prepared.deletions,
                "diff": prepared.unified_diff,
                "diff_truncated": prepared.diff_truncated,
            },
        )

    def _completion_outcome(
        self,
        session: AgentSession | None,
    ) -> tuple[str, SessionStatus, str, str | None]:
        if session is None:
            return (
                "completed",
                SessionStatus.COMPLETED_UNVERIFIED,
                "model_completed",
                None,
            )
        verification = session.refresh_verification_status()
        if verification == VerificationStatus.FAILED:
            return (
                "verification_failed",
                SessionStatus.FAILED,
                "verification_failed",
                "The latest real verification command failed.",
            )
        denied = [
            item
            for item in session.tool_executions
            if item.status == ToolExecutionStatus.DENIED
        ]
        active_changes = [item for item in session.changes if item.undo_status == "active"]
        if denied and not active_changes and verification == VerificationStatus.NOT_REQUIRED:
            return (
                "denied",
                SessionStatus.DENIED,
                "required_operation_denied",
                "The requested operation was denied by the user.",
            )
        if verification == VerificationStatus.PASSED:
            return (
                "completed",
                SessionStatus.COMPLETED_VERIFIED,
                "model_completed_verified",
                None,
            )
        return (
            "completed",
            SessionStatus.COMPLETED_UNVERIFIED,
            "model_completed_unverified",
            None,
        )

    def _set_phase(self, session: AgentSession, phase: TaskPhase) -> None:
        if session.phase == phase:
            return
        previous = session.phase
        session.set_phase(phase)
        self._emit(
            "phase_changed",
            {
                "session_id": session.session_id,
                "previous": previous.value,
                "phase": phase.value,
            },
        )

    def _with_local_report(
        self,
        final_text: str,
        session: AgentSession,
        *,
        session_status: SessionStatus,
        stop_reason: str,
        messages: list[dict[str, Any]],
        usage: dict[str, int],
        last_error: str | None,
    ) -> str:
        active = [item for item in session.changes if item.undo_status == "active"]
        by_path: dict[str, tuple[int, int, int]] = {}
        latest_by_path = {}
        for item in active:
            count, additions, deletions = by_path.get(item.path, (0, 0, 0))
            by_path[item.path] = (
                count + 1,
                additions + item.additions,
                deletions + item.deletions,
            )
            latest_by_path[item.path] = item
        change_lines = ["Local change summary:"]
        for path, (count, additions, deletions) in sorted(by_path.items()):
            latest = latest_by_path[path]
            try:
                current = self.change_tracker.current_hash(path)
                state = "current hash matches" if current == latest.after_hash else "CONFLICT: current file differs"
            except ChangeError:
                state = "CONFLICT: current path is unavailable"
            change_lines.append(
                f"- {path}: {count} change(s), +{additions}/-{deletions}; {state}"
            )
        if not active:
            change_lines.append("- None.")

        git_lines = ["Workspace/Git context:"]
        baseline_git = session.workspace_baseline.get("git", {})
        if isinstance(baseline_git, dict) and baseline_git.get("available"):
            baseline_entries = baseline_git.get("entries", [])
            original_paths = [
                str(item.get("path"))
                for item in baseline_entries
                if isinstance(item, dict) and item.get("path")
            ]
            git_lines.append(
                f"- Task started with {len(original_paths)} pre-existing Git change(s)."
            )
            if original_paths:
                git_lines.append("- Pre-existing: " + ", ".join(original_paths[:20]))
            current_git = capture_git_snapshot(Path(session.workspace))
            external_paths = compare_git_snapshots(
                baseline_git,
                current_git,
                agent_paths={item.path for item in session.changes},
            )
            if external_paths:
                git_lines.append(
                    "- Additional Git changes outside ChangeTracker were observed during "
                    "the task (user/process/command effects): "
                    + ", ".join(external_paths[:20])
                )
            else:
                git_lines.append(
                    "- No additional Git changes outside ChangeTracker were detected."
                )
            git_lines.append("- Agent attribution uses only ChangeTracker-managed writes.")
        else:
            git_lines.append("- No Git repository was detected for this workspace.")

        validation_lines = ["Validation:"]
        if session.verification_records:
            for record in session.verification_records:
                state = "passed" if record.passed else "failed"
                if not record.is_current:
                    state += f", stale ({record.invalidation_reason})"
                code = "timeout" if record.timed_out else f"exit {record.exit_code}"
                validation_lines.append(
                    f"- `{record.command}` ({record.cwd}): {state}, {code}, "
                    f"{record.duration_seconds:.2f}s"
                )
                output_summary = record.stderr_summary or record.stdout_summary
                if output_summary:
                    first_line = output_summary.replace("\r", "").split("\n", 1)[0]
                    validation_lines.append(f"  output: {first_line[:200]}")
        else:
            validation_lines.append("- No verification command was recorded.")

        unresolved = ["Unresolved items:"]
        if session.verification_status == VerificationStatus.FAILED:
            unresolved.append("- The latest verification command failed.")
        elif session.verification_status == VerificationStatus.STALE:
            unresolved.append("- Earlier verification is stale because files changed afterward.")
        elif (
            session.verification_status == VerificationStatus.UNVERIFIED
            and (session.changes or session.undo_history)
        ):
            unresolved.append("- Current file changes have not been verified.")
        denied = [
            item
            for item in session.tool_executions
            if item.status == ToolExecutionStatus.DENIED
        ]
        for item in denied:
            unresolved.append(f"- User denied {item.name} at step {item.step}.")
        if last_error and not any(last_error in item for item in unresolved):
            unresolved.append(f"- {last_error}")
        if session_status in {
            SessionStatus.FAILED,
            SessionStatus.INTERRUPTED,
            SessionStatus.CANCELLED,
            SessionStatus.DENIED,
        } and len(unresolved) == 1:
            unresolved.append(f"- Run did not complete normally: {stop_reason}.")
        if len(unresolved) == 1:
            unresolved.append("- None known.")

        if session.model_call_count == 0 or not usage:
            usage_state = "unknown (provider did not report usage)"
        elif session.usage_missing_count:
            usage_state = (
                f"partial ({session.usage_missing_count} of "
                f"{session.model_call_count} request(s) lacked total usage)"
            )
        else:
            usage_state = "complete"
        usage_text = ", ".join(f"{name}={value}" for name, value in sorted(usage.items()))
        statistics = [
            "Run statistics:",
            f"- model calls: {session.model_call_count}",
            f"- tool calls: {len(session.tool_executions)}",
            f"- retries: {session.retry_count}",
            f"- tool output: {session.tool_output_chars} characters",
            f"- failed tool calls: {session.failed_tool_call_count}",
            f"- invalid tool calls: {session.invalid_tool_call_count}",
            f"- repeated-read hints: {session.repeated_read_hint_count}",
            f"- total duration: {session.run_duration_seconds:.2f}s",
            f"- usage status: {usage_state}",
            f"- usage: {usage_text or 'unknown'}",
        ]
        outcome = [
            "Outcome:",
            f"- session status: {session_status.value}",
            f"- stop reason: {stop_reason}",
            f"- verification status: {session.verification_status.value}",
        ]
        report = "\n".join(
            [
                *outcome,
                "",
                *change_lines,
                "",
                *git_lines,
                "",
                *validation_lines,
                "",
                *unresolved,
                "",
                *statistics,
            ]
        )
        return final_text.rstrip() + "\n\n" + report

    def _current_run_duration(self) -> float:
        if self._run_started_at <= 0:
            return 0.0
        duration = max(0.0, time.monotonic() - self._run_started_at)
        self._run_started_at = 0.0
        return duration
