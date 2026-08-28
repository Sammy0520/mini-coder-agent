from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path


REPOSITORY = Path(__file__).resolve().parent.parent


class ReleaseScriptTests(unittest.TestCase):
    def test_secret_check_accepts_repository_candidates(self) -> None:
        completed = subprocess.run(
            [sys.executable, "scripts/check-secrets.py"],
            cwd=REPOSITORY,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertIn("secret check passed", completed.stdout)

    def test_demo_reset_creates_expected_failing_fixture(self) -> None:
        completed = subprocess.run(
            [sys.executable, "scripts/reset-demo.py"],
            cwd=REPOSITORY,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        workspace = REPOSITORY / "examples" / "order_service" / "workspace"
        self.assertTrue((workspace / "TASK.md").is_file())
        self.assertTrue((workspace / "shop" / "pricing.py").is_file())
        self.assertFalse((workspace / "shop" / "policy.py").exists())
        git_check = subprocess.run(
            ["git", "status", "--short"],
            cwd=workspace,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        self.assertEqual(git_check.returncode, 0, git_check.stderr)
        self.assertEqual(git_check.stdout, "")


if __name__ == "__main__":
    unittest.main()
