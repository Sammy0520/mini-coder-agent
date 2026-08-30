from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from mini_coder.benchmarks.runner import BenchmarkRunner


ROOT = Path(__file__).resolve().parents[1]


class BenchmarkRunnerTests(unittest.TestCase):
    def make_runner(self, output: Path, **kwargs) -> BenchmarkRunner:
        return BenchmarkRunner(
            manifest=ROOT / "benchmarks" / "manifest.json",
            output_directory=output,
            mini_config=ROOT / "agent.toml.example",
            agents=("mini",),
            **kwargs,
        )

    def test_manifest_defines_same_provider_model_and_task_categories(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            comparison, tasks = self.make_runner(Path(directory)).load()

        self.assertEqual(comparison.model_provider, "aicode007")
        self.assertEqual(comparison.base_url, "https://api.aicode007.com")
        self.assertEqual(comparison.wire_api, "responses")
        self.assertEqual(comparison.model, "gpt-5.6-sol")
        self.assertEqual(comparison.reasoning_effort, "xhigh")
        self.assertEqual(len(tasks), 5)
        self.assertEqual(
            {item.category for item in tasks},
            {"bug-fix", "feature", "zero-to-one", "refactor", "multi-turn"},
        )

    def test_cache_metrics_accept_openai_and_separate_dashboard_styles(self) -> None:
        included = BenchmarkRunner._cache_metrics(
            {"input_tokens": 10_000, "cached_tokens": 8_000}
        )
        separate = BenchmarkRunner._cache_metrics(
            {"input_tokens": 2_000, "cached_tokens": 8_000}
        )

        self.assertEqual(included["accounting"], "cached_included_in_input")
        self.assertEqual(included["cache_reuse_ratio"], 0.8)
        self.assertEqual(separate["accounting"], "cached_reported_separately")
        self.assertEqual(separate["cache_reuse_ratio"], 0.8)

    def test_codex_metrics_parse_jsonl_usage(self) -> None:
        usage, turns, calls, tools, models, response_ids = BenchmarkRunner._metrics(
            "codex",
            [
                {"type": "thread.started", "thread_id": "thread-1"},
                {
                    "type": "item.started",
                    "item": {"id": "tool-1", "type": "command_execution"},
                },
                {
                    "type": "item.completed",
                    "item": {
                        "id": "tool-1",
                        "type": "command_execution",
                        "model": "gpt-5.6-sol",
                    },
                },
                {
                    "type": "turn.completed",
                    "usage": {
                        "input_tokens": 100,
                        "cached_input_tokens": 80,
                        "output_tokens": 20,
                        "reasoning_output_tokens": 10,
                    },
                },
            ],
        )

        self.assertEqual(turns, 1)
        self.assertIsNone(calls)
        self.assertEqual(tools, 1)
        self.assertEqual(usage["cached_tokens"], 80)
        self.assertEqual(usage["reasoning_tokens"], 10)
        self.assertEqual(models, ["gpt-5.6-sol"])
        self.assertEqual(response_ids, [])

    def test_both_children_receive_the_same_project_local_key(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "agent.toml"
            config.write_text("model = 'unused'\n", encoding="utf-8")
            (root / "auth.json").write_text(
                '{"auth_mode":"apikey","OPENAI_API_KEY":"sk-fake-benchmark-key"}',
                encoding="utf-8",
            )
            runner = BenchmarkRunner(
                manifest=ROOT / "benchmarks" / "manifest.json",
                output_directory=root / "results",
                mini_config=config,
                agents=("mini", "codex"),
            )

            environment = runner._child_environment()

        self.assertEqual(
            environment["CODING_AGENT_API_KEY"], environment["OPENAI_API_KEY"]
        )
        self.assertIn(str(ROOT / "src"), environment["PYTHONPATH"])

    def test_hidden_validation_scores_an_isolated_fake_run(self) -> None:
        class FakeRunner(BenchmarkRunner):
            def _run_mini(self, task, workspace, artifacts):
                path = workspace / "pricing.py"
                path.write_text(
                    path.read_text(encoding="utf-8").replace("> 500", ">= 500"),
                    encoding="utf-8",
                )
                session = {
                    "session_id": "fake-session",
                    "total_usage": {
                        "input_tokens": 100,
                        "cached_tokens": 80,
                        "output_tokens": 10,
                    },
                    "model_call_count": 1,
                    "model_call_records": [
                        {"provider": {"model": "gpt-5.6-sol"}}
                    ],
                    "tool_executions": [{"name": "edit_file"}],
                }
                return [0], "fake-session", [{"type": "mini_session", "session": session}]

        with tempfile.TemporaryDirectory() as directory:
            runner = FakeRunner(
                manifest=ROOT / "benchmarks" / "manifest.json",
                output_directory=Path(directory) / "results",
                mini_config=ROOT / "agent.toml.example",
                agents=("mini",),
                selected_tasks=("boundary-fix",),
            )
            report = runner.run()

            isolated_git = (
                Path(report.results[0].workspace) / ".git"
            ).is_dir()

        result = report.results[0]
        self.assertTrue(result.passed)
        self.assertTrue(result.validation_passed)
        self.assertTrue(isolated_git)
        self.assertEqual(result.changed_paths, ["pricing.py"])
        self.assertEqual(result.cache["cache_reuse_ratio"], 0.8)


if __name__ == "__main__":
    unittest.main()
