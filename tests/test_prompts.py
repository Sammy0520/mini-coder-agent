from __future__ import annotations

import os
import platform
import unittest

from mini_coder.prompts import build_system_prompt


class PromptTests(unittest.TestCase):
    def test_default_prompt_contains_portable_tool_guidance(self) -> None:
        prompt = build_system_prompt()

        self.assertIn("search_text", prompt)
        self.assertIn("edit_file", prompt)
        self.assertIn("Never invoke apply_patch", prompt)
        self.assertIn("do not assume Bash", prompt)
        self.assertIn("Do not assume the workspace is a Git repository", prompt)
        self.assertIn("python -m unittest", prompt)
        self.assertIn("same language as the user", prompt)
        self.assertIn("not like a technical report", prompt)
        self.assertIn("Do not dump source code", prompt)
        self.assertIn(platform.system(), prompt)
        if os.name == "nt":
            self.assertIn("cmd.exe semantics", prompt)


if __name__ == "__main__":
    unittest.main()
