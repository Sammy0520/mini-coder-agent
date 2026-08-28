from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any

from ..exceptions import ToolError
from .base import RiskLevel, Tool, ToolContext, ToolResult


def _read_text(path: Path) -> str:
    data = path.read_bytes()
    if b"\x00" in data[:8192]:
        raise ToolError(f"Binary files are not supported: {path.name}")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("utf-8-sig")


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        newline="",
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    )
    temp_path = Path(handle.name)
    try:
        with handle:
            handle.write(text)
        os.replace(temp_path, path)
    finally:
        temp_path.unlink(missing_ok=True)


class ListFilesTool(Tool):
    name = "list_files"
    description = (
        "List files and directories inside the workspace. Internal and sensitive paths are "
        "hidden. When truncated, continue with next_offset."
    )
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Workspace-relative directory, default '.'"},
            "max_depth": {"type": "integer", "description": "Maximum recursion depth, 1-6"},
            "max_entries": {"type": "integer", "description": "Maximum returned entries, 1-500"},
            "offset": {"type": "integer", "description": "Zero-based entry offset for pagination"},
        },
        "additionalProperties": False,
    }

    def execute(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        root = context.policy.resolve(arguments.get("path", "."), must_exist=True)
        if not root.is_dir():
            raise ToolError("list_files path must be a directory")
        max_depth = min(max(arguments.get("max_depth", 3), 1), 6)
        max_entries = min(max(arguments.get("max_entries", 200), 1), 500)
        offset = min(max(arguments.get("offset", 0), 0), 10_000)
        entries: list[str] = []
        visible_seen = 0
        filtered_count = 0
        has_more = False

        def visit(directory: Path, depth: int) -> None:
            nonlocal visible_seen, filtered_count, has_more
            if depth > max_depth or has_more:
                return
            for child in sorted(directory.iterdir(), key=lambda item: (item.is_file(), item.name.casefold())):
                if context.policy.is_denied(child):
                    filtered_count += 1
                    continue
                if visible_seen < offset:
                    visible_seen += 1
                    if child.is_dir():
                        visit(child, depth + 1)
                    continue
                if len(entries) >= max_entries:
                    has_more = True
                    return
                suffix = "/" if child.is_dir() else ""
                entries.append(context.policy.display(child) + suffix)
                visible_seen += 1
                if child.is_dir():
                    visit(child, depth + 1)

        visit(root, 1)
        return ToolResult(
            True,
            f"Listed {len(entries)} entries from offset {offset}",
            {
                "entries": entries,
                "offset": offset,
                "next_offset": offset + len(entries) if has_more else None,
                "truncated": has_more,
                "filtered_entries": filtered_count,
            },
        )


class ReadFileTool(Tool):
    name = "read_file"
    description = (
        "Read a UTF-8 text file from the workspace with line numbers and bounded output. "
        "When truncated, continue from next_start_line."
    )
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "start_line": {"type": "integer", "description": "1-based start line"},
            "max_lines": {"type": "integer", "description": "Maximum lines to return, up to 1000"},
        },
        "required": ["path"],
        "additionalProperties": False,
    }

    def execute(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        path = context.policy.resolve(arguments["path"], must_exist=True)
        if not path.is_file():
            raise ToolError("read_file path must be a file")
        start = max(arguments.get("start_line", 1), 1)
        maximum = min(max(arguments.get("max_lines", 400), 1), 1000)
        lines = _read_text(path).splitlines()
        selected = lines[start - 1 : start - 1 + maximum]
        numbered = "\n".join(f"{index:>6} | {line}" for index, line in enumerate(selected, start=start))
        return ToolResult(
            True,
            f"Read {len(selected)} line(s) from {context.policy.display(path)}",
            {
                "content": numbered,
                "start_line": start,
                "end_line": start + len(selected) - 1 if selected else start - 1,
                "total_lines": len(lines),
                "truncated": start - 1 + len(selected) < len(lines),
                "next_start_line": (
                    start + len(selected)
                    if start - 1 + len(selected) < len(lines)
                    else None
                ),
                "file_size_bytes": path.stat().st_size,
            },
        )


class WriteFileTool(Tool):
    name = "write_file"
    description = "Create a UTF-8 text file, or overwrite one only when overwrite=true."
    risk = RiskLevel.WRITE
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "content": {"type": "string"},
            "overwrite": {"type": "boolean"},
        },
        "required": ["path", "content"],
        "additionalProperties": False,
    }

    def execute(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        path = context.policy.resolve(arguments["path"])
        if path.exists() and not arguments.get("overwrite", False):
            raise ToolError("File already exists; use edit_file or explicitly set overwrite=true")
        if path.exists() and not path.is_file():
            raise ToolError("write_file path is not a file")
        _atomic_write(path, arguments["content"])
        return ToolResult(
            True,
            f"Wrote {len(arguments['content'])} characters to {context.policy.display(path)}",
        )


class EditFileTool(Tool):
    name = "edit_file"
    description = (
        "Replace an exact text block in one UTF-8 file. Refuses ambiguous or missing matches. "
        "Prefer this over apply_patch, sed, heredocs, or shell-based file editing."
    )
    risk = RiskLevel.WRITE
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "old_text": {"type": "string"},
            "new_text": {"type": "string"},
            "expected_occurrences": {"type": "integer", "description": "Expected match count, default 1"},
        },
        "required": ["path", "old_text", "new_text"],
        "additionalProperties": False,
    }

    def execute(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        path = context.policy.resolve(arguments["path"], must_exist=True)
        if not path.is_file():
            raise ToolError("edit_file path must be a file")
        old_text = arguments["old_text"]
        if old_text == "":
            raise ToolError("old_text must not be empty")
        expected = max(arguments.get("expected_occurrences", 1), 1)
        original = _read_text(path)
        actual = original.count(old_text)
        if actual != expected:
            raise ToolError(f"Expected {expected} occurrence(s), found {actual}; file was not changed")
        updated = original.replace(old_text, arguments["new_text"], expected)
        _atomic_write(path, updated)
        return ToolResult(
            True,
            f"Replaced {expected} occurrence(s) in {context.policy.display(path)}",
            {"characters_before": len(original), "characters_after": len(updated)},
        )
