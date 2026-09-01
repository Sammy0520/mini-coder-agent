from __future__ import annotations

import json
import os
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..tasking import TaskIntent


@dataclass(frozen=True, slots=True)
class Skill:
    skill_id: str
    name: str
    description: str
    instructions: str
    builtin: bool = False

    def to_dict(self, *, include_instructions: bool = True) -> dict[str, Any]:
        data: dict[str, Any] = {
            "id": self.skill_id,
            "name": self.name,
            "description": self.description,
            "builtin": self.builtin,
        }
        if include_instructions:
            data["instructions"] = self.instructions
        return data


_BUILTIN_SKILLS = (
    Skill(
        skill_id="bug-fix",
        name="修复运行问题",
        description="复现报错、定位根因、做最小修复并重复验证。",
        instructions=(
            "先用项目已有入口、失败命令或最小复现确认现象。区分代码问题、配置问题和环境问题；"
            "找到最靠近根因的修改点，只做必要改动。修复后重复原复现步骤，并运行相关回归检查。"
            "无法复现时先收集证据，不凭猜测大改代码。"
        ),
        builtin=True,
    ),
    Skill(
        skill_id="small-app",
        name="从零构建小项目",
        description="把一句模糊想法收敛成依赖少、可运行、可演示的小应用。",
        instructions=(
            "先交付一个最小但完整的本地版本：核心流程必须真的可用，不只创建空架子。"
            "优先使用当前环境已有能力和少量文件；除非用户明确要求，不引入账号、云服务、支付或复杂部署。"
            "补充简短使用说明，并实际运行入口或最接近真实使用的检查。"
        ),
        builtin=True,
    ),
    Skill(
        skill_id="project-docs",
        name="整理项目文档",
        description="依据真实项目结构补充清晰、可执行的 README 与使用说明。",
        instructions=(
            "先阅读项目入口、配置、依赖和已有命令，再编写文档。说明应覆盖项目用途、准备条件、"
            "启动方式、常见使用流程和可验证的检查命令。不要发明不存在的功能或命令；"
            "保留项目已有的重要说明，并让第一次接触项目的人可以照着完成一次运行。"
        ),
        builtin=True,
    ),
)

_DOC_MARKERS = (
    "readme",
    "documentation",
    "docs",
    "文档",
    "项目说明",
    "使用说明",
    "使用指南",
    "快速开始",
)


def default_user_skill_directory() -> Path:
    configured = os.environ.get("MINI_CODER_SKILLS_DIR", "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    try:
        user_root = Path.home()
    except RuntimeError:
        user_root = Path.cwd()
    return (user_root / ".mini-coder" / "skills").resolve()


class SkillRegistry:
    """Small, deterministic skill catalog with lazy prompt injection."""

    def __init__(self, user_directory: str | Path | None = None) -> None:
        self.user_directory = (
            Path(user_directory).expanduser().resolve()
            if user_directory is not None
            else None
        )

    @classmethod
    def builtins_only(cls) -> "SkillRegistry":
        return cls(None)

    @classmethod
    def with_user_skills(
        cls,
        user_directory: str | Path | None = None,
    ) -> "SkillRegistry":
        return cls(user_directory or default_user_skill_directory())

    def list(self) -> list[Skill]:
        return [*_BUILTIN_SKILLS, *self._load_custom()]

    def get(self, skill_id: str) -> Skill | None:
        return next((skill for skill in self.list() if skill.skill_id == skill_id), None)

    def add_custom(self, *, name: str, description: str, instructions: str) -> Skill:
        if self.user_directory is None:
            raise ValueError("用户 Skill 存储未启用")
        clean_name = " ".join(name.strip().split())
        clean_description = " ".join(description.strip().split())
        clean_instructions = instructions.strip()
        if not 2 <= len(clean_name) <= 80:
            raise ValueError("Skill 名称应为 2 到 80 个字符")
        if not 4 <= len(clean_description) <= 240:
            raise ValueError("Skill 简介应为 4 到 240 个字符")
        if not 10 <= len(clean_instructions) <= 8_000:
            raise ValueError("Skill 内容应为 10 到 8000 个字符")
        if any(skill.name.casefold() == clean_name.casefold() for skill in self.list()):
            raise ValueError("已经有同名 Skill")
        skill = Skill(
            skill_id=f"custom-{uuid.uuid4().hex[:12]}",
            name=clean_name,
            description=clean_description,
            instructions=clean_instructions,
            builtin=False,
        )
        self.user_directory.mkdir(parents=True, exist_ok=True)
        target = self._path_for(skill.skill_id)
        temporary = target.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(skill.to_dict(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, target)
        return skill

    def delete_custom(self, skill_id: str) -> bool:
        if self.user_directory is None or not skill_id.startswith("custom-"):
            return False
        target = self._path_for(skill_id)
        try:
            target.unlink()
        except FileNotFoundError:
            return False
        except OSError as exc:
            raise ValueError("无法删除这个 Skill") from exc
        return True

    def select(self, task: str, intent: TaskIntent) -> Skill | None:
        lowered = " ".join(task.casefold().split())
        skills = self.list()
        for skill in skills:
            if skill.skill_id in lowered or skill.name.casefold() in lowered:
                return skill
        if any(marker in lowered for marker in _DOC_MARKERS):
            return self.get("project-docs")
        custom = self._select_custom(lowered, skills)
        if custom is not None:
            return custom
        if intent == TaskIntent.FIX:
            return self.get("bug-fix")
        if intent == TaskIntent.BUILD:
            return self.get("small-app")
        return None

    def _select_custom(self, task: str, skills: list[Skill]) -> Skill | None:
        task_terms = _terms(task)
        best: tuple[int, Skill] | None = None
        for skill in skills:
            if skill.builtin:
                continue
            metadata_terms = _terms(f"{skill.name} {skill.description}")
            score = len(task_terms & metadata_terms)
            if score >= 2 and (best is None or score > best[0]):
                best = (score, skill)
        return best[1] if best is not None else None

    def _load_custom(self) -> list[Skill]:
        if self.user_directory is None or not self.user_directory.is_dir():
            return []
        skills: list[Skill] = []
        for path in sorted(self.user_directory.glob("custom-*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8-sig"))
                skill_id = str(data.get("id") or "")
                name = str(data.get("name") or "").strip()
                description = str(data.get("description") or "").strip()
                instructions = str(data.get("instructions") or "").strip()
            except (OSError, json.JSONDecodeError, AttributeError):
                continue
            if (
                skill_id.startswith("custom-")
                and path.stem == skill_id
                and name
                and description
                and instructions
            ):
                skills.append(
                    Skill(skill_id, name, description, instructions, builtin=False)
                )
        return skills

    def _path_for(self, skill_id: str) -> Path:
        if self.user_directory is None or not re.fullmatch(r"custom-[a-f0-9]{12}", skill_id):
            raise ValueError("无效的 Skill 标识")
        return self.user_directory / f"{skill_id}.json"


def render_selected_skill(skill: Skill | None) -> str:
    if skill is None:
        return ""
    return (
        "Selected workflow skill (loaded for this turn only; the user request and safety "
        "rules still take priority):\n"
        f"- Skill: {skill.name} ({skill.skill_id})\n"
        f"- Purpose: {skill.description}\n"
        "Instructions:\n"
        f"{skill.instructions}"
    )[:9_000]


def _terms(value: str) -> set[str]:
    lowered = value.casefold()
    terms = set(re.findall(r"[a-z0-9][a-z0-9_-]{2,}", lowered))
    for chunk in re.findall(r"[\u4e00-\u9fff]{2,}", lowered):
        terms.update(chunk[index : index + 2] for index in range(len(chunk) - 1))
    return terms
