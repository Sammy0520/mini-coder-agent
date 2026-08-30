from __future__ import annotations

import fnmatch
import json
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
        "Use next_offset when truncated and inspect outcome/filter counts when no match exists. "
        "Equivalent unchanged searches may return a cache summary instead of repeated matches."
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
        cache_key = _search_cache_key(
            root=root,
            query=query,
            glob=glob,
            regex=bool(arguments.get("regex", False)),
            case_sensitive=bool(arguments.get("case_sensitive", False)),
            revision=context.observation_revision,
        )
        cached = _reuse_search(context.search_cache.get(cache_key, []), offset, maximum)
        if cached is not None:
            replay, exact = cached
            return ToolResult(
                True,
                (
                    "Reused the earlier equivalent search; workspace observations "
                    "have not been invalidated"
                ),
                {
                    **replay,
                    "cache_hit": True,
                    "cache_replay": "summary" if exact else "covered_subset",
                    "observation_revision": context.observation_revision,
                },
            )
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

        data = {
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
            "cache_hit": False,
            "observation_revision": context.observation_revision,
        }
        if cache_key not in context.search_cache and len(context.search_cache) >= 32:
            context.search_cache.pop(next(iter(context.search_cache)))
        pages = context.search_cache.setdefault(cache_key, [])
        pages.append({"offset": offset, "maximum": maximum, "data": data})
        del pages[:-8]
        return ToolResult(
            True,
            f"Found {len(matches)} match(es) from offset {offset}; outcome={outcome}",
            data,
        )


def _search_cache_key(
    *,
    root: Path,
    query: str,
    glob: str,
    regex: bool,
    case_sensitive: bool,
    revision: int,
) -> str:
    normalized_query = query
    if not regex and not case_sensitive:
        normalized_query = query.casefold()
    return json.dumps(
        [
            str(root.resolve()),
            normalized_query,
            glob,
            regex,
            case_sensitive,
            revision,
        ],
        ensure_ascii=False,
    )


def _reuse_search(
    pages: list[dict[str, Any]],
    offset: int,
    maximum: int,
) -> tuple[dict[str, Any], bool] | None:
    for page in reversed(pages):
        data = page.get("data")
        if not isinstance(data, dict):
            continue
        page_offset = page.get("offset")
        page_maximum = page.get("maximum")
        matches = data.get("matches")
        if (
            not isinstance(page_offset, int)
            or not isinstance(page_maximum, int)
            or not isinstance(matches, list)
            or offset < page_offset
        ):
            continue
        available_end = page_offset + len(matches)
        requested_end = offset + maximum
        exhausted = not bool(data.get("truncated", False))
        if requested_end > available_end and not exhausted:
            continue
        start_index = offset - page_offset
        subset = matches[start_index : start_index + maximum]
        exact = offset == page_offset and maximum == page_maximum
        replay = {
            name: value
            for name, value in data.items()
            if name not in {"matches", "cache_hit"}
        }
        if exact:
            replay["matches"] = []
            replay["reused_match_count"] = len(matches)
        else:
            replay["matches"] = subset
        has_more = (
            offset + len(subset) < available_end
            or (bool(data.get("truncated", False)) and offset + len(subset) >= available_end)
        )
        replay["offset"] = offset
        replay["truncated"] = has_more
        replay["next_offset"] = offset + len(subset) if has_more else None
        return replay, exact
    return None
