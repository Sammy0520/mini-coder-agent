from __future__ import annotations

import hashlib
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
    parallel_safe = True
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
    parallel_safe = True
    name = "read_file"
    description = (
        "Read a UTF-8 text file from the workspace with line numbers and bounded output. "
        "When truncated, continue from next_start_line. Previously covered unchanged ranges "
        "may return only unseen lines or a cache summary."
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
        raw = path.read_bytes()
        if b"\x00" in raw[:8192]:
            raise ToolError(f"Binary files are not supported: {path.name}")
        content_hash = hashlib.sha256(raw).hexdigest()
        cache_key = str(path.resolve())
        cached_ranges = context.read_cache.get(cache_key, [])
        if cached_ranges and cached_ranges[0].get("content_hash") != content_hash:
            cached_ranges = []
            context.read_cache.pop(cache_key, None)
        cached_total = next(
            (
                item.get("total_lines")
                for item in cached_ranges
                if isinstance(item.get("total_lines"), int)
            ),
            None,
        )
        if isinstance(cached_total, int):
            requested_end = min(cached_total, start + maximum - 1)
            covered = _covered_ranges(cached_ranges, start, requested_end)
            missing = _missing_ranges(start, requested_end, covered)
        else:
            requested_end = start + maximum - 1
            covered = []
            missing = [(start, requested_end)]
        if not missing:
            exemplar = cached_ranges[-1]
            return ToolResult(
                True,
                (
                    f"Reused the earlier read of {context.policy.display(path)}; "
                    "the requested line range is already covered and unchanged"
                ),
                {
                    "content": "",
                    "start_line": start,
                    "end_line": requested_end,
                    "requested_start_line": start,
                    "requested_end_line": requested_end,
                    "returned_ranges": [],
                    "covered_ranges": covered,
                    "total_lines": cached_total,
                    "truncated": requested_end < cached_total,
                    "next_start_line": requested_end + 1 if requested_end < cached_total else None,
                    "file_size_bytes": exemplar.get("file_size_bytes", len(raw)),
                    "content_hash": content_hash,
                    "cache_hit": True,
                    "partial_cache_hit": False,
                    "content_unchanged": True,
                },
            )
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise ToolError("Only UTF-8 text files are supported") from exc
        lines = text.splitlines()
        actual_end = min(len(lines), start + maximum - 1)
        covered = _covered_ranges(cached_ranges, start, actual_end)
        missing = _missing_ranges(start, actual_end, covered)
        returned_lines = [
            (line_number, lines[line_number - 1])
            for first, last in missing
            for line_number in range(first, last + 1)
        ]
        numbered = "\n".join(
            f"{line_number:>6} | {line}"
            for line_number, line in returned_lines
        )
        partial_hit = bool(covered and missing)
        data = {
            "content": numbered,
            "start_line": returned_lines[0][0] if returned_lines else start,
            "end_line": returned_lines[-1][0] if returned_lines else start - 1,
            "requested_start_line": start,
            "requested_end_line": actual_end,
            "returned_ranges": missing,
            "covered_ranges": covered,
            "total_lines": len(lines),
            "truncated": actual_end < len(lines),
            "next_start_line": (
                actual_end + 1
                if actual_end < len(lines)
                else None
            ),
            "file_size_bytes": len(raw),
            "content_hash": content_hash,
            "cache_hit": partial_hit,
            "partial_cache_hit": partial_hit,
        }
        if cache_key not in context.read_cache and len(context.read_cache) >= 32:
            context.read_cache.pop(next(iter(context.read_cache)))
        context.read_cache[cache_key] = _merge_cached_range(
            cached_ranges,
            {
                "start_line": start,
                "end_line": actual_end,
                "total_lines": len(lines),
                "file_size_bytes": len(raw),
                "content_hash": content_hash,
            },
        )
        return ToolResult(
            True,
            (
                f"Read {len(returned_lines)} new line(s) from {context.policy.display(path)}"
                + (f"; reused {sum(last - first + 1 for first, last in covered)} covered line(s)" if partial_hit else "")
            ),
            data,
        )


def _covered_ranges(
    cached: list[dict[str, Any]],
    start: int,
    end: int,
) -> list[tuple[int, int]]:
    if end < start:
        return []
    ranges = sorted(
        (
            max(start, int(item.get("start_line", start))),
            min(end, int(item.get("end_line", end))),
        )
        for item in cached
        if isinstance(item.get("start_line"), int)
        and isinstance(item.get("end_line"), int)
        and int(item["end_line"]) >= start
        and int(item["start_line"]) <= end
    )
    merged: list[tuple[int, int]] = []
    for first, last in ranges:
        if first > last:
            continue
        if merged and first <= merged[-1][1] + 1:
            merged[-1] = (merged[-1][0], max(merged[-1][1], last))
        else:
            merged.append((first, last))
    return merged


def _missing_ranges(
    start: int,
    end: int,
    covered: list[tuple[int, int]],
) -> list[tuple[int, int]]:
    if end < start:
        return []
    missing: list[tuple[int, int]] = []
    cursor = start
    for first, last in covered:
        if cursor < first:
            missing.append((cursor, first - 1))
        cursor = max(cursor, last + 1)
    if cursor <= end:
        missing.append((cursor, end))
    return missing


def _merge_cached_range(
    cached: list[dict[str, Any]],
    new_range: dict[str, Any],
) -> list[dict[str, Any]]:
    combined = [*cached, new_range]
    combined.sort(key=lambda item: int(item.get("start_line", 1)))
    merged: list[dict[str, Any]] = []
    for item in combined:
        if (
            merged
            and item.get("content_hash") == merged[-1].get("content_hash")
            and int(item.get("start_line", 1)) <= int(merged[-1].get("end_line", 0)) + 1
        ):
            merged[-1]["end_line"] = max(
                int(merged[-1]["end_line"]),
                int(item.get("end_line", 0)),
            )
            continue
        merged.append(dict(item))
    return merged[-16:]


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
        context.invalidate_observations()
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
        context.invalidate_observations()
        return ToolResult(
            True,
            f"Replaced {expected} occurrence(s) in {context.policy.display(path)}",
            {"characters_before": len(original), "characters_after": len(updated)},
        )
