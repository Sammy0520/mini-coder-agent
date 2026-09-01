from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Any


class TaskIntent(str, Enum):
    BUILD = "build"
    FIX = "fix"
    FEATURE = "feature"
    IMPROVE = "improve"
    EXPLAIN = "explain"


@dataclass(frozen=True, slots=True)
class TaskBrief:
    intent: TaskIntent
    goal: str
    workspace_kind: str
    assumptions: tuple[str, ...]
    acceptance_checks: tuple[str, ...]
    clarification_needed: bool = False
    clarification_question: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "intent": self.intent.value,
            "goal": self.goal,
            "workspace_kind": self.workspace_kind,
            "assumptions": list(self.assumptions),
            "acceptance_checks": list(self.acceptance_checks),
            "clarification_needed": self.clarification_needed,
            "clarification_question": self.clarification_question,
        }

    @classmethod
    def from_dict(cls, data: Any) -> "TaskBrief | None":
        if not isinstance(data, dict):
            return None
        try:
            intent = TaskIntent(str(data.get("intent")))
        except ValueError:
            return None
        goal = data.get("goal")
        workspace_kind = data.get("workspace_kind")
        assumptions = data.get("assumptions", [])
        acceptance = data.get("acceptance_checks", [])
        if (
            not isinstance(goal, str)
            or not isinstance(workspace_kind, str)
            or not isinstance(assumptions, list)
            or not all(isinstance(item, str) for item in assumptions)
            or not isinstance(acceptance, list)
            or not all(isinstance(item, str) for item in acceptance)
        ):
            return None
        question = data.get("clarification_question")
        return cls(
            intent=intent,
            goal=goal,
            workspace_kind=workspace_kind,
            assumptions=tuple(assumptions),
            acceptance_checks=tuple(acceptance),
            clarification_needed=bool(data.get("clarification_needed", False)),
            clarification_question=question if isinstance(question, str) else None,
        )


def create_turn_state(brief: TaskBrief) -> dict[str, Any]:
    """Create the single compact durable state for one conversation turn.

    The runtime, rather than the model, owns this structure. It is intentionally
    JSON-shaped so schema-v8 sessions can persist it inside working_memory.
    """
    return {
        "version": 1,
        "intent": brief.intent.value,
        "goal": brief.goal[:1_200],
        "workspace_kind": brief.workspace_kind,
        "phase": "frame",
        "requirements": list(brief.acceptance_checks),
        "assumptions": list(brief.assumptions),
        "clarification_needed": brief.clarification_needed,
        "clarification_question": brief.clarification_question,
        "decisions": [],
        "relevant_files": [],
        "changed_files": [],
        "verification": [],
        "unresolved": [],
        "completion_reserve": False,
        "last_outcome": "",
        "session_status": "running",
    }


def turn_state_from_memory(memory: Any) -> dict[str, Any] | None:
    """Read new state or normalize schema-v8 state created before TurnState."""
    if not isinstance(memory, dict):
        return None
    current = memory.get("turn_state")
    if isinstance(current, dict):
        return current
    legacy = memory.get("task_ledger")
    if isinstance(legacy, dict):
        return legacy
    brief = TaskBrief.from_dict(memory.get("task_brief"))
    return create_turn_state(brief) if brief is not None else None


def render_turn_state(state: Any) -> str:
    if not isinstance(state, dict):
        return ""

    def values(name: str, limit: int = 8) -> list[str]:
        raw = state.get(name, [])
        if not isinstance(raw, list):
            return []
        return [str(item)[:300] for item in raw[-limit:] if str(item).strip()]

    lines = [
        "Previous turn state (compact evidence, not a new user request):",
        f"- Goal: {str(state.get('goal') or '')[:1_200]}",
        f"- Intent: {str(state.get('intent') or 'feature')}",
        f"- Final phase: {str(state.get('phase') or 'finish')}",
    ]
    for label, key in (
        ("Requirements", "requirements"),
        ("Safe assumptions", "assumptions"),
        ("Relevant files", "relevant_files"),
        ("Changed files", "changed_files"),
        ("Verification evidence", "verification"),
        ("Unresolved items", "unresolved"),
    ):
        items = values(key)
        lines.append(f"- {label}: " + (" | ".join(items) if items else "none yet"))
    last_outcome = str(state.get("last_outcome") or "").strip()
    if last_outcome:
        lines.append(f"- Last outcome: {last_outcome[:800]}")
    active_skill = state.get("active_skill")
    if isinstance(active_skill, dict) and active_skill.get("name"):
        lines.append(
            f"- Previously selected skill: {str(active_skill.get('name'))[:120]} "
            f"({str(active_skill.get('id') or '')[:80]})"
        )
    return "\n".join(lines)[:4_000]


# Compatibility for callers and saved sessions created by the first staged-workflow
# implementation. New code stores only working_memory.turn_state.
create_task_ledger = create_turn_state
render_task_ledger = render_turn_state


_FIX_MARKERS = (
    "报错",
    "错误",
    "修复",
    "坏了",
    "跑不了",
    "不能运行",
    "失败",
    "bug",
    "fix",
    "broken",
    "crash",
    "failing",
    "error",
)
_BUILD_MARKERS = (
    "从零",
    "构建",
    "创建",
    "做一个",
    "搭一个",
    "新建",
    "build",
    "create",
    "scaffold",
    "from scratch",
)
_FEATURE_MARKERS = (
    "增加",
    "新增",
    "添加",
    "支持",
    "实现",
    "功能",
    "add ",
    "implement",
    "feature",
)
_IMPROVE_MARKERS = (
    "优化",
    "美化",
    "整理",
    "重构",
    "改进",
    "improve",
    "refactor",
    "polish",
    "clean up",
)
_EXPLAIN_MARKERS = (
    "解释",
    "分析",
    "看看原因",
    "为什么",
    "review",
    "explain",
    "analyze",
)
_TOO_VAGUE = {
    "帮我做一个",
    "帮我做个",
    "做一个项目",
    "做个项目",
    "帮我修改一下",
    "帮我优化一下",
    "build something",
    "make something",
}


def frame_task(task: str, workspace_overview: dict[str, Any]) -> TaskBrief:
    goal = " ".join(task.strip().split())
    lowered = goal.casefold()
    root_entries = workspace_overview.get("root_entries", [])
    workspace_kind = "empty" if not root_entries else "existing"
    intent = _detect_intent(lowered, workspace_kind)
    assumptions = _assumptions(intent, workspace_kind)
    acceptance = _acceptance_checks(goal, intent, workspace_kind)
    clarification_needed = lowered in _TOO_VAGUE or len(_meaningful_goal(goal)) < 2
    question = (
        "你希望最终得到什么类型的程序或改动？用一句话说明主要用途就可以。"
        if clarification_needed
        else None
    )
    return TaskBrief(
        intent=intent,
        goal=goal,
        workspace_kind=workspace_kind,
        assumptions=tuple(assumptions),
        acceptance_checks=tuple(acceptance),
        clarification_needed=clarification_needed,
        clarification_question=question,
    )


def render_task_brief(brief: TaskBrief) -> str:
    assumptions = "\n".join(f"- {item}" for item in brief.assumptions)
    checks = "\n".join(f"- {item}" for item in brief.acceptance_checks)
    clarification = (
        "\nA material choice is still missing. Ask only this one concise question and "
        f"do not call tools yet: {brief.clarification_question}"
        if brief.clarification_needed
        else (
            "\nProceed without asking the user to write a specification. Safe, reversible "
            "defaults are already stated below; ask one concise question only if later "
            "discovery reveals a choice that materially changes architecture, cost, external "
            "services, or data safety."
        )
    )
    return (
        "Runtime task brief (derived from the user's natural-language goal and bounded "
        "workspace discovery; it narrows execution but does not replace the user request):\n"
        f"- Intent: {brief.intent.value}\n"
        f"- Goal: {brief.goal}\n"
        f"- Workspace: {brief.workspace_kind}\n"
        "Default assumptions:\n"
        f"{assumptions}\n"
        "Minimum acceptance checks:\n"
        f"{checks}"
        f"{clarification}"
    )


def _detect_intent(lowered: str, workspace_kind: str) -> TaskIntent:
    if any(marker in lowered for marker in _FIX_MARKERS):
        return TaskIntent.FIX
    if any(marker in lowered for marker in _EXPLAIN_MARKERS):
        return TaskIntent.EXPLAIN
    if any(marker in lowered for marker in _IMPROVE_MARKERS):
        return TaskIntent.IMPROVE
    if any(marker in lowered for marker in _FEATURE_MARKERS) and workspace_kind == "existing":
        return TaskIntent.FEATURE
    if workspace_kind == "empty" or any(marker in lowered for marker in _BUILD_MARKERS):
        return TaskIntent.BUILD
    return TaskIntent.FEATURE


def _assumptions(intent: TaskIntent, workspace_kind: str) -> list[str]:
    if intent == TaskIntent.BUILD:
        assumptions = [
            "先交付一个最小但完整可用的本地版本，不自行扩展为大型系统",
            "优先选择依赖少、容易运行和演示的实现",
            "除非用户明确要求，不加入账号、云服务、支付或部署",
        ]
        if workspace_kind == "existing":
            assumptions.insert(0, "沿用当前项目的语言、框架、目录结构和代码风格")
        return assumptions
    if intent == TaskIntent.FIX:
        return [
            "先使用项目已有入口或检查方式复现问题，再修改代码",
            "修复根因并保持改动范围尽可能小",
            "修改后重复原来的复现步骤；环境问题不会伪装成代码通过",
        ]
    if intent == TaskIntent.EXPLAIN:
        return [
            "默认只读分析，不因为发现改进点而擅自修改文件",
            "结论以实际项目文件和可复现现象为依据",
        ]
    if intent == TaskIntent.IMPROVE:
        return [
            "沿用现有技术和交互，不做无关重写",
            "先识别最影响用户目标的问题，再做范围明确的改进",
            "保持已有行为并运行项目现有检查",
        ]
    return [
        "沿用当前项目的技术、结构和公共行为",
        "只实现用户目标所需的最小完整改动",
        "修改相关检查或文档，并验证原有功能没有明显回归",
    ]


def _acceptance_checks(
    goal: str,
    intent: TaskIntent,
    workspace_kind: str,
) -> list[str]:
    explicit = _extract_requirements(goal)
    checks = list(explicit[:4])
    if intent == TaskIntent.BUILD:
        checks.extend(
            [
                "核心使用流程可以完成，而不只是生成空架子",
                "项目有清晰入口并能按本地说明运行",
                "常见空输入或错误输入不会直接让程序崩溃",
            ]
        )
    elif intent == TaskIntent.FIX:
        checks.extend(
            [
                "问题在修改前被复现，或有明确且有证据的根因诊断",
                "修改针对根因，不通过隐藏异常或删除检查来绕过问题",
                "修改后相同复现步骤或相关项目检查通过",
                "没有引入与修复无关的大范围改动",
            ]
        )
    elif intent == TaskIntent.EXPLAIN:
        checks.extend(
            [
                "指出相关文件、行为和根因依据",
                "明确区分已确认事实与仍需验证的推测",
            ]
        )
    else:
        checks.extend(
            [
                "用户要求的行为可以从项目入口实际使用",
                "未涉及的原有行为保持可用",
                "相关项目检查通过，或清楚说明无法验证的环境原因",
            ]
        )
    if workspace_kind == "empty" and intent != TaskIntent.EXPLAIN:
        checks.append("新建文件集中在当前工作区，并包含简短使用说明")
    return list(dict.fromkeys(checks))[:8]


def _extract_requirements(task: str) -> list[str]:
    parts = re.split(r"(?:\r?\n+|[；;。]+|(?<!\d)[1-9][.、)])", task)
    result: list[str] = []
    for part in parts:
        item = " ".join(part.strip(" -\t:：").split())
        if len(item) >= 3 and item not in result:
            result.append(item[:240])
    return result or [task[:240]]


def _meaningful_goal(task: str) -> str:
    cleaned = re.sub(
        r"(?i)(帮我|请|一下|一个|做个|做一个|构建|创建|修改|优化|please|help me|build|make|create)",
        "",
        task,
    )
    return re.sub(r"\s+", "", cleaned)
