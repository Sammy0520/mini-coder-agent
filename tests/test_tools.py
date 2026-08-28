from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from unittest.mock import MagicMock

from mini_coder.exceptions import PathSafetyError
from mini_coder.tools import create_default_registry
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

    def test_command_risk_classifier_is_conservative(self) -> None:
        self.assertEqual(assess_command("git status").level, CommandRisk.READ_ONLY)
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

    def test_registry_rejects_unknown_and_extra_arguments(self) -> None:
        unknown = self.execute("does_not_exist", {})
        self.assertFalse(unknown.ok)
        extra = self.execute("read_file", {"path": "a.txt", "surprise": True})
        self.assertFalse(extra.ok)
        self.assertIn("Unexpected argument", extra.message)
        invalid_enum = self.execute(
            "run_command",
            {"command": "python --version", "purpose": "pretend"},
        )
        self.assertFalse(invalid_enum.ok)
        self.assertIn("must be one of", invalid_enum.message)


if __name__ == "__main__":
    unittest.main()
