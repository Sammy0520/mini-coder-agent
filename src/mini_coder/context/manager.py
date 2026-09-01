from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any


@dataclass(slots=True)
class PreparedContext:
    messages: list[dict[str, Any]]
    state: dict[str, Any]
    checkpoint_generation: int
    checkpoint_hash: str | None
    compaction_reason: str
    checkpoint_created: bool
    estimated_message_tokens: int
    estimated_total_tokens: int


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
        previous_item_hashes: Sequence[str] | None = None,
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
        current_hashes = [
            hashlib.sha256(item.encode("utf-8")).hexdigest()
            for item in current_items
        ]
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
        elif previous_item_hashes is not None:
            for before_hash, current_hash, current in zip(
                previous_item_hashes,
                current_hashes,
                current_items,
                strict=False,
            ):
                if before_hash != current_hash:
                    break
                common_items += 1
                common_chars += len(current)
        first_changed_index: int | None = None
        first_changed_type: str | None = None
        previous_length = (
            len(previous)
            if previous is not None
            else len(previous_item_hashes)
            if previous_item_hashes is not None
            else None
        )
        if previous_length is not None and (
            common_items < previous_length or common_items < len(current_items)
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
            "request_item_hashes": current_hashes,
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

    def prepare_v2(
        self,
        messages: Sequence[dict[str, Any]],
        *,
        state: dict[str, Any] | None,
        durable_state: dict[str, Any] | None = None,
        tool_schema_tokens: int = 0,
        high_watermark_ratio: float = 0.90,
        target_ratio: float = 0.68,
        hot_tool_batches: int = 3,
        min_checkpoint_batches: int = 6,
        new_turn: bool = False,
    ) -> PreparedContext:
        """Build a cache-stable checkpoint + append-only tail request.

        The returned state is JSON-compatible and can be persisted by AgentSession.
        Existing checkpoint bytes are reused exactly until a new generation commits.
        """
        if not 0.5 <= target_ratio < high_watermark_ratio < 1.0:
            raise ValueError("context watermarks must satisfy 0.5 <= target < high < 1")
        if hot_tool_batches < 1 or min_checkpoint_batches < 1:
            raise ValueError("checkpoint batch settings must be positive")
        if tool_schema_tokens < 0:
            raise ValueError("tool_schema_tokens must not be negative")

        canonical = copy.deepcopy(list(messages))
        current_state = _normalize_context_state(state)
        if new_turn:
            current_state = _empty_context_state(boundary_reason="new_turn")
        assembled = self._assemble_v2(canonical, current_state)
        message_tokens = self.estimate_tokens(assembled)
        total_tokens = message_tokens + tool_schema_tokens
        estimated_chars = self.estimate_chars(assembled) + tool_schema_tokens * 4
        high_tokens = int(self.max_tokens * high_watermark_ratio)
        high_chars = int(self.max_chars * high_watermark_ratio)
        hard_exceeded = total_tokens >= self.max_tokens or estimated_chars >= self.max_chars
        reaches_high = total_tokens >= high_tokens or estimated_chars >= high_chars
        if not reaches_high:
            reason = "new_turn" if new_turn else "none"
            return self._prepared_v2(
                assembled,
                current_state,
                reason=reason,
                checkpoint_created=False,
                tool_schema_tokens=tool_schema_tokens,
            )

        batches = _completed_tool_batches(canonical, start=self._prefix_end(canonical))
        previous_count = int(current_state.get("completed_tool_batches", 0) or 0)
        new_batch_count = max(0, len(batches) - previous_count)
        eligible = new_batch_count >= min_checkpoint_batches
        if hard_exceeded and not eligible and current_state.get("generation", 0):
            # Keep the committed checkpoint byte-for-byte stable. A temporarily
            # oversized hot tail may be trimmed, but it must not force a new
            # generation before the configured batch interval.
            checkpoint_index = self._prefix_end(canonical)
            emergency = self._shrink_v2_contents(
                assembled,
                tool_schema_tokens=tool_schema_tokens,
                protected_message_count=checkpoint_index + 1,
            )
            if self._within_v2_budget(emergency, tool_schema_tokens):
                return self._prepared_v2(
                    emergency,
                    current_state,
                    reason="emergency",
                    checkpoint_created=False,
                    tool_schema_tokens=tool_schema_tokens,
                )
        reason = "high_watermark" if eligible else "emergency" if hard_exceeded else "none"
        if reason == "none":
            return self._prepared_v2(
                assembled,
                current_state,
                reason="none",
                checkpoint_created=False,
                tool_schema_tokens=tool_schema_tokens,
            )

        protected = {
            str(item)
            for item in (durable_state or {}).get("protected_tool_call_ids", [])
            if item
        }
        boundary = _checkpoint_boundary(
            batches,
            # Crossing the hard limit is the only case allowed to retire the
            # configured hot tail. Protected failures still cap the boundary.
            hot_tool_batches=0 if hard_exceeded else hot_tool_batches,
            protected_tool_call_ids=protected,
        )
        if boundary is None:
            emergency = (
                self._shrink_v2_contents(
                    assembled,
                    tool_schema_tokens=tool_schema_tokens,
                )
                if hard_exceeded
                else assembled
            )
            return self._prepared_v2(
                emergency,
                current_state,
                reason="emergency" if hard_exceeded else "none",
                checkpoint_created=False,
                tool_schema_tokens=tool_schema_tokens,
            )

        checkpoint = self._build_checkpoint(
            canonical,
            current_state=current_state,
            durable_state=durable_state or {},
            boundary=boundary,
        )
        serialized = _stable_json(checkpoint)
        next_state = {
            "version": 2,
            "generation": checkpoint["generation"],
            "checkpoint": checkpoint,
            "checkpoint_message": {
                "role": "developer",
                "content": (
                    "Local deterministic context checkpoint v2. Earlier operations "
                    "already ran; use these durable facts and the exact hot tail below.\n"
                    + serialized
                ),
            },
            "checkpoint_hash": hashlib.sha256(serialized.encode("utf-8")).hexdigest(),
            "through_message_index": boundary[1],
            "completed_tool_batches": len(batches),
            "boundary_reason": reason,
            "last_request_item_hashes": list(
                current_state.get("last_request_item_hashes", [])
            ),
        }
        candidate = self._assemble_v2(canonical, next_state)
        target_tokens = int(self.max_tokens * target_ratio)
        candidate_total = self.estimate_tokens(candidate) + tool_schema_tokens
        if not self._within_v2_budget(candidate, tool_schema_tokens):
            candidate = self._shrink_v2_contents(
                candidate,
                tool_schema_tokens=tool_schema_tokens,
                protected_message_count=self._prefix_end(canonical) + 1,
            )
            reason = "emergency"
            next_state["boundary_reason"] = reason
        elif candidate_total > target_tokens:
            # Retaining the configured hot batches is more important than forcing
            # an exact target. The high/low gap still prevents immediate rewrites.
            next_state["target_exceeded"] = True
        return self._prepared_v2(
            candidate,
            next_state,
            reason=reason,
            checkpoint_created=True,
            tool_schema_tokens=tool_schema_tokens,
        )

    def _within_v2_budget(
        self,
        messages: list[dict[str, Any]],
        tool_schema_tokens: int,
    ) -> bool:
        return (
            self.estimate_chars(messages) + tool_schema_tokens * 4 <= self.max_chars
            and self.estimate_tokens(messages) + tool_schema_tokens <= self.max_tokens
        )

    def _shrink_v2_contents(
        self,
        messages: list[dict[str, Any]],
        *,
        tool_schema_tokens: int,
        protected_message_count: int = 0,
    ) -> list[dict[str, Any]]:
        result = copy.deepcopy(messages)
        for message in result[protected_message_count:]:
            if self._within_v2_budget(result, tool_schema_tokens):
                break
            content = message.get("content")
            if not isinstance(content, str) or len(content) < 200:
                continue
            char_overflow = max(
                0,
                self.estimate_chars(result)
                + tool_schema_tokens * 4
                - self.max_chars,
            )
            token_overflow = max(
                0,
                self.estimate_tokens(result)
                + tool_schema_tokens
                - self.max_tokens,
            )
            removable = min(
                len(content) - 200,
                max(char_overflow, token_overflow * 3, 100),
            )
            message["content"] = (
                content[: len(content) - removable] + "\n...[context truncated]"
            )
        return result

    def _prepared_v2(
        self,
        messages: list[dict[str, Any]],
        state: dict[str, Any],
        *,
        reason: str,
        checkpoint_created: bool,
        tool_schema_tokens: int,
    ) -> PreparedContext:
        message_tokens = self.estimate_tokens(messages)
        return PreparedContext(
            messages=messages,
            state=copy.deepcopy(state),
            checkpoint_generation=int(state.get("generation", 0) or 0),
            checkpoint_hash=(
                str(state.get("checkpoint_hash"))
                if state.get("checkpoint_hash")
                else None
            ),
            compaction_reason=reason,
            checkpoint_created=checkpoint_created,
            estimated_message_tokens=message_tokens,
            estimated_total_tokens=message_tokens + tool_schema_tokens,
        )

    def _assemble_v2(
        self,
        messages: list[dict[str, Any]],
        state: dict[str, Any],
    ) -> list[dict[str, Any]]:
        checkpoint_message = state.get("checkpoint_message")
        through = state.get("through_message_index")
        if not isinstance(checkpoint_message, dict) or not isinstance(through, int):
            return copy.deepcopy(messages)
        prefix_end = self._prefix_end(messages)
        if through < prefix_end or through >= len(messages):
            return copy.deepcopy(messages)
        return (
            copy.deepcopy(messages[:prefix_end])
            + [copy.deepcopy(checkpoint_message)]
            + copy.deepcopy(messages[through + 1 :])
        )

    def _build_checkpoint(
        self,
        messages: list[dict[str, Any]],
        *,
        current_state: dict[str, Any],
        durable_state: dict[str, Any],
        boundary: tuple[int, int, list[dict[str, Any]], list[dict[str, Any]]],
    ) -> dict[str, Any]:
        _, end, _, _ = boundary
        previous = current_state.get("checkpoint")
        previous_operations = (
            list(previous.get("operations", [])) if isinstance(previous, dict) else []
        )
        old_through = int(current_state.get("through_message_index", -1) or -1)
        retired = _completed_tool_batches(messages, start=max(0, old_through + 1))
        operations = previous_operations + [
            operation
            for batch_start, batch_end, calls, results in retired
            if batch_start > old_through and batch_end <= end
            for operation in _durable_batch_summary(calls, results)
        ]
        operations = operations[-60:]
        last_call_id = next(
            (
                str(call.get("id"))
                for call in reversed(boundary[2])
                if isinstance(call, dict) and call.get("id")
            ),
            None,
        )
        execution_ids = durable_state.get("tool_execution_ids_by_call_id")
        through_execution_id = (
            execution_ids.get(last_call_id)
            if isinstance(execution_ids, dict) and last_call_id is not None
            else None
        )
        checkpoint = {
            "version": 2,
            "generation": int(current_state.get("generation", 0) or 0) + 1,
            "through_execution_id": through_execution_id,
            "change_revision": int(durable_state.get("change_revision", 0) or 0),
            "goal": str(durable_state.get("goal") or "")[:1_200],
            "requirements": _stable_string_list(
                durable_state.get("requirements"), limit=12
            ),
            "decisions": _stable_string_list(durable_state.get("decisions"), limit=20),
            "relevant_files": _stable_string_list(
                durable_state.get("relevant_files"), limit=40
            ),
            "changed_files": _stable_string_list(
                durable_state.get("changed_files"), limit=40
            ),
            "changes": _stable_object_list(durable_state.get("changes"), limit=40),
            "verification": _stable_object_list(
                durable_state.get("verification"), limit=20
            ),
            "active_blockers": _stable_object_list(
                durable_state.get("active_blockers"), limit=12
            ),
            "applied_subagent_bundles": _stable_object_list(
                durable_state.get("applied_subagent_bundles"), limit=12
            ),
            "operations": operations,
        }
        return checkpoint

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


def _stable_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def _empty_context_state(*, boundary_reason: str = "none") -> dict[str, Any]:
    return {
        "version": 2,
        "generation": 0,
        "checkpoint": None,
        "checkpoint_message": None,
        "checkpoint_hash": None,
        "through_message_index": None,
        "completed_tool_batches": 0,
        "boundary_reason": boundary_reason,
    }


def _normalize_context_state(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or value.get("version") != 2:
        return _empty_context_state()
    generation = value.get("generation")
    through = value.get("through_message_index")
    batches = value.get("completed_tool_batches")
    if not isinstance(generation, int) or generation < 0:
        return _empty_context_state()
    if through is not None and (not isinstance(through, int) or through < 0):
        return _empty_context_state()
    if not isinstance(batches, int) or batches < 0:
        return _empty_context_state()
    return copy.deepcopy(value)


def _completed_tool_batches(
    messages: list[dict[str, Any]],
    *,
    start: int,
) -> list[tuple[int, int, list[dict[str, Any]], list[dict[str, Any]]]]:
    batches: list[tuple[int, int, list[dict[str, Any]], list[dict[str, Any]]]] = []
    index = max(0, start)
    while index < len(messages):
        message = messages[index]
        calls = message.get("tool_calls") if message.get("role") == "assistant" else None
        if not isinstance(calls, list) or not calls:
            index += 1
            continue
        call_ids = {
            str(call.get("id"))
            for call in calls
            if isinstance(call, dict) and call.get("id")
        }
        if not call_ids:
            index += 1
            continue
        results: list[dict[str, Any]] = []
        end = index + 1
        while end < len(messages) and messages[end].get("role") == "tool":
            candidate = messages[end]
            if str(candidate.get("tool_call_id")) not in call_ids:
                break
            results.append(candidate)
            end += 1
        returned = {str(item.get("tool_call_id")) for item in results}
        if returned == call_ids:
            batches.append((index, end - 1, calls, results))
            index = end
            continue
        index += 1
    return batches


def _checkpoint_boundary(
    batches: list[tuple[int, int, list[dict[str, Any]], list[dict[str, Any]]]],
    *,
    hot_tool_batches: int,
    protected_tool_call_ids: set[str],
) -> tuple[int, int, list[dict[str, Any]], list[dict[str, Any]]] | None:
    eligible_count = len(batches) - hot_tool_batches
    if eligible_count <= 0:
        return None
    protected_batch_indexes = [
        index
        for index, (_, _, calls, _) in enumerate(batches)
        if any(
            str(call.get("id")) in protected_tool_call_ids
            for call in calls
            if isinstance(call, dict)
        )
    ]
    if protected_batch_indexes:
        eligible_count = min(eligible_count, min(protected_batch_indexes))
    if eligible_count <= 0:
        return None
    return batches[eligible_count - 1]


def _stable_string_list(value: Any, *, limit: int) -> list[str]:
    if not isinstance(value, list):
        return []
    result: list[str] = []
    for item in value:
        text = " ".join(str(item).strip().split())[:500]
        if text and text not in result:
            result.append(text)
    return result[-limit:]


def _stable_object_list(value: Any, *, limit: int) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, dict):
            continue
        normalized = json.loads(_stable_json(item))
        fingerprint = _stable_json(normalized)
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        result.append(normalized)
    return result[-limit:]


def _durable_batch_summary(
    calls: list[dict[str, Any]],
    results: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    results_by_id = {str(item.get("tool_call_id")): item for item in results}
    operations: list[dict[str, Any]] = []
    for call in calls:
        call_id = str(call.get("id") or "")
        name = _tool_call_name(call)
        arguments = _tool_call_arguments(call)
        raw = str(results_by_id.get(call_id, {}).get("content") or "")
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            parsed = {}
        operation: dict[str, Any] = {
            "tool": name,
            "target": arguments.get("path") or arguments.get("directory"),
            "ok": parsed.get("ok") is True if isinstance(parsed, dict) else False,
        }
        if isinstance(parsed, dict):
            for key in (
                "start_line",
                "end_line",
                "total_lines",
                "content_hash",
                "additions",
                "deletions",
                "exit_code",
                "expected_exit_codes",
                "expectation_met",
                "verification_mode",
                "environment_error",
                "duration_seconds",
                "timed_out",
                "output_truncated",
            ):
                if key in parsed:
                    operation[key] = parsed[key]
            entries = parsed.get("entries")
            matches = parsed.get("matches")
            if isinstance(entries, list):
                operation["entry_count"] = len(entries)
            if isinstance(matches, list):
                operation["match_count"] = len(matches)
        operations.append(operation)
    return operations


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
