from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from unittest.mock import MagicMock

from mini_coder.exceptions import PathSafetyError
from mini_coder.tools import create_default_registry
from mini_coder.tools.command import _command_environment
from mini_coder.tools.command_risk import CommandRisk, assess_command
from mini_coder.tools.base import ToolContext
from mini_coder.tools.safety import WorkspacePolicy


class ToolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temp.name)
        self.context = ToolContext(
            policy=WorkspacePolicy(self.workspace),
            command_timeout_seconds=5,
            max_output_chars=4_000,
        )
        self.registry = create_default_registry()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def execute(self, name: str, arguments: dict):
        return self.registry.execute(name, arguments, self.context)

    def test_workspace_policy_rejects_escape_and_secrets(self) -> None:
        with self.assertRaises(PathSafetyError):
            self.context.policy.resolve("../outside.txt")
        with self.assertRaises(PathSafetyError):
            self.context.policy.resolve(".env")
        with self.assertRaises(PathSafetyError):
            self.context.policy.resolve("keys/private.pem")
        with self.assertRaises(PathSafetyError):
            self.context.policy.resolve(".mini-coder/sessions/session.json")
        with self.assertRaises(PathSafetyError):
            self.context.policy.resolve("__pycache__/module.pyc")

    def test_list_files_hides_agent_state_and_python_cache(self) -> None:
        (self.workspace / ".mini-coder" / "sessions").mkdir(parents=True)
        (self.workspace / ".mini-coder" / "sessions" / "session.json").write_text(
            "{}", encoding="utf-8"
        )
        (self.workspace / "__pycache__").mkdir()
        (self.workspace / "__pycache__" / "module.pyc").write_bytes(b"cache")
        (self.workspace / "visible.py").write_text("value = 1\n", encoding="utf-8")

        result = self.execute("list_files", {"path": "."})

        self.assertTrue(result.ok)
        self.assertIn("visible.py", result.data["entries"])
        self.assertFalse(any(".mini-coder" in item for item in result.data["entries"]))
        self.assertFalse(any("__pycache__" in item for item in result.data["entries"]))

    def test_write_read_edit_search_and_list(self) -> None:
        written = self.execute(
            "write_file", {"path": "src/example.py", "content": "value = 1\n"}
        )
        self.assertTrue(written.ok)

        read = self.execute("read_file", {"path": "src/example.py"})
        self.assertTrue(read.ok)
        self.assertIn("value = 1", read.data["content"])

        edited = self.execute(
            "edit_file",
            {"path": "src/example.py", "old_text": "value = 1", "new_text": "value = 2"},
        )
        self.assertTrue(edited.ok)
        self.assertEqual((self.workspace / "src/example.py").read_text(encoding="utf-8"), "value = 2\n")

        searched = self.execute("search_text", {"query": "value = 2", "glob": "*.py"})
        self.assertTrue(searched.ok)
        self.assertEqual(searched.data["matches"][0]["line"], 1)

        listed = self.execute("list_files", {"path": "."})
        self.assertTrue(listed.ok)
        self.assertIn("src/example.py", listed.data["entries"])

    def test_identical_read_reuses_unchanged_range_without_replaying_content(self) -> None:
        path = self.workspace / "sample.py"
        path.write_text("value = 1\n", encoding="utf-8")

        first = self.execute("read_file", {"path": "sample.py"})
        second = self.execute("read_file", {"path": "sample.py"})

        self.assertFalse(first.data["cache_hit"])
        self.assertTrue(second.data["cache_hit"])
        self.assertTrue(second.data["content_unchanged"])
        self.assertEqual(second.data["content"], "")
        self.assertEqual(first.data["content_hash"], second.data["content_hash"])

        path.write_text("value = 2\n", encoding="utf-8")
        changed = self.execute("read_file", {"path": "sample.py"})
        self.assertFalse(changed.data["cache_hit"])
        self.assertIn("value = 2", changed.data["content"])

    def test_overlapping_read_returns_only_previously_unseen_lines(self) -> None:
        path = self.workspace / "lines.txt"
        path.write_text(
            "\n".join(f"line {index}" for index in range(1, 11)) + "\n",
            encoding="utf-8",
        )

        first = self.execute(
            "read_file", {"path": "lines.txt", "start_line": 1, "max_lines": 5}
        )
        overlap = self.execute(
            "read_file", {"path": "lines.txt", "start_line": 3, "max_lines": 5}
        )
        covered = self.execute(
            "read_file", {"path": "lines.txt", "start_line": 2, "max_lines": 4}
        )

        self.assertFalse(first.data["cache_hit"])
        self.assertTrue(overlap.data["cache_hit"])
        self.assertTrue(overlap.data["partial_cache_hit"])
        self.assertEqual(overlap.data["covered_ranges"], [(3, 5)])
        self.assertEqual(overlap.data["returned_ranges"], [(6, 7)])
        self.assertNotIn("line 3", overlap.data["content"])
        self.assertIn("line 6", overlap.data["content"])
        self.assertTrue(covered.data["cache_hit"])
        self.assertEqual(covered.data["content"], "")

    def test_search_cache_reuses_equivalent_query_and_covered_page(self) -> None:
        (self.workspace / "items.txt").write_text(
            "\n".join(f"Needle {index}" for index in range(6)) + "\n",
            encoding="utf-8",
        )

        first = self.execute(
            "search_text", {"query": "needle", "max_results": 5}
        )
        exact = self.execute(
            "search_text", {"query": "NEEDLE", "max_results": 5}
        )
        subset = self.execute(
            "search_text",
            {"query": "needle", "offset": 2, "max_results": 2},
        )

        self.assertFalse(first.data["cache_hit"])
        self.assertTrue(exact.data["cache_hit"])
        self.assertEqual(exact.data["cache_replay"], "summary")
        self.assertEqual(exact.data["reused_match_count"], 5)
        self.assertEqual(exact.data["matches"], [])
        self.assertTrue(subset.data["cache_hit"])
        self.assertEqual(subset.data["cache_replay"], "covered_subset")
        self.assertEqual([item["line"] for item in subset.data["matches"]], [3, 4])

    def test_search_cache_is_invalidated_after_workspace_write(self) -> None:
        (self.workspace / "value.txt").write_text("old value\n", encoding="utf-8")
        first = self.execute("search_text", {"query": "value"})
        cached = self.execute("search_text", {"query": "value"})

        self.execute(
            "write_file",
            {"path": "new.txt", "content": "new value\n"},
        )
        refreshed = self.execute("search_text", {"query": "value"})

        self.assertFalse(first.data["cache_hit"])
        self.assertTrue(cached.data["cache_hit"])
        self.assertFalse(refreshed.data["cache_hit"])
        self.assertEqual(len(refreshed.data["matches"]), 2)

    def test_list_search_and_read_support_continuation_metadata(self) -> None:
        for index in range(5):
            (self.workspace / f"file{index}.txt").write_text(
                f"needle {index}\nsecond line\n",
                encoding="utf-8",
            )

        first_list = self.execute("list_files", {"max_entries": 2})
        second_list = self.execute(
            "list_files",
            {"max_entries": 2, "offset": first_list.data["next_offset"]},
        )
        self.assertTrue(first_list.data["truncated"])
        self.assertEqual(first_list.data["next_offset"], 2)
        self.assertFalse(set(first_list.data["entries"]) & set(second_list.data["entries"]))

        first_search = self.execute(
            "search_text",
            {"query": "needle", "max_results": 2},
        )
        second_search = self.execute(
            "search_text",
            {
                "query": "needle",
                "max_results": 2,
                "offset": first_search.data["next_offset"],
            },
        )
        self.assertEqual([item["line"] for item in first_search.data["matches"]], [1, 1])
        self.assertEqual(first_search.data["next_offset"], 2)
        self.assertNotEqual(
            first_search.data["matches"][0]["path"],
            second_search.data["matches"][0]["path"],
        )

        read = self.execute("read_file", {"path": "file0.txt", "max_lines": 1})
        self.assertTrue(read.data["truncated"])
        self.assertEqual(read.data["next_start_line"], 2)
        continued = self.execute(
            "read_file",
            {"path": "file0.txt", "start_line": read.data["next_start_line"]},
        )
        self.assertIn("second line", continued.data["content"])

    def test_search_explains_when_all_candidates_are_filtered(self) -> None:
        (self.workspace / "node_modules").mkdir()
        (self.workspace / "node_modules" / "hidden.js").write_text(
            "secret needle\n",
            encoding="utf-8",
        )

        result = self.execute("search_text", {"query": "needle"})

        self.assertTrue(result.ok)
        self.assertEqual(result.data["matches"], [])
        self.assertEqual(result.data["outcome"], "all_candidates_filtered")
        self.assertGreater(result.data["filtered"]["policy"], 0)

    def test_edit_refuses_ambiguous_match(self) -> None:
        (self.workspace / "repeat.txt").write_text("same\nsame\n", encoding="utf-8")
        result = self.execute(
            "edit_file",
            {"path": "repeat.txt", "old_text": "same", "new_text": "changed"},
        )
        self.assertFalse(result.ok)
        self.assertEqual(
            (self.workspace / "repeat.txt").read_text(encoding="utf-8"),
            "same\nsame\n",
        )

    def test_command_returns_output_and_exit_code(self) -> None:
        command = f'"{sys.executable}" -c "print(12345)"'
        result = self.execute("run_command", {"command": command})
        self.assertTrue(result.ok, result.message)
        self.assertEqual(result.data["exit_code"], 0)
        self.assertFalse(result.data["output_truncated"])
        self.assertEqual(result.data["command_risk"], "unknown")
        self.assertIn("12345", result.data["stdout"])

    def test_command_can_treat_an_expected_nonzero_exit_as_success(self) -> None:
        command = f'"{sys.executable}" -c "import sys; sys.exit(2)"'

        result = self.execute(
            "run_command",
            {
                "command": command,
                "expected_exit_codes": [2],
                "purpose": "verify",
                "verification_mode": "expected_rejection",
            },
        )

        self.assertTrue(result.ok, result.data)
        self.assertEqual(result.data["exit_code"], 2)
        self.assertEqual(result.data["expected_exit_codes"], [2])
        self.assertTrue(result.data["expectation_met"])
        self.assertEqual(result.data["verification_mode"], "expected_rejection")

    def test_command_rejects_invalid_expected_exit_codes(self) -> None:
        command = f'"{sys.executable}" -c "print(1)"'

        result = self.execute(
            "run_command",
            {"command": command, "expected_exit_codes": [0, 0]},
        )

        self.assertFalse(result.ok)
        self.assertIn("must not contain duplicates", result.message)

    def test_command_risk_classifier_is_conservative(self) -> None:
        self.assertEqual(assess_command("git status").level, CommandRisk.READ_ONLY)
        self.assertEqual(
            assess_command("python -m pip show PySide6").level,
            CommandRisk.READ_ONLY,
        )
        self.assertEqual(
            assess_command("python -m unittest").level,
            CommandRisk.WORKSPACE_WRITE,
        )
        self.assertEqual(assess_command("git push origin main").level, CommandRisk.EXTERNAL_EFFECT)
        self.assertEqual(assess_command("rm -rf build").level, CommandRisk.DANGEROUS)
        self.assertEqual(assess_command("mv old.txt new.txt").level, CommandRisk.DANGEROUS)
        self.assertEqual(assess_command("copy old.txt new.txt").level, CommandRisk.DANGEROUS)
        self.assertEqual(assess_command("python script.py").level, CommandRisk.UNKNOWN)
        self.assertEqual(
            assess_command("git status && git log -1").level,
            CommandRisk.UNKNOWN,
        )

    def test_command_timeout_terminates_process_tree_and_records_truncation(self) -> None:
        command = f'"{sys.executable}" -c "import time; time.sleep(5)"'

        result = self.execute(
            "run_command",
            {"command": command, "timeout_seconds": 1},
        )

        self.assertFalse(result.ok)
        self.assertTrue(result.data["timed_out"])
        self.assertTrue(result.data["process_tree_terminated"])
        self.assertIn("output_truncated", result.data)
        self.assertLess(result.data["duration_seconds"], 4)

    def test_command_cancellation_terminates_process_tree(self) -> None:
        release = threading.Event()
        process = MagicMock()
        process.pid = 12345
        process.poll.return_value = None
        process.communicate.side_effect = lambda: (release.wait(2), ("", ""))[1]
        self.context.cancellation_requested = MagicMock(side_effect=[False, True])

        def terminate(_process):
            release.set()
            return True

        with patch(
            "mini_coder.tools.command.subprocess.Popen",
            return_value=process,
        ), patch(
            "mini_coder.tools.command._terminate_process_tree",
            side_effect=terminate,
        ) as terminate_tree:
            result = self.execute(
                "run_command",
                {"command": "python long_task.py", "timeout_seconds": 5},
            )

        self.assertFalse(result.ok)
        self.assertTrue(result.data["cancelled"])
        self.assertTrue(result.data["process_tree_terminated"])
        terminate_tree.assert_called_once_with(process)

    def test_keyboard_interrupt_requests_process_tree_cleanup(self) -> None:
        process = MagicMock()
        process.pid = 12345
        process.poll.return_value = None
        process.communicate.side_effect = [KeyboardInterrupt(), ("", "")]
        with patch("mini_coder.tools.command.subprocess.Popen", return_value=process), patch(
            "mini_coder.tools.command._terminate_process_tree",
            return_value=True,
        ) as terminate:
            with self.assertRaises(KeyboardInterrupt):
                self.execute("run_command", {"command": "python script.py"})

        terminate.assert_called_once_with(process)

    def test_command_output_redacts_key_shaped_values(self) -> None:
        fake_key = "sk-command-secret-123456"
        command = f'"{sys.executable}" -c "print(\'{fake_key}\')"'

        result = self.execute("run_command", {"command": command})

        self.assertTrue(result.ok)
        self.assertNotIn(fake_key, result.data["stdout"])
        self.assertIn("[REDACTED]", result.data["stdout"])

    def test_command_prefers_agent_python_on_path(self) -> None:
        result = self.execute(
            "run_command",
            {"command": 'python -c "import sys; print(sys.executable)"'},
        )
        self.assertTrue(result.ok, result.data)
        actual = os.path.normcase(os.path.abspath(result.data["stdout"].strip()))
        expected = os.path.normcase(os.path.abspath(sys.executable))
        self.assertEqual(actual, expected)

    def test_command_environment_can_preserve_project_path(self) -> None:
        with patch.dict(
            os.environ,
            {"PATH": "project-bin", "VIRTUAL_ENV": "project-environment"},
            clear=False,
        ):
            environment = _command_environment(preserve_project_path=True)

        self.assertEqual(environment["PATH"], "project-bin")
        self.assertEqual(environment["VIRTUAL_ENV"], "project-environment")

    def test_command_does_not_inherit_model_api_keys(self) -> None:
        command = (
            'python -c "import os; '
            "print(os.getenv('OPENAI_API_KEY', 'missing')); "
            "print(os.getenv('CODING_AGENT_API_KEY', 'missing'))\""
        )
        with patch.dict(
            os.environ,
            {
                "OPENAI_API_KEY": "must-not-leak",
                "CODING_AGENT_API_KEY": "must-not-leak",
            },
        ):
            result = self.execute("run_command", {"command": command})

        self.assertTrue(result.ok, result.data)
        self.assertEqual(result.data["stdout"].splitlines(), ["missing", "missing"])

    def test_command_receives_agent_runtime_directory(self) -> None:
        runtime_directory = self.workspace / ".mini-coder" / "runtime" / "test-run"
        self.context.runtime_directory = runtime_directory
        command = (
            'python -c "import os; '
            "print(os.environ.get('MINI_CODER_RUNTIME_DIR', 'missing'))\""
        )

        result = self.execute("run_command", {"command": command})

        self.assertTrue(result.ok, result.data)
        self.assertEqual(
            os.path.normcase(os.path.abspath(result.data["stdout"].strip())),
            os.path.normcase(os.path.abspath(runtime_directory)),
        )
        self.assertTrue(runtime_directory.is_dir())

    def test_registry_rejects_unknown_and_extra_arguments(self) -> None:
        unknown = self.execute("does_not_exist", {})
        self.assertFalse(unknown.ok)
        extra = self.execute("read_file", {"path": "a.txt", "surprise": True})
        self.assertFalse(extra.ok)
        self.assertIn("Unexpected argument", extra.message)
        self.assertEqual(extra.data["error_code"], "invalid_arguments")
        self.assertIn("schema", extra.data["suggestion"])
        invalid_enum = self.execute(
            "run_command",
            {"command": "python --version", "purpose": "pretend"},
        )
        self.assertFalse(invalid_enum.ok)
        self.assertIn("must be one of", invalid_enum.message)
        self.assertEqual(invalid_enum.data["error_code"], "invalid_argument_value")


if __name__ == "__main__":
    unittest.main()
