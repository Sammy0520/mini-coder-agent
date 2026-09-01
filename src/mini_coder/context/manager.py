from __future__ import annotations

import copy
import hashlib
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
        soft_limit_ratio: float = 0.8,
        compaction_chunk_batches: int = 4,
        hot_tool_batches: int = 2,
    ) -> None:
        if max_chars < 2_000:
            raise ValueError("max_chars must be at least 2000")
        self.max_chars = max_chars
        self.summary_chars = min(summary_chars, max_chars // 4)
        self.max_tokens = max_tokens or max(1_000, max_chars // 4)
        if not 0.5 <= soft_limit_ratio < 1.0:
            raise ValueError("soft_limit_ratio must be between 0.5 and 1.0")
        if compaction_chunk_batches < 1:
            raise ValueError("compaction_chunk_batches must be positive")
        if hot_tool_batches < 1:
            raise ValueError("hot_tool_batches must be positive")
        self.soft_limit_ratio = soft_limit_ratio
        self.compaction_chunk_batches = compaction_chunk_batches
        self.hot_tool_batches = hot_tool_batches
        self._previous_request_items: list[str] | None = None

    @staticmethod
    def stable_hash(value: Any) -> str:
        rendered = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        return hashlib.sha256(rendered.encode("utf-8")).hexdigest()

    def diagnose_request(
        self,
        messages: Sequence[dict[str, Any]],
        *,
        tool_definitions: Sequence[dict[str, Any]],
        cache_key: str,
        checkpoint_generation: int = 0,
        checkpoint_hash: str | None = None,
        compaction_reason: str = "none",
    ) -> dict[str, Any]:
        """Return content-free cache diagnostics and remember this exact request."""
        copied = list(messages)
        prefix_end = self._prefix_end(copied)
        stable_prefix = copied[:prefix_end]
        hot_tail = copied[prefix_end:]
        current_items = [
            json.dumps(
                item,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            )
            for item in copied
        ]
        previous = self._previous_request_items
        common_items = 0
        common_chars = 0
        if previous is not None:
            for before, current in zip(previous, current_items, strict=False):
                if before == current:
                    common_items += 1
                    common_chars += len(current)
                    continue
                common_chars += _common_prefix_chars(before, current)
                break
        first_changed_index: int | None = None
        first_changed_type: str | None = None
        if previous is not None and (
            common_items < len(previous) or common_items < len(current_items)
        ):
            first_changed_index = common_items
            candidate = (
                copied[first_changed_index]
                if first_changed_index < len(copied)
                else None
            )
            first_changed_type = (
                str(candidate.get("role") or candidate.get("type") or "unknown")
                if isinstance(candidate, dict)
                else "removed"
            )
        self._previous_request_items = current_items
        return {
            "stable_prefix_hash": self.stable_hash(stable_prefix),
            "tool_definitions_hash": self.stable_hash(list(tool_definitions)),
            "cache_key_hash": self.stable_hash(cache_key),
            "checkpoint_generation": checkpoint_generation,
            "checkpoint_hash": checkpoint_hash,
            "stable_prefix_tokens": self.estimate_tokens(stable_prefix),
            "hot_tail_tokens": self.estimate_tokens(hot_tail),
            "estimated_longest_common_prefix_tokens": _estimate_text_tokens(
                common_chars
            ),
            "common_prefix_message_items": common_items,
            "first_changed_input_index": first_changed_index,
            "first_changed_input_type": first_changed_type,
            "compaction_reason": compaction_reason,
        }

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
        # Preserve an append-only hot history while it comfortably fits. Prompt
        # caches match exact prefixes, so rewriting an old tool batch after every
        # model turn saves raw tokens but prevents the provider from reusing most
        # of the preceding request.
        if self._within_soft_budget(copied):
            return copied

        compact_count = self._stable_compaction_count(copied)
        if compact_count:
            copied = self._compact_completed_tool_batches(
                copied,
                compact_count=compact_count,
            )
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

    def _stable_compaction_count(self, messages: list[dict[str, Any]]) -> int:
        """Compact old batches in chunks so the boundary moves infrequently."""
        batch_count = sum(
            1
            for message in messages
            if message.get("role") == "assistant" and message.get("tool_calls")
        )
        available = max(0, batch_count - self.hot_tool_batches)
        return (
            available // self.compaction_chunk_batches
        ) * self.compaction_chunk_batches

    @staticmethod
    def _compact_completed_tool_batches(
        messages: list[dict[str, Any]],
        *,
        compact_count: int,
    ) -> list[dict[str, Any]]:
        """Replace older completed tool batches with small protocol-free summaries.

        Write arguments and read/search results can both contain complete source text.
        Replaying them on every later request is expensive after the model has already
        consumed the immediate result. The latest tool batch stays exact so the function
        calling protocol remains useful for the next response.
        """
        tool_indexes = [
            index
            for index, message in enumerate(messages)
            if message.get("role") == "assistant" and message.get("tool_calls")
        ]
        if compact_count <= 0 or not tool_indexes:
            return messages
        compact_indexes = set(tool_indexes[:compact_count])
        result: list[dict[str, Any]] = []
        index = 0
        while index < len(messages):
            message = messages[index]
            calls = message.get("tool_calls") if message.get("role") == "assistant" else None
            if index not in compact_indexes or not isinstance(calls, list) or not calls:
                result.append(message)
                index += 1
                continue

            call_ids = {
                str(call.get("id"))
                for call in calls
                if isinstance(call, dict) and call.get("id")
            }
            end = index + 1
            tool_results: list[dict[str, Any]] = []
            while end < len(messages):
                candidate = messages[end]
                if candidate.get("role") != "tool":
                    break
                if str(candidate.get("tool_call_id")) not in call_ids:
                    break
                tool_results.append(candidate)
                end += 1
            returned_ids = {
                str(item.get("tool_call_id")) for item in tool_results
            }
            names = [_tool_call_name(call) for call in calls]
            compactable = {
                "read_file",
                "list_files",
                "search_text",
                "write_file",
                "edit_file",
            }
            if (
                not any(name in compactable for name in names)
                or any(name not in compactable for name in names)
                or not call_ids
                or returned_ids != call_ids
            ):
                result.append(message)
                index += 1
                continue

            result.append(
                {
                    "role": "developer",
                    "content": _completed_batch_summary(calls, tool_results),
                }
            )
            index = end
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

    def _within_soft_budget(self, messages: Sequence[dict[str, Any]]) -> bool:
        return (
            self.estimate_chars(messages) <= int(self.max_chars * self.soft_limit_ratio)
            and self.estimate_tokens(messages)
            <= int(self.max_tokens * self.soft_limit_ratio)
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


def _tool_call_name(call: Any) -> str:
    if not isinstance(call, dict):
        return "unknown"
    function = call.get("function")
    if not isinstance(function, dict):
        return "unknown"
    return str(function.get("name") or "unknown")


def _common_prefix_chars(left: str, right: str) -> int:
    limit = min(len(left), len(right))
    index = 0
    while index < limit and left[index] == right[index]:
        index += 1
    return index


def _estimate_text_tokens(char_count: int) -> int:
    return max(0, (char_count + 3) // 4)


def _tool_call_arguments(call: Any) -> dict[str, Any]:
    if not isinstance(call, dict):
        return {}
    function = call.get("function")
    if not isinstance(function, dict):
        return {}
    arguments = function.get("arguments")
    if isinstance(arguments, dict):
        return arguments
    if isinstance(arguments, str):
        try:
            parsed = json.loads(arguments)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _completed_batch_summary(
    calls: list[Any],
    tool_results: list[dict[str, Any]],
) -> str:
    results_by_id = {
        str(item.get("tool_call_id")): item for item in tool_results
    }
    lines = [
        "Local context compression: an earlier completed tool batch is summarized below. "
        "The operations already ran; do not repeat them unless later evidence requires it."
    ]
    for call in calls:
        call_id = str(call.get("id")) if isinstance(call, dict) else ""
        name = _tool_call_name(call)
        arguments = _tool_call_arguments(call)
        target = arguments.get("path") or arguments.get("directory")
        raw_content = str(results_by_id.get(call_id, {}).get("content") or "")
        outcome = "completed"
        try:
            parsed_result = json.loads(raw_content)
        except json.JSONDecodeError:
            parsed_result = None
        if isinstance(parsed_result, dict):
            outcome = "completed" if parsed_result.get("ok") is True else "failed"
            target = target or parsed_result.get("path")
            details = []
            for key, label in (("additions", "+"), ("deletions", "-")):
                value = parsed_result.get(key)
                if isinstance(value, int) and not isinstance(value, bool):
                    details.append(f"{label}{value}")
            detail_text = f" ({'/'.join(details)})" if details else ""
            if name == "read_file":
                start = parsed_result.get("start_line", arguments.get("start_line", 1))
                end = parsed_result.get("end_line")
                total = parsed_result.get("total_lines")
                digest = str(parsed_result.get("content_hash") or "")[:12]
                read_details = []
                if isinstance(start, int) and isinstance(end, int):
                    read_details.append(f"lines {start}-{end}")
                if isinstance(total, int):
                    read_details.append(f"{total} total")
                if digest:
                    read_details.append(f"hash {digest}")
                if read_details:
                    detail_text = " (" + ", ".join(read_details) + ")"
            elif name == "list_files":
                entries = parsed_result.get("entries")
                if isinstance(entries, list):
                    detail_text = f" ({len(entries)} entries)"
            elif name == "search_text":
                matches = parsed_result.get("matches")
                if isinstance(matches, list):
                    reused = parsed_result.get("reused_match_count")
                    match_count = reused if isinstance(reused, int) else len(matches)
                    cache_note = ", cached" if parsed_result.get("cache_hit") is True else ""
                    detail_text = f" ({match_count} matches{cache_note})"
        else:
            detail_text = ""
        rendered_target = f" {target}" if isinstance(target, str) and target else ""
        lines.append(f"- {name}{rendered_target}: {outcome}{detail_text}")
    return "\n".join(lines)
