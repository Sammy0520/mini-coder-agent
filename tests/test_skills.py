from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from mini_coder.skills import SkillRegistry, render_selected_skill
from mini_coder.tasking import TaskIntent


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


if __name__ == "__main__":
    unittest.main()
