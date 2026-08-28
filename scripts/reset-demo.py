from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path


def _remove_readonly(function, path: str, _error) -> None:
    """Retry deletion of Windows Git objects that carry a read-only bit."""
    os.chmod(path, stat.S_IWRITE)
    function(path)


def main() -> int:
    repository = Path(__file__).resolve().parent.parent
    demo_root = (repository / "examples" / "order_service").resolve()
    fixture = (demo_root / "fixture").resolve()
    workspace = (demo_root / "workspace").resolve()

    if fixture.parent != demo_root or workspace.parent != demo_root:
        raise RuntimeError("refusing to reset a path outside the order_service demo")
    if not fixture.is_dir():
        raise RuntimeError(f"demo fixture is missing: {fixture}")
    if workspace.exists():
        shutil.rmtree(workspace, onerror=_remove_readonly)
    shutil.copytree(fixture, workspace)
    git_available = shutil.which("git") is not None
    if git_available:
        commands = (
            ("git", "init", "-q"),
            ("git", "config", "user.name", "Mini Coder Demo"),
            ("git", "config", "user.email", "demo@invalid.local"),
            ("git", "add", "."),
            ("git", "-c", "commit.gpgsign=false", "commit", "-q", "-m", "demo baseline"),
        )
        for command in commands:
            completed = subprocess.run(
                command,
                cwd=workspace,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            if completed.returncode != 0:
                raise RuntimeError(
                    f"could not initialize isolated demo Git repository: "
                    f"{completed.stderr.strip()}"
                )
    print(f"Reset demo workspace: {workspace}")
    if git_available:
        print("Initialized an isolated Git baseline inside the demo workspace.")
    else:
        print("Git was not found; the demo still works but has no isolated Git baseline.")
    print("Expected initial state: tests fail because shop.policy is not implemented yet.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
