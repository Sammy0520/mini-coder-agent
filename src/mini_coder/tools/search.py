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
        "Prefer this portable tool over shell commands such as rg, grep, or findstr."
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
        matches: list[dict[str, Any]] = []

        candidates = [root] if root.is_file() else root.rglob("*")
        for path in candidates:
            if len(matches) >= maximum:
                break
            if not path.is_file() or context.policy.is_denied(path):
                continue
            relative = context.policy.display(path)
            if not (fnmatch.fnmatch(path.name, glob) or fnmatch.fnmatch(relative, glob)):
                continue
            try:
                data = path.read_bytes()
                if b"\x00" in data[:8192]:
                    continue
                text = data.decode("utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            for line_number, line in enumerate(text.splitlines(), start=1):
                if pattern.search(line):
                    matches.append(
                        {"path": relative, "line": line_number, "text": line[:500]}
                    )
                    if len(matches) >= maximum:
                        break

        return ToolResult(
            True,
            f"Found {len(matches)} match(es)",
            {"matches": matches, "truncated": len(matches) >= maximum},
        )
