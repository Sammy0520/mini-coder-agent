from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    result: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            result.append(item)
    return result


def codex_metrics(events: Iterable[dict[str, Any]]) -> dict[str, Any]:
    usage: dict[str, int] = {}
    turns = 0
    tool_calls = 0
    tool_ids: set[str] = set()
    thread_id: str | None = None
    for event in events:
        event_type = str(event.get("type") or "")
        if event_type == "thread.started" and event.get("thread_id"):
            thread_id = thread_id or str(event["thread_id"])
        if event_type == "turn.completed":
            turns += 1
            for key, value in (event.get("usage") or {}).items():
                if not isinstance(value, int) or isinstance(value, bool):
                    continue
                normalized = {
                    "cached_input_tokens": "cached_tokens",
                    "reasoning_output_tokens": "reasoning_tokens",
                }.get(str(key), str(key))
                usage[normalized] = usage.get(normalized, 0) + value
        item = event.get("item")
        if not isinstance(item, dict):
            continue
        if item.get("type") in {"command_execution", "file_change", "mcp_tool_call"}:
            item_id = item.get("id")
            if item_id:
                tool_ids.add(str(item_id))
            elif event_type.endswith(".completed"):
                tool_calls += 1
    return {
        **usage,
        "turns": turns,
        "model_calls": None,
        "tool_calls": len(tool_ids) + tool_calls,
        "thread_id": thread_id,
    }


def mini_session_metrics(session: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(session, dict):
        return {}
    usage = {
        str(key): value
        for key, value in (session.get("total_usage") or {}).items()
        if isinstance(value, int) and not isinstance(value, bool)
    }
    return {
        **usage,
        "turns": int(session.get("turn_count", 0)),
        "model_calls": int(session.get("model_call_count", 0)),
        "tool_calls": len(session.get("tool_executions") or []),
        "session_id": session.get("session_id"),
    }


def newest_session(directory: Path) -> dict[str, Any] | None:
    if not directory.is_dir():
        return None
    candidates = sorted(directory.glob("*.json"), key=lambda path: path.stat().st_mtime_ns)
    if not candidates:
        return None
    try:
        data = json.loads(candidates[-1].read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None
