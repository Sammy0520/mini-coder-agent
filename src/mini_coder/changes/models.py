from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from ..exceptions import ChangeError

_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def validate_change_id(value: str) -> str:
    if not isinstance(value, str) or not _ID_PATTERN.fullmatch(value):
        raise ChangeError(
            "change identifiers must start with an ASCII letter or digit and contain only "
            "letters, digits, underscores, or hyphens"
        )
    return value


@dataclass(slots=True)
class PreparedChange:
    path: str
    tool_execution_id: str
    tool_name: str
    before_hash: str | None
    after_hash: str
    before_snapshot: str | None
    after_snapshot: str
    encoding: str
    newline: str
    unified_diff: str
    additions: int
    deletions: int
    diff_truncated: bool

    def __post_init__(self) -> None:
        if not self.path:
            raise ChangeError("change path must not be empty")
        validate_change_id(self.tool_execution_id)
        if self.tool_name not in {"write_file", "edit_file"}:
            raise ChangeError(f"unsupported tracked tool: {self.tool_name}")
        if self.before_hash is not None and not _is_sha256(self.before_hash):
            raise ChangeError("before_hash must be a SHA-256 hex digest or null")
        if not _is_sha256(self.after_hash):
            raise ChangeError("after_hash must be a SHA-256 hex digest")
        if self.encoding not in {"utf-8", "utf-8-sig"}:
            raise ChangeError(f"unsupported text encoding: {self.encoding}")
        if self.newline not in {"\n", "\r\n", "\r"}:
            raise ChangeError("newline must be LF, CRLF, or CR")
        if self.additions < 0 or self.deletions < 0:
            raise ChangeError("diff statistics must not be negative")

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "tool_execution_id": self.tool_execution_id,
            "tool_name": self.tool_name,
            "before_hash": self.before_hash,
            "after_hash": self.after_hash,
            "before_snapshot": self.before_snapshot,
            "after_snapshot": self.after_snapshot,
            "encoding": self.encoding,
            "newline": self.newline,
            "unified_diff": self.unified_diff,
            "additions": self.additions,
            "deletions": self.deletions,
            "diff_truncated": self.diff_truncated,
        }

    @classmethod
    def from_dict(cls, data: Any) -> "PreparedChange":
        if not isinstance(data, dict):
            raise ChangeError("prepared change must be a JSON object")
        return cls(
            path=_required_string(data, "path"),
            tool_execution_id=_required_string(data, "tool_execution_id"),
            tool_name=_required_string(data, "tool_name"),
            before_hash=_optional_string(data, "before_hash"),
            after_hash=_required_string(data, "after_hash"),
            before_snapshot=_optional_string(data, "before_snapshot"),
            after_snapshot=_required_string(data, "after_snapshot", allow_empty=True),
            encoding=_required_string(data, "encoding"),
            newline=_required_string(data, "newline"),
            unified_diff=_required_string(data, "unified_diff", allow_empty=True),
            additions=_required_int(data, "additions"),
            deletions=_required_int(data, "deletions"),
            diff_truncated=_required_bool(data, "diff_truncated"),
        )


@dataclass(slots=True)
class ChangeRecord:
    change_id: str
    path: str
    tool_execution_id: str
    tool_name: str
    before_hash: str | None
    after_hash: str
    before_snapshot: str | None
    encoding: str
    newline: str
    unified_diff: str
    additions: int
    deletions: int
    diff_truncated: bool
    created_at: str = field(default_factory=utc_now)
    undo_status: str = "active"
    undone_at: str | None = None

    def __post_init__(self) -> None:
        validate_change_id(self.change_id)
        PreparedChange(
            path=self.path,
            tool_execution_id=self.tool_execution_id,
            tool_name=self.tool_name,
            before_hash=self.before_hash,
            after_hash=self.after_hash,
            before_snapshot=self.before_snapshot,
            after_snapshot="",
            encoding=self.encoding,
            newline=self.newline,
            unified_diff=self.unified_diff,
            additions=self.additions,
            deletions=self.deletions,
            diff_truncated=self.diff_truncated,
        )
        if self.undo_status not in {"active", "undone"}:
            raise ChangeError(f"unsupported undo status: {self.undo_status}")
        if self.undo_status == "undone" and self.undone_at is None:
            raise ChangeError("undone change must include undone_at")

    @classmethod
    def from_prepared(cls, prepared: PreparedChange) -> "ChangeRecord":
        return cls(
            change_id=uuid.uuid4().hex,
            path=prepared.path,
            tool_execution_id=prepared.tool_execution_id,
            tool_name=prepared.tool_name,
            before_hash=prepared.before_hash,
            after_hash=prepared.after_hash,
            before_snapshot=prepared.before_snapshot,
            encoding=prepared.encoding,
            newline=prepared.newline,
            unified_diff=prepared.unified_diff,
            additions=prepared.additions,
            deletions=prepared.deletions,
            diff_truncated=prepared.diff_truncated,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "change_id": self.change_id,
            "path": self.path,
            "tool_execution_id": self.tool_execution_id,
            "tool_name": self.tool_name,
            "before_hash": self.before_hash,
            "after_hash": self.after_hash,
            "before_snapshot": self.before_snapshot,
            "encoding": self.encoding,
            "newline": self.newline,
            "unified_diff": self.unified_diff,
            "additions": self.additions,
            "deletions": self.deletions,
            "diff_truncated": self.diff_truncated,
            "created_at": self.created_at,
            "undo_status": self.undo_status,
            "undone_at": self.undone_at,
        }

    @classmethod
    def from_dict(cls, data: Any) -> "ChangeRecord":
        if not isinstance(data, dict):
            raise ChangeError("change record must be a JSON object")
        return cls(
            change_id=_required_string(data, "change_id"),
            path=_required_string(data, "path"),
            tool_execution_id=_required_string(data, "tool_execution_id"),
            tool_name=_required_string(data, "tool_name"),
            before_hash=_optional_string(data, "before_hash"),
            after_hash=_required_string(data, "after_hash"),
            before_snapshot=_optional_string(data, "before_snapshot"),
            encoding=_required_string(data, "encoding"),
            newline=_required_string(data, "newline"),
            unified_diff=_required_string(data, "unified_diff", allow_empty=True),
            additions=_required_int(data, "additions"),
            deletions=_required_int(data, "deletions"),
            diff_truncated=_required_bool(data, "diff_truncated"),
            created_at=_required_string(data, "created_at"),
            undo_status=_required_string(data, "undo_status"),
            undone_at=_optional_string(data, "undone_at"),
        )


@dataclass(slots=True)
class UndoRecord:
    undo_id: str
    change_id: str
    path: str
    from_hash: str
    restored_hash: str | None
    created_at: str = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        validate_change_id(self.undo_id)
        validate_change_id(self.change_id)
        if not self.path:
            raise ChangeError("undo path must not be empty")
        if not _is_sha256(self.from_hash):
            raise ChangeError("undo from_hash must be a SHA-256 hex digest")
        if self.restored_hash is not None and not _is_sha256(self.restored_hash):
            raise ChangeError("undo restored_hash must be a SHA-256 hex digest or null")

    def to_dict(self) -> dict[str, Any]:
        return {
            "undo_id": self.undo_id,
            "change_id": self.change_id,
            "path": self.path,
            "from_hash": self.from_hash,
            "restored_hash": self.restored_hash,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: Any) -> "UndoRecord":
        if not isinstance(data, dict):
            raise ChangeError("undo record must be a JSON object")
        return cls(
            undo_id=_required_string(data, "undo_id"),
            change_id=_required_string(data, "change_id"),
            path=_required_string(data, "path"),
            from_hash=_required_string(data, "from_hash"),
            restored_hash=_optional_string(data, "restored_hash"),
            created_at=_required_string(data, "created_at"),
        )


def _is_sha256(value: str) -> bool:
    return len(value) == 64 and all(character in "0123456789abcdef" for character in value)


def _required_string(data: dict[str, Any], name: str, *, allow_empty: bool = False) -> str:
    value = data.get(name)
    if not isinstance(value, str) or (not allow_empty and not value):
        suffix = "a string" if allow_empty else "a non-empty string"
        raise ChangeError(f"change field {name!r} must be {suffix}")
    return value


def _optional_string(data: dict[str, Any], name: str) -> str | None:
    value = data.get(name)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ChangeError(f"change field {name!r} must be a string or null")
    return value


def _required_int(data: dict[str, Any], name: str) -> int:
    value = data.get(name)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ChangeError(f"change field {name!r} must be a non-negative integer")
    return value


def _required_bool(data: dict[str, Any], name: str) -> bool:
    value = data.get(name)
    if not isinstance(value, bool):
        raise ChangeError(f"change field {name!r} must be a boolean")
    return value
