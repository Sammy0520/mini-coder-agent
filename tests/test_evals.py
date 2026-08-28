from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import redirect_stderr
from dataclasses import replace
from io import StringIO
from pathlib import Path

from mini_coder.evals.cli import main as eval_main
from mini_coder.evals.runner import EvalRunner
from mini_coder.evals.scenarios import all_scenarios, get_scenarios
from mini_coder.session import SessionStore


class EvalScenarioTests(unittest.TestCase):
    def test_catalog_contains_all_required_distinct_scenarios(self) -> None:
        scenarios = all_scenarios()
        identifiers = [item.scenario_id for item in scenarios]

        self.assertEqual(len(identifiers), 10)
        self.assertEqual(len(set(identifiers)), len(identifiers))
        self.assertIn("boundary_bug", identifiers)
        self.assertIn("multifile_interface", identifiers)
        self.assertIn("failed_then_fix", identifiers)
        self.assertIn("readonly_analysis", identifiers)
        self.assertIn("workspace_escape", identifiers)
        self.assertIn("session_resume", identifiers)
        self.assertIn("approval_denied", identifiers)
        self.assertIn("undo_conflict", identifiers)
        self.assertIn("rate_limit_retry", identifiers)
        self.assertIn("long_output", identifiers)

    def test_scenario_selection_rejects_unknown_name(self) -> None:
        with self.assertRaisesRegex(ValueError, "unknown eval scenario"):
            get_scenarios(["missing"])

    def test_live_mode_requires_explicit_scenario(self) -> None:
        with redirect_stderr(StringIO()):
            self.assertEqual(eval_main(["--live"]), 2)


class EvalRunnerTests(unittest.TestCase):
    def test_all_deterministic_evals_pass_and_write_reports(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            output = Path(raw) / "results"
            report = EvalRunner(output_directory=output).run(all_scenarios())

            self.assertTrue(report.success)
            self.assertEqual(report.passed, 10)
            self.assertEqual(report.failed, 0)
            self.assertEqual(report.total, 10)
            self.assertTrue((output / "summary.md").is_file())
            payload = json.loads((output / "eval-report.json").read_text(encoding="utf-8"))
            self.assertEqual(payload["schema_version"], 1)
            self.assertEqual(payload["mode"], "deterministic")
            self.assertTrue(payload["success"])
            self.assertFalse((output / "artifacts").exists())

            by_name = {item.scenario_id: item for item in report.results}
            self.assertEqual(by_name["failed_then_fix"].verification_status, "passed")
            self.assertGreaterEqual(by_name["failed_then_fix"].tool_calls, 4)
            self.assertEqual(by_name["rate_limit_retry"].retries, 1)
            self.assertTrue(by_name["workspace_escape"].workspace_boundary_attempted)
            self.assertFalse(by_name["workspace_escape"].workspace_boundary_violated)
            self.assertFalse(by_name["readonly_analysis"].changed_paths)
            self.assertFalse(by_name["boundary_bug"].usage_available)

    def test_failed_eval_preserves_session_events_and_workspace(self) -> None:
        scenario = replace(
            get_scenarios(["readonly_analysis"])[0],
            expected_changed_paths=frozenset({"impossible.py"}),
        )
        with tempfile.TemporaryDirectory() as raw:
            output = Path(raw) / "results"
            report = EvalRunner(output_directory=output).run((scenario,))

            self.assertFalse(report.success)
            result = report.results[0]
            self.assertIsNotNone(result.artifact_directory)
            artifacts = Path(result.artifact_directory or "")
            self.assertTrue((artifacts / "workspace" / "config.py").is_file())
            self.assertTrue((artifacts / "events.json").is_file())
            self.assertTrue(any((artifacts / "sessions").glob("*.json")))
            self.assertFalse((artifacts / "workspace" / ".mini-coder").exists())

    def test_live_mode_rejects_fault_injection_scenarios_before_provider_use(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            runner = EvalRunner(output_directory=Path(raw), live=True)
            with self.assertRaisesRegex(ValueError, "deterministic fault injection"):
                runner.run(get_scenarios(["rate_limit_retry"]))

    def test_live_failure_artifacts_default_to_hash_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            workspace = root / "workspace"
            workspace.mkdir()
            (workspace / "private.py").write_text("TOKEN = 'private-value'\n", encoding="utf-8")
            runner = EvalRunner(output_directory=root / "results", live=True)
            store = SessionStore(workspace / ".mini-coder" / "sessions")

            target = runner._preserve_artifacts(
                get_scenarios(["boundary_bug"])[0],
                workspace,
                [],
                store,
                None,
            )

            self.assertTrue((target / "workspace-hashes.json").is_file())
            self.assertFalse((target / "workspace").exists())
            self.assertNotIn(
                "private-value",
                (target / "workspace-hashes.json").read_text(encoding="utf-8"),
            )


if __name__ == "__main__":
    unittest.main()
