from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mini_coder.exceptions import PathSafetyError
from mini_coder.tools import create_default_registry
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
        self.assertIn("12345", result.data["stdout"])

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


if __name__ == "__main__":
    unittest.main()
