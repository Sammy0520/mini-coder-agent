from __future__ import annotations

import difflib
import fnmatch
import hashlib
import os
import shutil
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from .models import PatchFile, SubagentError


IGNORED_DIRECTORIES = {
    ".git",
    ".mini-coder",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    ".venv",
    "__pycache__",
    "build",
    "coverage",
    "dist",
    "node_modules",
    "target",
    "vendor",
}
MAX_PATCH_FILE_BYTES = 2_000_000


def _hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _decode(data: bytes, path: str) -> tuple[str, str, str]:
    if b"\x00" in data[:8192]:
        raise SubagentError(f"Subagent changed a binary file: {path}")
    try:
        if data.startswith(b"\xef\xbb\xbf"):
            text = data.decode("utf-8-sig")
            encoding = "utf-8-sig"
        else:
            text = data.decode("utf-8")
            encoding = "utf-8"
    except UnicodeDecodeError as exc:
        raise SubagentError(f"Subagent changed a non-UTF-8 file: {path}") from exc
    newline = "\r\n" if "\r\n" in text else ("\r" if "\r" in text else "\n")
    return text, encoding, newline


def path_is_allowed(path: str, patterns: tuple[str, ...]) -> bool:
    normalized = path.replace("\\", "/").lstrip("./")
    for raw in patterns:
        pattern = raw.replace("\\", "/").lstrip("./")
        if pattern.endswith("/**"):
            prefix = pattern[:-3].rstrip("/")
            if normalized == prefix or normalized.startswith(prefix + "/"):
                return True
        # A bare path is convenient for models and means ownership of that file or
        # directory subtree. A real file cannot also contain descendants, so this
        # remains no broader than the named workspace path.
        if not any(character in pattern for character in "*?["):
            if normalized == pattern or normalized.startswith(pattern.rstrip("/") + "/"):
                return True
        if normalized == pattern or fnmatch.fnmatchcase(normalized, pattern):
            return True
    return False


@dataclass(frozen=True, slots=True)
class _BaseFile:
    content: bytes
    digest: str


class IsolatedWorkspace:
    """A bounded physical workspace copy used by one writable Subagent."""

    def __init__(
        self,
        source: Path,
        destination: Path,
        base_files: dict[str, _BaseFile],
    ) -> None:
        self.source = source
        self.destination = destination
        self.base_files = base_files

    @classmethod
    def create(
        cls,
        source: str | Path,
        destination: str | Path,
        *,
        max_files: int,
        max_bytes: int,
    ) -> "IsolatedWorkspace":
        source_path = Path(source).expanduser().resolve()
        destination_path = Path(destination).expanduser().resolve()
        try:
            destination_path.relative_to(source_path)
        except ValueError:
            pass
        destination_path.mkdir(parents=True, exist_ok=False)
        base_files: dict[str, _BaseFile] = {}
        file_count = 0
        byte_count = 0
        try:
            for root, directories, files in os.walk(source_path):
                directories[:] = [
                    name for name in directories if name not in IGNORED_DIRECTORIES
                ]
                root_path = Path(root)
                relative_root = root_path.relative_to(source_path)
                target_root = destination_path / relative_root
                target_root.mkdir(parents=True, exist_ok=True)
                for name in files:
                    source_file = root_path / name
                    if source_file.is_symlink() or not source_file.is_file():
                        continue
                    relative = source_file.relative_to(source_path).as_posix()
                    data = source_file.read_bytes()
                    file_count += 1
                    byte_count += len(data)
                    if file_count > max_files or byte_count > max_bytes:
                        raise SubagentError(
                            "Workspace is too large for an isolated Implementer copy "
                            f"({file_count} files, {byte_count} bytes)"
                        )
                    target = destination_path / PurePosixPath(relative)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(data)
                    try:
                        shutil.copystat(source_file, target, follow_symlinks=False)
                    except OSError:
                        pass
                    base_files[relative] = _BaseFile(data, _hash(data))
        except Exception:
            shutil.rmtree(destination_path, ignore_errors=True)
            raise
        return cls(source_path, destination_path, base_files)

    def collect_patch(self, allowed_paths: tuple[str, ...]) -> list[PatchFile]:
        current: dict[str, bytes] = {}
        for root, directories, files in os.walk(self.destination):
            directories[:] = [
                name for name in directories if name not in IGNORED_DIRECTORIES
            ]
            root_path = Path(root)
            for name in files:
                path = root_path / name
                if path.is_symlink() or not path.is_file():
                    continue
                relative = path.relative_to(self.destination).as_posix()
                current[relative] = path.read_bytes()

        deleted = sorted(set(self.base_files) - set(current))
        if deleted:
            raise SubagentError(
                "Writable Subagents do not support file deletion yet: " + ", ".join(deleted[:8])
            )

        changed = [
            path
            for path, data in current.items()
            if path not in self.base_files or _hash(data) != self.base_files[path].digest
        ]
        violations = [path for path in changed if not path_is_allowed(path, allowed_paths)]
        if violations:
            raise SubagentError(
                "Subagent changed paths outside its authorization: "
                + ", ".join(sorted(violations)[:12])
            )

        result: list[PatchFile] = []
        for path in sorted(changed):
            after_bytes = current[path]
            if len(after_bytes) > MAX_PATCH_FILE_BYTES:
                raise SubagentError(f"Subagent patch file exceeds 2 MB: {path}")
            before = self.base_files.get(path)
            before_bytes = before.content if before is not None else None
            before_text = None
            if before_bytes is not None:
                before_text, _, _ = _decode(before_bytes, path)
            after_text, encoding, newline = _decode(after_bytes, path)
            diff_lines = list(
                difflib.unified_diff(
                    [] if before_text is None else before_text.splitlines(keepends=True),
                    after_text.splitlines(keepends=True),
                    fromfile="/dev/null" if before_text is None else f"a/{path}",
                    tofile=f"b/{path}",
                    lineterm="\n",
                )
            )
            result.append(
                PatchFile(
                    path=path,
                    before_hash=before.digest if before is not None else None,
                    after_hash=_hash(after_bytes),
                    after_text=after_text,
                    encoding=encoding,
                    newline=newline,
                    unified_diff="".join(diff_lines)[:24_000],
                    additions=sum(
                        line.startswith("+") and not line.startswith("+++")
                        for line in diff_lines
                    ),
                    deletions=sum(
                        line.startswith("-") and not line.startswith("---")
                        for line in diff_lines
                    ),
                )
            )
        return result

    def cleanup(self) -> None:
        """Remove only this coordinator-created workspace copy."""
        try:
            relative = self.destination.relative_to(self.source)
        except ValueError as exc:
            raise SubagentError("Refusing to remove an unbounded Subagent workspace") from exc
        parts = tuple(part.casefold() for part in relative.parts)
        if len(parts) < 5 or parts[:3] != (".mini-coder", "runtime", "subagents"):
            raise SubagentError("Refusing to remove an unexpected Subagent workspace")
        shutil.rmtree(self.destination, ignore_errors=True)
