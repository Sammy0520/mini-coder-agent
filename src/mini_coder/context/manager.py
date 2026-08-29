from __future__ import annotations

import copy
import json
from collections.abc import Sequence
from typing import Any


class ContextManager:
    """Build a bounded, protocol-valid view of locally owned conversation history."""

    def __init__(
        self,
        max_chars: int = 80_000,
        summary_chars: int = 2_000,
        max_tokens: int | None = None,
    ) -> None:
        if max_chars < 2_000:
            raise ValueError("max_chars must be at least 2000")
        self.max_chars = max_chars
        self.summary_chars = min(summary_chars, max_chars // 4)
        self.max_tokens = max_tokens or max(1_000, max_chars // 4)

    def estimate_chars(self, messages: Sequence[dict[str, Any]]) -> int:
        return len(json.dumps(list(messages), ensure_ascii=False, default=str))

    def estimate_tokens(self, messages: Sequence[dict[str, Any]]) -> int:
        """Conservative local estimate used only for context budgeting."""
        rendered = json.dumps(list(messages), ensure_ascii=False, default=str)
        wide = sum(1 for char in rendered if ord(char) > 127)
        ascii_chars = len(rendered) - wide
        return wide + (ascii_chars + 3) // 4

    def prepare(
        self,
        messages: Sequence[dict[str, Any]],
        *,
        memory: dict[str, Any] | None = None,
        follow_up: bool = False,
    ) -> list[dict[str, Any]]:
        copied = copy.deepcopy(list(messages))
        if follow_up:
            copied = self._prepare_follow_up(copied, memory or {})
        else:
            copied = self._compact_replayed_provider_state(copied)
            copied = self._compact_old_tool_outputs(copied)
        if self._within_budget(copied):
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

        if not self._within_budget(result):
            result = self._shrink_contents(result)
        return result

    @staticmethod
    def _compact_replayed_provider_state(
        messages: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        tool_assistant_indexes = [
            index
            for index, message in enumerate(messages)
            if message.get("role") == "assistant" and message.get("tool_calls")
        ]
        if len(tool_assistant_indexes) < 2:
            return messages
        latest = tool_assistant_indexes[-1]
        for index in tool_assistant_indexes[:-1]:
            provider_items = messages[index].get("_provider_items")
            if not isinstance(provider_items, list):
                continue
            messages[index]["_provider_items"] = [
                item
                for item in provider_items
                if not isinstance(item, dict) or item.get("type") != "reasoning"
            ]
        return messages

    @staticmethod
    def _compact_old_tool_outputs(
        messages: list[dict[str, Any]],
        limit: int = 3_000,
    ) -> list[dict[str, Any]]:
        latest_tool_request = max(
            (
                index
                for index, message in enumerate(messages)
                if message.get("role") == "assistant" and message.get("tool_calls")
            ),
            default=-1,
        )
        for index, message in enumerate(messages):
            content = message.get("content")
            if (
                index >= latest_tool_request
                or message.get("role") != "tool"
                or not isinstance(content, str)
                or len(content) <= limit
            ):
                continue
            head = limit * 2 // 3
            tail = limit - head
            removed = len(content) - limit
            message["content"] = (
                content[:head]
                + f"\n...[older tool output compacted: {removed} characters omitted]...\n"
                + content[-tail:]
            )
        return messages

    def _within_budget(self, messages: Sequence[dict[str, Any]]) -> bool:
        return (
            self.estimate_chars(messages) <= self.max_chars
            and self.estimate_tokens(messages) <= self.max_tokens
        )

    def _prepare_follow_up(
        self,
        messages: list[dict[str, Any]],
        memory: dict[str, Any],
    ) -> list[dict[str, Any]]:
        """Start a new user turn from compact session facts, not old tool transcripts."""
        prefix: list[dict[str, Any]] = []
        for message in messages:
            if message.get("role") not in {"system", "developer"}:
                break
            prefix.append(message)

        user_indexes = [
            index for index, message in enumerate(messages) if message.get("role") == "user"
        ]
        if not user_indexes:
            return messages
        current_user_index = user_indexes[-1]
        previous_answer = next(
            (
                message
                for message in reversed(messages[:current_user_index])
                if message.get("role") == "assistant"
                and not message.get("tool_calls")
                and str(message.get("content") or "").strip()
            ),
            None,
        )
        memory_text = json.dumps(memory, ensure_ascii=False, default=str, indent=2)
        memory_message = {
            "role": "developer",
            "content": (
                "Session working memory from earlier turns. Treat it as context only; "
                "the latest user message remains the current request.\n" + memory_text
            )[: self.summary_chars * 2],
        }
        result = prefix + [memory_message]
        if previous_answer is not None:
            result.append(previous_answer)
        result.extend(messages[current_user_index:])
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
        for message in result:
            if self._within_budget(result):
                break
            content = message.get("content")
            if not isinstance(content, str) or len(content) < 200:
                continue
            char_overflow = max(0, self.estimate_chars(result) - self.max_chars)
            token_overflow = max(0, self.estimate_tokens(result) - self.max_tokens)
            removable = min(len(content) - 200, max(char_overflow, token_overflow * 3, 100))
            message["content"] = content[: len(content) - removable] + "\n...[context truncated]"
        return result
