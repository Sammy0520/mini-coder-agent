from __future__ import annotations

import unittest

from mini_coder.tasking import (
    TaskIntent,
    create_turn_state,
    detect_parallel_opportunity,
    frame_task,
    render_parallel_opportunity,
    render_task_brief,
)


class TaskFramingTests(unittest.TestCase):
    def test_task_brief_rendering_remains_available(self) -> None:
        brief = frame_task("做一个简单网页。", {"root_entries": []})

        rendered = render_task_brief(brief)

        self.assertIn("Runtime task brief", rendered)
        self.assertIn("做一个简单网页", rendered)

    def test_colloquial_regression_request_is_classified_as_fix(self) -> None:
        brief = frame_task(
            "我刚试了一下，编辑一个任务以后刷新页面，标题又变回去了，帮我修好。",
            {"root_entries": ["index.html", "app.js"]},
        )

        self.assertEqual(brief.intent, TaskIntent.FIX)

    def test_plan_first_full_stack_request_does_not_route_before_confirmation(self) -> None:
        brief = frame_task(
            "做一个网页和本地服务分开的学习计划器，先展示你的想法，我确认后再开始。",
            {"root_entries": []},
        )

        self.assertIsNone(detect_parallel_opportunity(brief))

    def test_confirmation_reuses_prior_full_stack_boundary_for_parallel_route(self) -> None:
        planned = frame_task(
            "做一个网页和本地服务分开的学习计划器，先展示你的想法，我确认后再开始。",
            {"root_entries": []},
        )
        confirmed = frame_task(
            "可以，就按刚刚的方案开始实现，其他功能先不要。",
            {"root_entries": []},
        )

        opportunity = detect_parallel_opportunity(
            confirmed,
            create_turn_state(planned),
        )

        self.assertIsNotNone(opportunity)
        assert opportunity is not None
        self.assertEqual(
            [item["allowed_paths"] for item in opportunity.slices],
            [["frontend/**"], ["backend/**", "data/**"]],
        )
        rendered = render_parallel_opportunity(opportunity)
        self.assertIn("delegate_subagents", rendered)
        self.assertIn("two concurrent Implementers", rendered)

    def test_fix_never_receives_parallel_build_route(self) -> None:
        brief = frame_task(
            "前端调用后端 API 报错了，帮我修好。",
            {"root_entries": ["frontend", "backend"]},
        )

        self.assertEqual(brief.intent, TaskIntent.FIX)
        self.assertIsNone(detect_parallel_opportunity(brief))


if __name__ == "__main__":
    unittest.main()
