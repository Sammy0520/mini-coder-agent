from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path

from mini_coder.subagents import (
    ParallelSubagentCoordinator,
    SubagentError,
    WorkerOutcome,
)


class ParallelSubagentTests(unittest.TestCase):
    def test_bare_allowed_directory_authorizes_descendants(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)

            def worker(spec, child_workspace, event):
                (child_workspace / "frontend").mkdir()
                (child_workspace / "frontend" / "index.html").write_text(
                    "<h1>Ready</h1>\n", encoding="utf-8"
                )
                return WorkerOutcome(status="completed", final_text="done")

            coordinator = ParallelSubagentCoordinator(workspace=workspace, worker=worker)
            result = coordinator.delegate(
                [{
                    "agent_id": "frontend",
                    "role": "implementer",
                    "task": "Build frontend",
                    "allowed_paths": ["frontend"],
                }]
            )

            self.assertEqual(result["results"][0]["status"], "patch_pending")
            self.assertEqual(len(result["pending_bundle_ids"]), 1)

    def test_budget_interrupted_implementer_preserves_bounded_patch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)

            def worker(spec, child_workspace, event):
                (child_workspace / "backend").mkdir()
                (child_workspace / "backend" / "server.py").write_text(
                    "READY = True\n", encoding="utf-8"
                )
                return WorkerOutcome(
                    status="interrupted",
                    final_text="Stopped at token budget after creating the server.",
                    usage={"total_tokens": 40_001},
                    model_calls=5,
                    tool_calls=4,
                )

            coordinator = ParallelSubagentCoordinator(workspace=workspace, worker=worker)
            result = coordinator.delegate(
                [{
                    "agent_id": "backend",
                    "role": "implementer",
                    "task": "Build backend",
                    "allowed_paths": ["backend/**"],
                }]
            )

            child = result["results"][0]
            self.assertEqual(child["status"], "patch_pending")
            self.assertIn("preserved for parent review", child["error"])
            self.assertEqual(len(result["pending_bundle_ids"]), 1)

    def test_parallel_limit_is_intentionally_small(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "between 1 and 2"):
                ParallelSubagentCoordinator(
                    workspace=directory,
                    worker=lambda spec, workspace, event: WorkerOutcome(status="completed"),
                    max_parallel=3,
                )

    def test_two_scouts_really_run_concurrently(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            barrier = threading.Barrier(2, timeout=2)
            active = 0
            peak = 0
            lock = threading.Lock()

            def worker(spec, workspace, event):
                nonlocal active, peak
                with lock:
                    active += 1
                    peak = max(peak, active)
                barrier.wait()
                with lock:
                    active -= 1
                return WorkerOutcome(
                    status="completed",
                    final_text=f"located by {spec.agent_id}",
                    usage={"total_tokens": 10},
                    model_calls=1,
                    tool_calls=2,
                )

            coordinator = ParallelSubagentCoordinator(
                workspace=directory,
                worker=worker,
                max_parallel=2,
            )
            result = coordinator.delegate(
                [
                    {"agent_id": "frontend", "role": "scout", "task": "Find UI"},
                    {"agent_id": "backend", "role": "scout", "task": "Find API"},
                ]
            )

            self.assertEqual(peak, 2)
            self.assertTrue(result["parallel"])
            self.assertEqual(result["subagent_model_calls"], 2)
            self.assertEqual(result["subagent_usage"]["total_tokens"], 20)

    def test_implementer_changes_are_isolated_until_bundle_apply(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            target = workspace / "src" / "feature.py"
            target.parent.mkdir()
            target.write_text("VALUE = 1\n", encoding="utf-8")

            def worker(spec, child_workspace, event):
                child_target = child_workspace / "src" / "feature.py"
                child_target.write_text("VALUE = 2\n", encoding="utf-8")
                (child_workspace / "tests").mkdir(exist_ok=True)
                (child_workspace / "tests" / "test_feature.py").write_text(
                    "def test_value():\n    assert True\n",
                    encoding="utf-8",
                )
                return WorkerOutcome(
                    status="completed",
                    final_text="Implemented the feature and its test.",
                    model_calls=2,
                    tool_calls=4,
                )

            coordinator = ParallelSubagentCoordinator(
                workspace=workspace,
                worker=worker,
            )
            delegated = coordinator.delegate(
                [
                    {
                        "agent_id": "feature_worker",
                        "role": "implementer",
                        "task": "Update feature",
                        "allowed_paths": ["src/**", "tests/**"],
                    }
                ]
            )

            self.assertEqual(target.read_text(encoding="utf-8"), "VALUE = 1\n")
            self.assertEqual(
                list((workspace / ".mini-coder" / "runtime" / "subagents").glob("*/*/workspace")),
                [],
            )
            bundle_id = delegated["pending_bundle_ids"][0]
            applied = coordinator.apply_bundles([bundle_id])

            self.assertTrue(applied["applied"])
            self.assertEqual(applied["file_count"], 2)
            self.assertEqual(target.read_text(encoding="utf-8"), "VALUE = 2\n")
            self.assertTrue((workspace / "tests" / "test_feature.py").is_file())
            self.assertEqual(len(applied["tracked_changes"]), 2)

    def test_scope_violation_never_reaches_real_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            (workspace / "src").mkdir()
            (workspace / "src" / "ok.py").write_text("OK = True\n", encoding="utf-8")

            def worker(spec, child_workspace, event):
                (child_workspace / "outside.txt").write_text("not allowed\n", encoding="utf-8")
                return WorkerOutcome(status="completed", final_text="done")

            coordinator = ParallelSubagentCoordinator(workspace=workspace, worker=worker)
            result = coordinator.delegate(
                [
                    {
                        "agent_id": "bounded",
                        "role": "implementer",
                        "task": "Only edit src",
                        "allowed_paths": ["src/**"],
                    }
                ]
            )

            self.assertEqual(result["results"][0]["status"], "scope_violation")
            self.assertFalse((workspace / "outside.txt").exists())
            self.assertEqual(result["pending_bundle_ids"], [])

    def test_real_workspace_change_causes_bundle_conflict(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            target = workspace / "item.txt"
            target.write_text("base\n", encoding="utf-8")

            def worker(spec, child_workspace, event):
                (child_workspace / "item.txt").write_text("agent\n", encoding="utf-8")
                return WorkerOutcome(status="completed", final_text="done")

            coordinator = ParallelSubagentCoordinator(workspace=workspace, worker=worker)
            result = coordinator.delegate(
                [
                    {
                        "agent_id": "writer",
                        "role": "implementer",
                        "task": "Update item",
                        "allowed_paths": ["item.txt"],
                    }
                ]
            )
            target.write_text("user\n", encoding="utf-8")

            with self.assertRaisesRegex(SubagentError, "Workspace changed"):
                coordinator.apply_bundles(result["pending_bundle_ids"])
            self.assertEqual(target.read_text(encoding="utf-8"), "user\n")

    def test_child_progress_does_not_forward_prompts_or_model_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            events = []

            def worker(spec, workspace, event):
                event(
                    "model_response_received",
                    {"content": "secret generated code", "usage": {"total_tokens": 99}},
                )
                event(
                    "phase_changed",
                    {"phase": "locate", "reason": "found entry point", "prompt": "secret"},
                )
                return WorkerOutcome(status="completed", final_text="done")

            coordinator = ParallelSubagentCoordinator(
                workspace=directory,
                worker=worker,
                event_callback=lambda name, payload: events.append((name, payload)),
            )
            coordinator.delegate(
                [{"agent_id": "safe", "role": "scout", "task": "inspect"}]
            )

            progress = [payload for name, payload in events if name == "subagent_progress"]
            self.assertEqual(progress[0]["child_payload"], {})
            self.assertEqual(
                progress[1]["child_payload"],
                {"phase": "locate", "reason": "found entry point"},
            )

    def test_child_checkpoint_metadata_is_forwarded_without_prompt_content(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            events = []

            def worker(spec, workspace, event):
                event(
                    "context_checkpoint_committed",
                    {
                        "checkpoint_generation": 2,
                        "checkpoint_hash": "safe-hash",
                        "compaction_reason": "high_watermark",
                        "checkpoint_created": True,
                        "estimated_total_tokens": 7_200,
                        "content": "secret prompt and source",
                    },
                )
                return WorkerOutcome(
                    status="completed",
                    final_text="done",
                    context_metrics={
                        "checkpoint_generations": [1, 2],
                        "model_calls": [],
                    },
                )

            coordinator = ParallelSubagentCoordinator(
                workspace=directory,
                worker=worker,
                event_callback=lambda name, payload: events.append((name, payload)),
            )
            result = coordinator.delegate(
                [{"agent_id": "worker", "role": "scout", "task": "inspect"}]
            )

            progress = next(
                payload for name, payload in events if name == "subagent_progress"
            )
            self.assertEqual(
                progress["child_payload"],
                {
                    "checkpoint_generation": 2,
                    "checkpoint_hash": "safe-hash",
                    "compaction_reason": "high_watermark",
                    "checkpoint_created": True,
                    "estimated_total_tokens": 7_200,
                },
            )
            self.assertNotIn("secret", str(progress))
            self.assertEqual(
                result["results"][0]["context_metrics"]["checkpoint_generations"],
                [1, 2],
            )


if __name__ == "__main__":
    unittest.main()
