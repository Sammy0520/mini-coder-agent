from __future__ import annotations

import copy
import json
from collections.abc import Sequence
from typing import Any


class ContextManager:
    """Build a bounded, protocol-valid view of locally owned conversation history."""

    def __init__(self, max_chars: int = 80_000, summary_chars: int = 2_000) -> None:
        if max_chars < 2_000:
            raise ValueError("max_chars must be at least 2000")
        self.max_chars = max_chars
        self.summary_chars = min(summary_chars, max_chars // 4)

    def estimate_chars(self, messages: Sequence[dict[str, Any]]) -> int:
        return len(json.dumps(list(messages), ensure_ascii=False, default=str))

    def prepare(self, messages: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
        copied = copy.deepcopy(list(messages))
        if self.estimate_chars(copied) <= self.max_chars:
            return copied

        prefix_end = self._prefix_end(copied)
        prefix = copied[:prefix_end]
        groups = self._group_turns(copied[prefix_end:])
        reserve = self.summary_chars + 300
        selected: list[list[dict[str, Any]]] = []
        current_size = self.estimate_chars(prefix)

        for group in reversed(groups):
            group_size = self.estimate_chars(group)
            if current_size + group_size + reserve > self.max_chars:
                break
            selected.append(group)
            current_size += group_size
        selected.reverse()

        omitted_count = len(groups) - len(selected)
        omitted = groups[:omitted_count]
        summary = self._summarize_groups(omitted)
        result = prefix + ([summary] if omitted else [])
        for group in selected:
            result.extend(group)

        if self.estimate_chars(result) > self.max_chars:
            result = self._shrink_contents(result)
        return result

    @staticmethod
    def _prefix_end(messages: list[dict[str, Any]]) -> int:
        end = 0
        for index, message in enumerate(messages):
            role = message.get("role")
            if role in {"system", "developer"}:
                end = index + 1
                continue
            if role == "user":
                return index + 1
            break
        return end

    @staticmethod
    def _group_turns(messages: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
        groups: list[list[dict[str, Any]]] = []
        index = 0
        while index < len(messages):
            message = messages[index]
            group = [message]
            index += 1
            if message.get("role") == "assistant" and message.get("tool_calls"):
                call_ids = {
                    item.get("id") for item in message.get("tool_calls", []) if item.get("id")
                }
                while index < len(messages):
                    candidate = messages[index]
                    if candidate.get("role") != "tool":
                        break
                    if call_ids and candidate.get("tool_call_id") not in call_ids:
                        break
                    group.append(candidate)
                    index += 1
            groups.append(group)
        return groups

    def _summarize_groups(self, groups: list[list[dict[str, Any]]]) -> dict[str, Any]:
        notes: list[str] = []
        tool_names: list[str] = []
        for group in groups:
            for message in group:
                if message.get("role") == "assistant":
                    for call in message.get("tool_calls", []) or []:
                        name = call.get("function", {}).get("name")
                        if name:
                            tool_names.append(str(name))
                    content = str(message.get("content") or "").strip()
                    if content:
                        notes.append(content[:240])
                elif message.get("role") == "tool":
                    content = str(message.get("content") or "")
                    notes.append(f"tool result: {content[:240]}")
        header = f"Locally compacted {len(groups)} older conversation turn(s)."
        if tool_names:
            header += " Tools used: " + ", ".join(tool_names[:30]) + "."
        body = "\n".join(notes)
        content = (header + ("\n" + body if body else ""))[: self.summary_chars]
        return {"role": "system", "content": content}

    def _shrink_contents(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        result = copy.deepcopy(messages)
        overflow = self.estimate_chars(result) - self.max_chars
        for message in result:
            if overflow <= 0:
                break
            content = message.get("content")
            if not isinstance(content, str) or len(content) < 200:
                continue
            removable = min(len(content) - 200, overflow + 100)
            message["content"] = content[: len(content) - removable] + "\n...[context truncated]"
            overflow = self.estimate_chars(result) - self.max_chars
        return result

