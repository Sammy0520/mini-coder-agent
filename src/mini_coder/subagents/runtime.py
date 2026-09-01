from __future__ import annotations

import copy
from dataclasses import replace
from pathlib import Path
from typing import Any

from ..agent import AgentRunner
from ..config import AgentConfig, ApprovalPolicy
from ..model import OpenAICompatibleClient
from ..session import AgentSession
from ..skills import SkillRegistry
from ..tools import create_default_registry
from ..tools.registry import ToolRegistry
from .coordinator import ParallelSubagentCoordinator
from .models import SubagentRole, SubagentSpec, WorkerOutcome


SCOUT_PROMPT = """You are Mini Coder's bounded read-only Scout Subagent.
Investigate only the assigned subtask. Use the small read/search tool set, avoid broad
repository scans, and return a compact evidence report. Include candidate files and
symbols or line ranges, the strongest evidence, a recommended next action, confidence,
and remaining unknowns. You cannot write files, run commands, delegate, or complete the
parent task. Stop as soon as the location or blocker is clear."""


IMPLEMENTER_PROMPT = """You are Mini Coder's bounded Implementer Subagent working in an
isolated copy of the user's workspace. Complete only the assigned subtask and only within
the authorized path patterns. Inspect before editing, make a small coherent change, and
run the smallest relevant safe verification. Do not delegate, access external services,
install dependencies, change Git state, or broaden scope. Your writes do not reach the
real workspace until the parent Agent and user accept the resulting patch. Finish with a
concise summary of changes, verification, and any limitation."""


class EphemeralSessionStore:
    """SessionStore-shaped in-memory sink for child runs hidden from the sidebar."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.latest: AgentSession | None = None

    def path_for(self, session_id: str) -> Path:
        return self.root / f"{session_id}.json"

    def save(self, session: AgentSession) -> Path:
        self.latest = copy.deepcopy(session)
        return self.path_for(session.session_id)


def create_scout_registry() -> ToolRegistry:
    from ..tools import ListFilesTool, ReadFileTool, SearchTextTool

    registry = ToolRegistry()
    registry.register(ListFilesTool())
    registry.register(ReadFileTool())
    registry.register(SearchTextTool())
    return registry


def build_default_coordinator(
    *,
    config: AgentConfig,
    skill_registry: SkillRegistry,
    event_callback,
    cancellation_callback,
    response_language: str | None,
) -> ParallelSubagentCoordinator:
    def worker(spec: SubagentSpec, workspace: Path, child_event) -> WorkerOutcome:
        prompt_key = f"{config.prompt_cache_key}-subagent-{spec.role.value}-v1"
        child_config = replace(
            config,
            workspace=workspace,
            approval_policy=ApprovalPolicy.AUTO,
            max_steps=config.max_subagent_steps,
            max_seconds=config.max_subagent_seconds,
            max_model_calls=config.max_subagent_model_calls,
            max_tool_calls=config.max_subagent_tool_calls,
            max_total_tokens=config.max_subagent_total_tokens,
            max_context_tokens=config.max_subagent_context_tokens,
            max_context_chars=min(
                config.max_context_chars,
                config.max_subagent_context_tokens * 4,
            ),
            prompt_cache_key=prompt_key,
            auto_approve_unknown_commands=False,
            subagents_enabled=False,
        )
        model = OpenAICompatibleClient(
            api_key=child_config.api_key or "not-required",
            base_url=child_config.base_url,
            model=child_config.model or "",
            wire_api=child_config.wire_api,
            reasoning_effort=child_config.model_reasoning_effort,
            verbosity="low",
            timeout_seconds=child_config.model_timeout_seconds,
            streaming=child_config.model_streaming,
            prompt_cache_enabled=child_config.prompt_cache_enabled,
            prompt_cache_key=child_config.prompt_cache_key,
        )
        store = EphemeralSessionStore(
            workspace.parent / ".mini-coder-subagent-session"
        )
        registry = (
            create_scout_registry()
            if spec.role == SubagentRole.SCOUT
            else create_default_registry()
        )
        runner = AgentRunner(
            model=model,
            registry=registry,
            config=child_config,
            system_prompt=(
                SCOUT_PROMPT
                if spec.role == SubagentRole.SCOUT
                else IMPLEMENTER_PROMPT
            ),
            skill_registry=skill_registry,
            event_callback=child_event,
            cancellation_callback=cancellation_callback,
            session_store=store,  # type: ignore[arg-type]
            session_title=spec.label or spec.agent_id,
            response_language=response_language,
        )
        result = runner.run(spec.render_task())
        session = store.latest
        return WorkerOutcome(
            status=result.status,
            final_text=result.final_text,
            usage=dict(result.total_usage),
            model_calls=session.model_call_count if session is not None else 0,
            tool_calls=len(session.tool_executions) if session is not None else 0,
            verification=(
                [item.to_dict() for item in session.verification_records]
                if session is not None
                else []
            ),
        )

    return ParallelSubagentCoordinator(
        workspace=config.workspace,
        worker=worker,
        event_callback=event_callback,
        cancellation_callback=cancellation_callback,
        max_parallel=config.max_parallel_subagents,
        max_batches=config.max_subagent_batches,
        max_workspace_files=config.max_subagent_workspace_files,
        max_workspace_bytes=config.max_subagent_workspace_bytes,
    )
