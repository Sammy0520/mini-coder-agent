from __future__ import annotations

import fnmatch
import re
from pathlib import Path
from typing import Any

from ..exceptions import ToolError
from .base import Tool, ToolContext, ToolResult


class SearchTextTool(Tool):
    name = "search_text"
    description = (
        "Search text or a regular expression across workspace text files. "
        "Prefer this portable tool over shell commands such as rg, grep, or findstr. "
        "Use next_offset when truncated and inspect outcome/filter counts when no match exists."
    )
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "path": {"type": "string", "description": "File or directory, default '.'"},
            "glob": {"type": "string", "description": "File glob, default '*'"},
            "regex": {"type": "boolean"},
            "case_sensitive": {"type": "boolean"},
            "max_results": {"type": "integer", "description": "Maximum matches, up to 500"},
            "offset": {"type": "integer", "description": "Zero-based match offset for pagination"},
        },
        "required": ["query"],
        "additionalProperties": False,
    }

    def execute(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        query = arguments["query"]
        if not query:
            raise ToolError("query must not be empty")
        root = context.policy.resolve(arguments.get("path", "."), must_exist=True)
        glob = arguments.get("glob", "*")
        flags = 0 if arguments.get("case_sensitive", False) else re.IGNORECASE
        expression = query if arguments.get("regex", False) else re.escape(query)
        try:
            pattern = re.compile(expression, flags)
        except re.error as exc:
            raise ToolError(f"Invalid regular expression: {exc}") from exc
        maximum = min(max(arguments.get("max_results", 100), 1), 500)
        offset = min(max(arguments.get("offset", 0), 0), 10_000)
        matches: list[dict[str, Any]] = []
        match_index = 0
        has_more = False
        scanned_files = 0
        filtered_policy = 0
        filtered_glob = 0
        skipped_binary = 0
        skipped_large = 0
        skipped_decode = 0
        scan_truncated = False

        candidates: list[Path] = []
        if root.is_file():
            candidates.append(root)
        else:
            stack = [root]
            visited_entries = 0
            while stack and visited_entries < 10_000:
                directory = stack.pop()
                try:
                    children = sorted(
                        directory.iterdir(),
                        key=lambda item: (item.is_file(), item.name.casefold()),
                        reverse=True,
                    )
                except OSError:
                    skipped_decode += 1
                    continue
                for child in children:
                    visited_entries += 1
                    if visited_entries > 10_000:
                        scan_truncated = True
                        break
                    if context.policy.is_denied(child):
                        filtered_policy += 1
                    elif child.is_dir():
                        stack.append(child)
                    elif child.is_file():
                        candidates.append(child)
            if stack:
                scan_truncated = True
        candidates.sort(key=lambda item: context.policy.display(item).casefold())
        for path in candidates:
            if has_more:
                break
            if not path.is_file():
                continue
            if context.policy.is_denied(path):
                filtered_policy += 1
                continue
            relative = context.policy.display(path)
            if not (fnmatch.fnmatch(path.name, glob) or fnmatch.fnmatch(relative, glob)):
                filtered_glob += 1
                continue
            try:
                if path.stat().st_size > 2_000_000:
                    skipped_large += 1
                    continue
                data = path.read_bytes()
                if b"\x00" in data[:8192]:
                    skipped_binary += 1
                    continue
                text = data.decode("utf-8")
            except (OSError, UnicodeDecodeError):
                skipped_decode += 1
                continue
            scanned_files += 1
            for line_number, line in enumerate(text.splitlines(), start=1):
                if pattern.search(line):
                    if match_index < offset:
                        match_index += 1
                        continue
                    if len(matches) >= maximum:
                        has_more = True
                        break
                    matches.append(
                        {
                            "path": relative,
                            "line": line_number,
                            "text": line[:500],
                            "text_truncated": len(line) > 500,
                        }
                    )
                    match_index += 1

        filtered_total = (
            filtered_policy + filtered_glob + skipped_binary + skipped_large + skipped_decode
        )
        if matches:
            outcome = "matches"
        elif scanned_files == 0 and filtered_total:
            outcome = "all_candidates_filtered"
        elif filtered_total:
            outcome = "no_match_in_scanned_files_with_filtered_candidates"
        else:
            outcome = "no_match"

        return ToolResult(
            True,
            f"Found {len(matches)} match(es) from offset {offset}; outcome={outcome}",
            {
                "matches": matches,
                "offset": offset,
                "next_offset": offset + len(matches) if has_more else None,
                "truncated": has_more,
                "outcome": outcome,
                "scanned_files": scanned_files,
                "filtered": {
                    "policy": filtered_policy,
                    "glob": filtered_glob,
                    "binary": skipped_binary,
                    "large": skipped_large,
                    "decode": skipped_decode,
                },
                "scan_truncated": scan_truncated,
            },
        )
