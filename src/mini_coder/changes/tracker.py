from __future__ import annotations

import difflib
import hashlib
import os
import stat
import tempfile
import uuid
from pathlib import Path
from typing import Any

from ..exceptions import ChangeConflictError, ChangeError
from ..tools.safety import WorkspacePolicy
from .models import ChangeRecord, PreparedChange, UndoRecord, utc_now

MAX_TRACKED_FILE_BYTES = 2_000_000
DEFAULT_MAX_DIFF_CHARS = 12_000


class ChangeTracker:
    """Prepare, atomically apply, and safely undo workspace text changes."""

    def __init__(
        self,
        workspace: str | Path,
        *,
        max_diff_chars: int = DEFAULT_MAX_DIFF_CHARS,
        max_file_bytes: int = MAX_TRACKED_FILE_BYTES,
    ) -> None:
        self.workspace = Path(workspace).expanduser().resolve()
        self.policy = WorkspacePolicy(self.workspace)
        self.max_diff_chars = max_diff_chars
        self.max_file_bytes = max_file_bytes

    def prepare(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        tool_execution_id: str,
    ) -> PreparedChange:
        if tool_name not in {"write_file", "edit_file"}:
            raise ChangeError(f"unsupported tracked tool: {tool_name}")
        if not isinstance(arguments, dict):
            raise ChangeError("tracked tool arguments must be an object")
        requested = arguments.get("path")
        if not isinstance(requested, str) or not requested:
            raise ChangeError("tracked write path must be a non-empty string")
        self._reject_symlinks(requested)
        path = self.policy.resolve(requested, must_exist=True if tool_name == "edit_file" else None)
        if path.exists() and not path.is_file():
            raise ChangeError(f"tracked path is not a regular file: {requested}")

        before_bytes = path.read_bytes() if path.exists() else None
        if before_bytes is not None and len(before_bytes) > self.max_file_bytes:
            raise ChangeError(
                f"tracked file exceeds {self.max_file_bytes} bytes: {self.policy.display(path)}"
            )
        before_text, encoding, newline = self._decode(before_bytes, path)

        if tool_name == "write_file":
            if not isinstance(arguments.get("content"), str):
                raise ChangeError("write_file content must be a string")
            if path.exists() and not arguments.get("overwrite", False):
                raise ChangeError(
                    "File already exists; use edit_file or explicitly set overwrite=true"
                )
            after_text = _normalize_newlines(arguments["content"], newline)
        else:
            old_text = arguments.get("old_text")
            new_text = arguments.get("new_text")
            if not isinstance(old_text, str) or not isinstance(new_text, str):
                raise ChangeError("edit_file old_text and new_text must be strings")
            if old_text == "":
                raise ChangeError("old_text must not be empty")
            expected = arguments.get("expected_occurrences", 1)
            if not isinstance(expected, int) or isinstance(expected, bool):
                raise ChangeError("expected_occurrences must be an integer")
            expected = max(expected, 1)
            normalized_old = _normalize_newlines(old_text, newline)
            normalized_new = _normalize_newlines(new_text, newline)
            actual = before_text.count(normalized_old)
            if actual != expected:
                raise ChangeError(
                    f"Expected {expected} occurrence(s), found {actual}; file was not changed"
                )
            after_text = before_text.replace(normalized_old, normalized_new, expected)

        after_bytes = _encode(after_text, encoding)
        if len(after_bytes) > self.max_file_bytes:
            raise ChangeError(
                f"result would exceed {self.max_file_bytes} bytes: {self.policy.display(path)}"
            )
        if before_bytes is not None and after_bytes == before_bytes:
            raise ChangeError(f"Requested operation would not change {self.policy.display(path)}")
        before_hash = _hash_bytes(before_bytes) if before_bytes is not None else None
        after_hash = _hash_bytes(after_bytes)
        diff, additions, deletions, truncated = self._diff(
            self.policy.display(path), before_text if before_bytes is not None else None, after_text
        )
        return PreparedChange(
            path=self.policy.display(path),
            tool_execution_id=tool_execution_id,
            tool_name=tool_name,
            before_hash=before_hash,
            after_hash=after_hash,
            before_snapshot=before_text if before_bytes is not None else None,
            after_snapshot=after_text,
            encoding=encoding,
            newline=newline,
            unified_diff=diff,
            additions=additions,
            deletions=deletions,
            diff_truncated=truncated,
        )

    def apply(self, prepared: PreparedChange) -> ChangeRecord:
        path = self._resolve_record_path(prepared.path)
        self._reject_symlinks(prepared.path)
        if path.exists() and not path.is_file():
            raise ChangeConflictError(
                f"File changed after approval: {prepared.path} is no longer a regular file"
            )
        current_bytes = path.read_bytes() if path.exists() else None
        current_hash = _hash_bytes(current_bytes) if current_bytes is not None else None
        if current_hash != prepared.before_hash:
            raise ChangeConflictError(
                f"File changed after approval: {prepared.path}; expected "
                f"{prepared.before_hash or '<missing>'}, found {current_hash or '<missing>'}"
            )
        after_bytes = _encode(prepared.after_snapshot, prepared.encoding)
        if _hash_bytes(after_bytes) != prepared.after_hash:
            raise ChangeError("prepared change after_hash does not match its snapshot")
        _atomic_write_bytes(path, after_bytes)
        return ChangeRecord.from_prepared(prepared)

    def current_hash(self, relative: str) -> str | None:
        path = self._resolve_record_path(relative)
        self._reject_symlinks(relative)
        if not path.exists():
            return None
        if not path.is_file():
            raise ChangeConflictError(f"Tracked path is no longer a regular file: {relative}")
        return _hash_bytes(path.read_bytes())

    def undo_last(
        self,
        changes: list[ChangeRecord],
    ) -> tuple[ChangeRecord, UndoRecord]:
        change = next((item for item in reversed(changes) if item.undo_status == "active"), None)
        if change is None:
            raise ChangeError("session has no active file change to undo")
        return self._undo_change(change)

    def undo_change(
        self,
        changes: list[ChangeRecord],
        change_id: str,
    ) -> tuple[ChangeRecord, UndoRecord]:
        change = next(
            (
                item
                for item in changes
                if item.change_id == change_id and item.undo_status == "active"
            ),
            None,
        )
        if change is None:
            raise ChangeError("tracked file change is not active or does not exist")
        return self._undo_change(change)

    def _undo_change(self, change: ChangeRecord) -> tuple[ChangeRecord, UndoRecord]:
        path = self._resolve_record_path(change.path)
        self._reject_symlinks(change.path)
        if path.exists() and not path.is_file():
            raise ChangeConflictError(
                f"Cannot undo {change.path}: path is no longer a regular file"
            )
        current_bytes = path.read_bytes() if path.exists() else None
        current_hash = _hash_bytes(current_bytes) if current_bytes is not None else None
        if current_hash != change.after_hash:
            raise ChangeConflictError(
                f"Cannot undo {change.path}: file changed after the Agent write; expected "
                f"{change.after_hash}, found {current_hash or '<missing>'}"
            )

        if change.before_hash is None:
            if not path.exists():
                raise ChangeConflictError(f"Cannot undo {change.path}: created file is missing")
            path.unlink()
            restored_hash = None
        else:
            if change.before_snapshot is None:
                raise ChangeError(f"Cannot undo {change.path}: before snapshot is missing")
            restored_bytes = _encode(change.before_snapshot, change.encoding)
            restored_hash = _hash_bytes(restored_bytes)
            if restored_hash != change.before_hash:
                raise ChangeError(f"Cannot undo {change.path}: before snapshot hash is invalid")
            _atomic_write_bytes(path, restored_bytes)

        change.undo_status = "undone"
        change.undone_at = utc_now()
        undo = UndoRecord(
            undo_id=uuid.uuid4().hex,
            change_id=change.change_id,
            path=change.path,
            from_hash=change.after_hash,
            restored_hash=restored_hash,
        )
        return change, undo

    def _resolve_record_path(self, relative: str) -> Path:
        if Path(relative).is_absolute():
            raise ChangeError("tracked change path must be workspace-relative")
        return self.policy.resolve(relative)

    def _reject_symlinks(self, requested: str) -> None:
        raw = Path(requested)
        if raw.is_absolute():
            try:
                raw = raw.relative_to(self.workspace)
            except ValueError as exc:
                raise ChangeError(f"Path escapes the workspace: {requested}") from exc
        current = self.workspace
        for part in raw.parts:
            current = current / part
            if current.is_symlink():
                raise ChangeError(f"Symbolic links are not supported for tracked writes: {requested}")

    def _decode(self, data: bytes | None, path: Path) -> tuple[str, str, str]:
        if data is None:
            return "", "utf-8", "\n"
        if b"\x00" in data[:8192]:
            raise ChangeError(f"Binary files are not supported: {self.policy.display(path)}")
        try:
            if data.startswith(b"\xef\xbb\xbf"):
                text = data.decode("utf-8-sig")
                encoding = "utf-8-sig"
            else:
                text = data.decode("utf-8")
                encoding = "utf-8"
        except UnicodeDecodeError as exc:
            raise ChangeError(
                f"Only UTF-8 text files are supported: {self.policy.display(path)}"
            ) from exc
        return text, encoding, _detect_newline(text)

    def _diff(
        self,
        path: str,
        before: str | None,
        after: str,
    ) -> tuple[str, int, int, bool]:
        from_name = "/dev/null" if before is None else f"a/{path}"
        to_name = f"b/{path}"
        lines = list(
            difflib.unified_diff(
                [] if before is None else before.splitlines(keepends=True),
                after.splitlines(keepends=True),
                fromfile=from_name,
                tofile=to_name,
                lineterm="\n",
            )
        )
        full = "".join(lines)
        if before is None and not full:
            full = f"--- /dev/null\n+++ b/{path}\n"
        additions = sum(1 for line in lines if line.startswith("+") and not line.startswith("+++"))
        deletions = sum(1 for line in lines if line.startswith("-") and not line.startswith("---"))
        if len(full) <= self.max_diff_chars:
            return full, additions, deletions, False
        marker = f"\n... [diff truncated by {len(full) - self.max_diff_chars} characters]\n"
        keep = max(0, self.max_diff_chars - len(marker))
        return full[:keep] + marker, additions, deletions, True


def _hash_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _encode(text: str, encoding: str) -> bytes:
    return text.encode(encoding)


def _detect_newline(text: str) -> str:
    if "\r\n" in text:
        return "\r\n"
    if "\r" in text:
        return "\r"
    return "\n"


def _normalize_newlines(text: str, newline: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n").replace("\n", newline)


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    previous_mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else None
    descriptor, name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if previous_mode is not None:
            os.chmod(temporary, previous_mode)
        os.replace(temporary, path)
    except OSError as exc:
        try:
            os.close(descriptor)
        except OSError:
            pass
        raise ChangeError(f"Atomic write failed for {path.name}: {exc}") from exc
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
