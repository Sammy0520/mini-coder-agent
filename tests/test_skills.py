from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from mini_coder.skills import SkillRegistry, render_selected_skill
from mini_coder.tasking import TaskIntent, frame_task


class SkillRegistryTests(unittest.TestCase):
    def test_builtin_selection_uses_intent_without_loading_every_skill(self) -> None:
        registry = SkillRegistry.builtins_only()

        fix = registry.select("程序启动时报错，帮我修好", TaskIntent.FIX)
        build = registry.select("帮我做一个本地记账工具", TaskIntent.BUILD)
        docs = registry.select("把 README 整理得更容易上手", TaskIntent.IMPROVE)

        self.assertEqual(fix.skill_id if fix else None, "bug-fix")
        self.assertEqual(build.skill_id if build else None, "small-app")
        self.assertEqual(docs.skill_id if docs else None, "project-docs")
        prompt = render_selected_skill(fix)
        self.assertIn("bug-fix", prompt)
        self.assertNotIn("small-app", prompt)
        self.assertNotIn("project-docs", prompt)

    def test_build_wording_wins_over_incidental_improve_word(self) -> None:
        brief = frame_task(
            "帮我从零做一个本地书签整理工具，尽量简单好用",
            {"root_entries": ["README.txt"]},
        )
        registry = SkillRegistry.builtins_only()
        selected = registry.select(brief.goal, brief.intent)

        self.assertEqual(brief.intent, TaskIntent.BUILD)
        self.assertEqual(selected.skill_id if selected else None, "small-app")
        self.assertIn("node --check", render_selected_skill(selected))

    def test_custom_skill_round_trip_auto_selection_and_delete(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            registry = SkillRegistry.with_user_skills(Path(directory))
            skill = registry.add_custom(
                name="数据库迁移助手",
                description="处理数据库迁移、字段升级和兼容检查",
                instructions="先查看现有 schema 和迁移记录，再生成可回滚的小步迁移。",
            )

            restored = SkillRegistry.with_user_skills(Path(directory))
            selected = restored.select(
                "帮我处理数据库字段迁移和兼容检查",
                TaskIntent.FEATURE,
            )

            self.assertEqual(selected.skill_id if selected else None, skill.skill_id)
            self.assertTrue(restored.delete_custom(skill.skill_id))
            self.assertIsNone(restored.get(skill.skill_id))

    def test_builtin_skills_cannot_be_deleted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            registry = SkillRegistry.with_user_skills(Path(directory))
            self.assertFalse(registry.delete_custom("bug-fix"))

    def test_feature_intent_uses_fourth_builtin(self) -> None:
        registry = SkillRegistry.builtins_only()
        selected = registry.select("给现有页面增加搜索功能", TaskIntent.FEATURE)

        self.assertEqual(len([skill for skill in registry.list() if skill.builtin]), 4)
        self.assertEqual(selected.skill_id if selected else None, "feature-work")

    def test_markdown_import_reads_frontmatter_and_persists_origin(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            registry = SkillRegistry.with_user_skills(Path(directory))
            imported = registry.import_markdown(
                """---
name: 发布助手
description: 发布前核对版本和测试结果
---
# 发布流程

先运行已有测试，再核对版本号与变更记录。
""",
                source_name="SKILL.md",
            )
            restored = SkillRegistry.with_user_skills(Path(directory)).get(
                imported.skill_id
            )

            self.assertEqual(imported.name, "发布助手")
            self.assertEqual(restored.origin if restored else None, "markdown")
            self.assertIn("运行已有测试", restored.instructions if restored else "")


if __name__ == "__main__":
    unittest.main()
