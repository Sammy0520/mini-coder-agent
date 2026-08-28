from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tomllib
from pathlib import Path
from typing import Any

from .tools.safety import WorkspacePolicy


_MANIFESTS = {
    "pyproject.toml",
    "requirements.txt",
    "setup.cfg",
    "package.json",
    "cargo.toml",
    "pom.xml",
    "build.gradle",
    "build.gradle.kts",
    "go.mod",
    "pnpm-lock.yaml",
    "yarn.lock",
    "package-lock.json",
}
_INSTRUCTIONS = {
    "agents.md": 100,
    ".github/copilot-instructions.md": 90,
    "claude.md": 80,
    "contributing.md": 70,
    "readme.md": 20,
}
_TEST_DIR_NAMES = {"test", "tests", "spec", "specs", "__tests__"}
_ENTRY_NAMES = {
    "main.py",
    "app.py",
    "manage.py",
    "cli.py",
    "index.js",
    "index.ts",
    "main.js",
    "main.ts",
    "main.rs",
    "main.go",
}
_MAX_SCAN_ENTRIES = 1_500
_MAX_SCAN_DEPTH = 5
_MAX_FINGERPRINT_BYTES = 2_000_000


def inspect_workspace(root: Path, policy: WorkspacePolicy) -> dict[str, Any]:
    """Build a bounded, deterministic overview without reading the whole repository."""
    manifests: list[str] = []
    instructions: list[dict[str, Any]] = []
    test_paths: list[str] = []
    entry_points: list[str] = []
    root_entries: list[str] = []
    scanned = 0
    skipped = 0

    for child in sorted(root.iterdir(), key=lambda item: item.name.casefold()):
        if policy.is_denied(child):
            skipped += 1
            continue
        root_entries.append(policy.display(child) + ("/" if child.is_dir() else ""))
        if len(root_entries) >= 40:
            break

    stack: list[tuple[Path, int]] = [(root, 1)]
    while stack and scanned < _MAX_SCAN_ENTRIES:
        directory, depth = stack.pop()
        try:
            children = sorted(
                directory.iterdir(),
                key=lambda item: (item.is_file(), item.name.casefold()),
                reverse=True,
            )
        except OSError:
            skipped += 1
            continue
        for child in children:
            if scanned >= _MAX_SCAN_ENTRIES:
                break
            scanned += 1
            if policy.is_denied(child):
                skipped += 1
                continue
            relative = policy.display(child)
            lowered = relative.casefold()
            if child.is_dir():
                if child.name.casefold() in _TEST_DIR_NAMES:
                    test_paths.append(relative + "/")
                if depth < _MAX_SCAN_DEPTH:
                    stack.append((child, depth + 1))
                continue
            if child.name.casefold() in _MANIFESTS:
                manifests.append(relative)
            instruction_priority = _instruction_priority(lowered)
            if instruction_priority is not None:
                instructions.append(
                    {
                        "path": relative,
                        "priority": instruction_priority + len(child.relative_to(root).parts),
                    }
                )
            if _looks_like_test(child.name):
                test_paths.append(relative)
            if child.name.casefold() in _ENTRY_NAMES:
                entry_points.append(relative)

    manifests = sorted(set(manifests))[:50]
    instructions = sorted(
        instructions,
        key=lambda item: (-int(item["priority"]), str(item["path"])),
    )[:30]
    test_paths = sorted(set(test_paths))[:80]
    entry_points = sorted(set(entry_points), key=lambda item: (item.count("/"), item))[:40]
    verification = _verification_candidates(root, manifests, test_paths)
    git = capture_git_snapshot(root)
    return {
        "root_entries": root_entries,
        "manifests": manifests,
        "instruction_files": instructions,
        "test_paths": test_paths,
        "entry_points": entry_points,
        "verification_candidates": verification,
        "scan": {
            "scanned_entries": scanned,
            "skipped_entries": skipped,
            "truncated": bool(stack) or scanned >= _MAX_SCAN_ENTRIES,
            "max_depth": _MAX_SCAN_DEPTH,
        },
        "git": git,
    }


def render_workspace_overview(overview: dict[str, Any]) -> str:
    def rendered(items: list[Any], empty: str = "none detected") -> str:
        return ", ".join(str(item) for item in items) if items else empty

    instructions = [
        f"{item['path']} (priority {item['priority']})"
        for item in overview.get("instruction_files", [])
        if isinstance(item, dict)
    ]
    git = overview.get("git", {})
    git_text = "not detected"
    if isinstance(git, dict) and git.get("available"):
        dirty = len(git.get("entries", []))
        git_text = f"branch={git.get('branch') or 'detached'}, task-start changes={dirty}"
    scan = overview.get("scan", {})
    return (
        "Local workspace overview (bounded discovery; inspect files before relying on details):\n"
        f"- Root entries: {rendered(overview.get('root_entries', []))}\n"
        f"- Manifests: {rendered(overview.get('manifests', []))}\n"
        f"- Likely entry points: {rendered(overview.get('entry_points', []))}\n"
        f"- Tests: {rendered(overview.get('test_paths', []))}\n"
        f"- Verification candidates: {rendered(overview.get('verification_candidates', []))}\n"
        f"- Project instruction files: {rendered(instructions)}\n"
        "- Instruction policy: system/runtime safety rules always win; among project files, "
        "more specific nested AGENTS.md guidance applies within its subtree. Read relevant "
        "instruction files before changing files there.\n"
        f"- Git: {git_text}\n"
        f"- Discovery scanned {scan.get('scanned_entries', 0)} entries, skipped "
        f"{scan.get('skipped_entries', 0)}, truncated={bool(scan.get('truncated', False))}."
    )


def capture_git_snapshot(root: Path, *, max_entries: int = 500) -> dict[str, Any]:
    git_root = _find_git_marker(root)
    if git_root is None:
        return {"available": False, "entries": [], "truncated": False}
    status = _run_git(
        root,
        ["status", "--porcelain=v1", "--untracked-files=all", "--", "."],
        safe_directory=git_root,
    )
    if status is None:
        return {"available": False, "entries": [], "truncated": False}
    entries: list[dict[str, Any]] = []
    policy = WorkspacePolicy(root)
    lines = status.splitlines()
    truncated = len(lines) > 5_000
    for line in lines[:5_000]:
        if len(line) < 4:
            continue
        path_text = line[3:]
        if " -> " in path_text:
            path_text = path_text.rsplit(" -> ", 1)[-1]
        path_text = path_text.strip('"').replace("\\", "/")
        candidate = (root / path_text).resolve()
        if not candidate.is_relative_to(root.resolve()) or policy.is_denied(candidate):
            continue
        if len(entries) >= max_entries:
            truncated = True
            break
        entries.append(
            {
                "status": line[:2],
                "path": path_text,
                "fingerprint": _fingerprint(candidate),
            }
        )
    branch = _run_git(root, ["branch", "--show-current"], safe_directory=git_root)
    head = _run_git(root, ["rev-parse", "--short", "HEAD"], safe_directory=git_root)
    return {
        "available": True,
        "branch": (branch or "").strip() or None,
        "head": (head or "").strip() or None,
        "entries": entries,
        "truncated": truncated,
    }


def compare_git_snapshots(
    baseline: dict[str, Any],
    current: dict[str, Any],
    *,
    agent_paths: set[str],
) -> list[str]:
    if not baseline.get("available") or not current.get("available"):
        return []
    before = {
        str(item.get("path")): (item.get("status"), item.get("fingerprint"))
        for item in baseline.get("entries", [])
        if isinstance(item, dict) and item.get("path")
    }
    after = {
        str(item.get("path")): (item.get("status"), item.get("fingerprint"))
        for item in current.get("entries", [])
        if isinstance(item, dict) and item.get("path")
    }
    changed = []
    for path in sorted(set(before) | set(after)):
        if path in agent_paths:
            continue
        if before.get(path) != after.get(path):
            changed.append(path)
    return changed


def _instruction_priority(relative: str) -> int | None:
    name = relative.rsplit("/", 1)[-1]
    if name == "agents.md":
        return _INSTRUCTIONS["agents.md"]
    return _INSTRUCTIONS.get(relative) or _INSTRUCTIONS.get(name)


def _looks_like_test(name: str) -> bool:
    lowered = name.casefold()
    return (
        lowered.startswith("test_")
        or lowered.endswith(("_test.py", ".test.js", ".test.ts", ".spec.js", ".spec.ts"))
        or lowered in {"pytest.ini", "tox.ini", "jest.config.js", "vitest.config.ts"}
    )


def _verification_candidates(root: Path, manifests: list[str], tests: list[str]) -> list[str]:
    names = {Path(item).name.casefold() for item in manifests}
    candidates: list[str] = []
    if "pyproject.toml" in names:
        for manifest in manifests:
            if Path(manifest).name.casefold() != "pyproject.toml":
                continue
            try:
                data = tomllib.loads((root / manifest).read_text(encoding="utf-8"))
            except (OSError, UnicodeError, tomllib.TOMLDecodeError):
                data = {}
            if "pytest" in json.dumps(data, ensure_ascii=False).casefold():
                candidates.append("python -m pytest")
                break
    if not candidates and "requirements.txt" in names:
        for manifest in manifests:
            if Path(manifest).name.casefold() != "requirements.txt":
                continue
            try:
                requirements = (root / manifest).read_text(encoding="utf-8").casefold()
            except (OSError, UnicodeError):
                requirements = ""
            if "pytest" in requirements:
                candidates.append("python -m pytest")
                break
    if not candidates and any(
        Path(item).name.casefold() in {"pytest.ini", "tox.ini"} for item in tests
    ):
        candidates.append("python -m pytest")
    if any(Path(item).suffix == ".py" for item in tests) and not candidates:
        candidates.append("python -m unittest discover -v")
    if "package.json" in names:
        for manifest in manifests:
            if Path(manifest).name.casefold() != "package.json":
                continue
            try:
                package = json.loads((root / manifest).read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                package = {}
            scripts = package.get("scripts", {}) if isinstance(package, dict) else {}
            if isinstance(scripts, dict) and scripts.get("test"):
                manifest_dir = Path(manifest).parent
                if (root / manifest_dir / "pnpm-lock.yaml").exists():
                    candidates.append("pnpm test")
                elif (root / manifest_dir / "yarn.lock").exists():
                    candidates.append("yarn test")
                else:
                    candidates.append("npm test")
            break
    if "cargo.toml" in names:
        candidates.append("cargo test")
    if "pom.xml" in names:
        candidates.append("mvn test")
    if {"build.gradle", "build.gradle.kts"} & names:
        candidates.append("gradle test")
    if "go.mod" in names:
        candidates.append("go test ./...")
    return candidates[:10]


def _find_git_marker(root: Path) -> Path | None:
    current = root.resolve()
    for candidate in (current, *current.parents):
        if (candidate / ".git").exists():
            return candidate
    return None


def _run_git(root: Path, arguments: list[str], *, safe_directory: Path) -> str | None:
    environment = os.environ.copy()
    environment.pop("OPENAI_API_KEY", None)
    environment.pop("CODING_AGENT_API_KEY", None)
    try:
        completed = subprocess.run(
            [
                "git",
                "-c",
                f"safe.directory={safe_directory.as_posix()}",
                "-c",
                "core.quotepath=false",
                "-C",
                str(root),
                *arguments,
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=environment,
            timeout=3,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return completed.stdout if completed.returncode == 0 else None


def _fingerprint(path: Path) -> str | None:
    try:
        if not path.is_file() or path.stat().st_size > _MAX_FINGERPRINT_BYTES:
            return None
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None
