from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from mini_coder.tools.safety import WorkspacePolicy
from mini_coder.workspace import (
    capture_git_snapshot,
    compare_git_snapshots,
    inspect_workspace,
    render_workspace_overview,
)


class WorkspaceInspectionTests(unittest.TestCase):
    def test_bounded_overview_finds_manifests_tests_and_instruction_priority(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "pyproject.toml").write_text(
                '[project]\nname="demo"\ndependencies=["pytest"]\n',
                encoding="utf-8",
            )
            (root / "AGENTS.md").write_text("root guidance", encoding="utf-8")
            (root / "src" / "feature").mkdir(parents=True)
            (root / "src" / "feature" / "AGENTS.md").write_text(
                "nested guidance",
                encoding="utf-8",
            )
            (root / "src" / "feature" / "main.py").write_text(
                "def main(): pass\n",
                encoding="utf-8",
            )
            (root / "tests").mkdir()
            (root / "tests" / "test_feature.py").write_text("def test_ok(): pass\n")
            (root / "node_modules").mkdir()
            (root / "node_modules" / "noise.js").write_text("noise")
            (root / "build").mkdir()
            (root / "build" / "artifact.txt").write_text("noise")

            overview = inspect_workspace(root, WorkspacePolicy(root))
            rendered = render_workspace_overview(overview)

            self.assertIn("pyproject.toml", overview["manifests"])
            self.assertIn("tests/", overview["test_paths"])
            self.assertIn("tests/test_feature.py", overview["test_paths"])
            self.assertEqual(overview["entry_points"], ["src/feature/main.py"])
            self.assertEqual(overview["verification_candidates"], ["python -m pytest"])
            instruction_paths = [item["path"] for item in overview["instruction_files"]]
            self.assertLess(
                instruction_paths.index("src/feature/AGENTS.md"),
                instruction_paths.index("AGENTS.md"),
            )
            self.assertNotIn("noise.js", rendered)
            self.assertNotIn("artifact.txt", rendered)
            self.assertIn("bounded discovery", rendered)

    def test_git_snapshot_distinguishes_existing_external_and_agent_changes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._git(root, "init")
            self._git(root, "config", "user.email", "test@example.invalid")
            self._git(root, "config", "user.name", "Test User")
            (root / "existing.txt").write_text("committed\n", encoding="utf-8")
            (root / "agent.txt").write_text("committed\n", encoding="utf-8")
            self._git(root, "add", "existing.txt", "agent.txt")
            self._git(root, "commit", "-m", "baseline")
            (root / "existing.txt").write_text("user before\n", encoding="utf-8")

            baseline = capture_git_snapshot(root)
            (root / "existing.txt").write_text("user during\n", encoding="utf-8")
            (root / "external.txt").write_text("external\n", encoding="utf-8")
            (root / "中文.txt").write_text("external unicode path\n", encoding="utf-8")
            (root / "agent.txt").write_text("agent\n", encoding="utf-8")
            (root / "node_modules").mkdir()
            (root / "node_modules" / "noise.js").write_text("ignored\n", encoding="utf-8")
            current = capture_git_snapshot(root)

            self.assertTrue(baseline["available"])
            self.assertEqual([item["path"] for item in baseline["entries"]], ["existing.txt"])
            changed = compare_git_snapshots(
                baseline,
                current,
                agent_paths={"agent.txt"},
            )
            self.assertEqual(changed, ["existing.txt", "external.txt", "中文.txt"])
            self.assertNotIn(
                "node_modules/noise.js",
                [item["path"] for item in current["entries"]],
            )

    @staticmethod
    def _git(root: Path, *arguments: str) -> None:
        completed = subprocess.run(
            ["git", "-C", str(root), *arguments],
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            raise AssertionError(completed.stderr)


if __name__ == "__main__":
    unittest.main()
