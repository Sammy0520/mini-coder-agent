from __future__ import annotations

import unittest

from mini_coder.context import ContextManager


class ContextManagerTests(unittest.TestCase):
    @staticmethod
    def _v2_messages(batch_count: int, *, output_chars: int = 700) -> list[dict]:
        messages: list[dict] = [
            {"role": "system", "content": "stable system"},
            {"role": "developer", "content": "stable workspace"},
            {"role": "user", "content": "build the feature"},
        ]
        for index in range(batch_count):
            call_id = f"call-{index}"
            messages.extend(
                [
                    {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [
                            {
                                "id": call_id,
                                "type": "function",
                                "function": {
                                    "name": "read_file",
                                    "arguments": '{"path":"app.py"}',
                                },
                            }
                        ],
                    },
                    {
                        "role": "tool",
                        "tool_call_id": call_id,
                        "content": (
                            '{"ok":true,"content":"'
                            + (chr(65 + index % 26) * output_chars)
                            + '","content_hash":"hash-'
                            + str(index)
                            + '"}'
                        ),
                    },
                ]
            )
        return messages

    def test_cache_diagnostics_are_content_free_and_track_append_only_prefix(self) -> None:
        manager = ContextManager(max_chars=20_000, max_tokens=5_000)
        messages = [
            {"role": "system", "content": "stable-secret-looking-prompt"},
            {"role": "user", "content": "task"},
        ]
        tools = [{"type": "function", "name": "read_file"}]

        first = manager.diagnose_request(
            messages,
            tool_definitions=tools,
            cache_key="private-cache-key",
        )
        second = manager.diagnose_request(
            messages + [{"role": "assistant", "content": "next"}],
            tool_definitions=tools,
            cache_key="private-cache-key",
        )

        rendered = str(second)
        self.assertNotIn("stable-secret-looking-prompt", rendered)
        self.assertNotIn("private-cache-key", rendered)
        self.assertEqual(first["stable_prefix_hash"], second["stable_prefix_hash"])
        self.assertEqual(first["tool_definitions_hash"], second["tool_definitions_hash"])
        self.assertEqual(first["cache_key_hash"], second["cache_key_hash"])
        self.assertEqual(second["common_prefix_message_items"], 2)
        self.assertEqual(second["first_changed_input_index"], 2)
        self.assertEqual(second["first_changed_input_type"], "assistant")
        self.assertGreater(second["estimated_longest_common_prefix_tokens"], 0)

    def test_compaction_keeps_tool_call_pairs_valid(self) -> None:
        messages = [
            {"role": "system", "content": "system"},
            {"role": "user", "content": "task"},
        ]
        for index in range(8):
            call_id = f"call-{index}"
            messages.extend(
                [
                    {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [
                            {
                                "id": call_id,
                                "type": "function",
                                "function": {"name": "read_file", "arguments": "{}"},
                            }
                        ],
                    },
                    {"role": "tool", "tool_call_id": call_id, "content": "x" * 600},
                ]
            )

        manager = ContextManager(max_chars=2_400, summary_chars=400)
        prepared = manager.prepare(messages)

        self.assertLessEqual(manager.estimate_chars(prepared), 2_400)
        self.assertTrue(any("Locally compacted" in item.get("content", "") for item in prepared))

    def test_follow_up_uses_session_memory_instead_of_old_tool_transcript(self) -> None:
        manager = ContextManager(max_chars=20_000, max_tokens=5_000)
        messages = [
            {"role": "system", "content": "system"},
            {"role": "developer", "content": "workspace"},
            {"role": "user", "content": "first task"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call-1",
                        "type": "function",
                        "function": {"name": "read_file", "arguments": "{}"},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call-1", "content": "old output " * 500},
            {"role": "assistant", "content": "First task is complete."},
            {"role": "user", "content": "now add another feature"},
        ]

        prepared = manager.prepare(
            messages,
            memory={"active_changed_files": ["app.py"], "last_outcome": "done"},
            follow_up=True,
        )

        rendered = str(prepared)
        self.assertIn("Session working memory", rendered)
        self.assertIn("app.py", rendered)
        self.assertIn("First task is complete", rendered)
        self.assertIn("now add another feature", rendered)
        self.assertNotIn("old output", rendered)
        self.assertLess(manager.estimate_tokens(prepared), manager.estimate_tokens(messages))

    def test_hot_history_is_preserved_while_below_soft_budget(self) -> None:
        manager = ContextManager(max_chars=50_000, max_tokens=20_000)
        messages = [
            {"role": "system", "content": "system"},
            {"role": "user", "content": "task"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": "one", "function": {"name": "read_file"}}],
                "_provider_items": [
                    {"type": "reasoning", "encrypted_content": "secret-state" * 500},
                    {"type": "function_call", "call_id": "one"},
                ],
            },
            {"role": "tool", "tool_call_id": "one", "content": "A" * 8_000},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": "two", "function": {"name": "read_file"}}],
                "_provider_items": [
                    {"type": "reasoning", "encrypted_content": "latest-state"},
                    {"type": "function_call", "call_id": "two"},
                ],
            },
            {"role": "tool", "tool_call_id": "two", "content": "B" * 8_000},
        ]

        prepared = manager.prepare(messages)

        self.assertIn("secret-state", str(prepared))
        self.assertIn("latest-state", str(prepared))
        self.assertNotIn("earlier completed tool batch", str(prepared))
        self.assertIn("A" * 1_000, str(prepared))
        self.assertEqual(prepared[-1]["content"], "B" * 8_000)
        included_ids = {
            call["id"]
            for message in prepared
            for call in message.get("tool_calls", [])
        }
        for message in prepared:
            if message.get("role") == "tool":
                self.assertIn(message["tool_call_id"], included_ids)

    def test_completed_write_payload_stays_cacheable_below_soft_budget(self) -> None:
        old_content = "old source body\n" * 800
        latest_content = "latest source body\n" * 300
        messages = [
            {"role": "system", "content": "system"},
            {"role": "user", "content": "build it"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "write-old",
                        "type": "function",
                        "function": {
                            "name": "write_file",
                            "arguments": {"path": "old.py", "content": old_content},
                        },
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "write-old",
                "content": '{"ok": true, "path": "old.py", "additions": 800, "deletions": 0}',
            },
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "write-latest",
                        "type": "function",
                        "function": {
                            "name": "write_file",
                            "arguments": {"path": "latest.py", "content": latest_content},
                        },
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "write-latest",
                "content": '{"ok": true, "path": "latest.py", "additions": 300, "deletions": 0}',
            },
        ]

        manager = ContextManager(max_chars=80_000, max_tokens=30_000)
        prepared = manager.prepare(messages)
        rendered = str(prepared)

        self.assertNotIn("earlier completed tool batch", rendered)
        latest_message = next(
            item
            for item in reversed(prepared)
            if item.get("role") == "assistant" and item.get("tool_calls")
        )
        self.assertEqual(
            latest_message["tool_calls"][0]["function"]["arguments"]["content"],
            latest_content,
        )
        self.assertEqual(prepared, messages)

    def test_completed_read_and_search_stay_cacheable_below_soft_budget(self) -> None:
        messages = [
            {"role": "system", "content": "system"},
            {"role": "user", "content": "inspect"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "read-old",
                        "function": {
                            "name": "read_file",
                            "arguments": {"path": "app.py", "start_line": 1},
                        },
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "read-old",
                "content": (
                    '{"ok":true,"content":"' + "source " * 1000
                    + '","start_line":1,"end_line":100,"total_lines":100,'
                    '"content_hash":"abcdef1234567890"}'
                ),
            },
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"id": "latest", "function": {"name": "search_text", "arguments": {"query": "TODO"}}}
                ],
            },
            {"role": "tool", "tool_call_id": "latest", "content": '{"ok":true,"matches":[]}'},
        ]

        prepared = ContextManager(max_chars=30_000, max_tokens=10_000).prepare(messages)
        rendered = str(prepared)

        self.assertIn("source source source", rendered)
        self.assertNotIn("earlier completed tool batch", rendered)
        self.assertEqual(prepared[-1]["tool_call_id"], "latest")

    def test_near_budget_compaction_moves_in_stable_chunks(self) -> None:
        messages = [
            {"role": "system", "content": "system"},
            {"role": "user", "content": "task"},
        ]
        manager = ContextManager(
            max_chars=30_000,
            max_tokens=20_000,
            soft_limit_ratio=0.6,
            compaction_chunk_batches=4,
            hot_tool_batches=2,
        )
        prepared_at_seven = None
        for index in range(8):
            call_id = f"call-{index}"
            messages.extend(
                [
                    {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [
                            {
                                "id": call_id,
                                "type": "function",
                                "function": {
                                    "name": "read_file",
                                    "arguments": '{"path":"app.py"}',
                                },
                            }
                        ],
                    },
                    {
                        "role": "tool",
                        "tool_call_id": call_id,
                        "content": "source" * 700,
                    },
                ]
            )
            if index == 6:
                prepared_at_seven = manager.prepare(messages)

        prepared_at_eight = manager.prepare(messages)
        assert prepared_at_seven is not None
        self.assertTrue(
            any("earlier completed tool batch" in item.get("content", "") for item in prepared_at_seven)
        )
        self.assertEqual(
            prepared_at_eight[: len(prepared_at_seven)],
            prepared_at_seven,
        )

    def test_v2_stays_append_only_below_high_watermark(self) -> None:
        manager = ContextManager(max_chars=20_000, max_tokens=5_000)
        messages = self._v2_messages(2, output_chars=100)

        prepared = manager.prepare_v2(
            messages,
            state=None,
            tool_schema_tokens=100,
            high_watermark_ratio=0.9,
            target_ratio=0.68,
            hot_tool_batches=2,
            min_checkpoint_batches=3,
        )

        self.assertFalse(prepared.checkpoint_created)
        self.assertEqual(prepared.messages, messages)
        self.assertEqual(prepared.estimated_total_tokens, prepared.estimated_message_tokens + 100)

    def test_v2_checkpoint_bytes_freeze_until_next_eligible_generation(self) -> None:
        manager = ContextManager(max_chars=6_000, max_tokens=2_000)
        messages = self._v2_messages(8)
        first = manager.prepare_v2(
            messages,
            state=None,
            high_watermark_ratio=0.7,
            target_ratio=0.55,
            hot_tool_batches=2,
            min_checkpoint_batches=3,
        )
        self.assertTrue(first.checkpoint_created)
        self.assertEqual(first.checkpoint_generation, 1)
        frozen = first.state["checkpoint_message"]

        appended = self._v2_messages(9)
        second = manager.prepare_v2(
            appended,
            state=first.state,
            high_watermark_ratio=0.7,
            target_ratio=0.55,
            hot_tool_batches=2,
            min_checkpoint_batches=3,
        )

        self.assertFalse(second.checkpoint_created)
        self.assertEqual(second.checkpoint_generation, 1)
        self.assertEqual(second.state["checkpoint_message"], frozen)
        self.assertEqual(second.checkpoint_hash, first.checkpoint_hash)

        later = self._v2_messages(12, output_chars=900)
        third = manager.prepare_v2(
            later,
            state=second.state,
            high_watermark_ratio=0.7,
            target_ratio=0.55,
            hot_tool_batches=2,
            min_checkpoint_batches=3,
        )
        self.assertTrue(third.checkpoint_created)
        self.assertEqual(third.checkpoint_generation, 2)
        self.assertNotEqual(third.checkpoint_hash, first.checkpoint_hash)

    def test_v2_checkpoint_is_deterministic_and_protocol_valid(self) -> None:
        messages = self._v2_messages(8)
        durable = {
            "goal": "finish feature",
            "requirements": ["works offline", "has tests"],
            "changed_files": ["app.py"],
            "change_revision": 2,
        }
        kwargs = {
            "state": None,
            "durable_state": durable,
            "high_watermark_ratio": 0.7,
            "target_ratio": 0.55,
            "hot_tool_batches": 2,
            "min_checkpoint_batches": 3,
        }

        left = ContextManager(max_chars=6_000, max_tokens=2_000).prepare_v2(
            messages, **kwargs
        )
        right = ContextManager(max_chars=6_000, max_tokens=2_000).prepare_v2(
            messages, **kwargs
        )

        self.assertEqual(left.state["checkpoint_message"], right.state["checkpoint_message"])
        self.assertEqual(left.checkpoint_hash, right.checkpoint_hash)
        included_ids = {
            str(call.get("id"))
            for message in left.messages
            for call in message.get("tool_calls", [])
        }
        for message in left.messages:
            if message.get("role") == "tool":
                self.assertIn(str(message.get("tool_call_id")), included_ids)

    def test_v2_keeps_protected_failed_batch_in_hot_tail(self) -> None:
        messages = self._v2_messages(10)
        prepared = ContextManager(max_chars=6_000, max_tokens=2_000).prepare_v2(
            messages,
            state=None,
            durable_state={"protected_tool_call_ids": ["call-5"]},
            high_watermark_ratio=0.7,
            target_ratio=0.55,
            hot_tool_batches=2,
            min_checkpoint_batches=3,
        )

        hot_ids = {
            str(call.get("id"))
            for message in prepared.messages
            for call in message.get("tool_calls", [])
        }
        self.assertIn("call-5", hot_ids)
        self.assertNotIn("call-4", hot_ids)

    def test_v2_marks_emergency_when_hard_limit_precedes_interval(self) -> None:
        manager = ContextManager(max_chars=2_000, max_tokens=1_000)
        messages = self._v2_messages(4, output_chars=1_200)
        prepared = manager.prepare_v2(
            messages,
            state=None,
            high_watermark_ratio=0.9,
            target_ratio=0.68,
            hot_tool_batches=2,
            min_checkpoint_batches=20,
        )

        self.assertEqual(prepared.compaction_reason, "emergency")
        self.assertFalse(prepared.checkpoint_created)
        self.assertEqual(prepared.checkpoint_generation, 0)
        rendered = str(prepared.messages)
        self.assertIn("call-3", rendered)
        self.assertLessEqual(manager.estimate_chars(prepared.messages), manager.max_chars)

    def test_v2_emergency_tail_trim_does_not_rewrite_existing_checkpoint(self) -> None:
        manager = ContextManager(max_chars=8_000, max_tokens=2_500)
        first = manager.prepare_v2(
            self._v2_messages(6, output_chars=700),
            state=None,
            tool_schema_tokens=100,
            high_watermark_ratio=0.8,
            target_ratio=0.6,
            hot_tool_batches=1,
            min_checkpoint_batches=3,
        )
        self.assertTrue(first.checkpoint_created)
        frozen = first.state["checkpoint_message"]

        second = manager.prepare_v2(
            self._v2_messages(7, output_chars=4_000),
            state=first.state,
            tool_schema_tokens=100,
            high_watermark_ratio=0.8,
            target_ratio=0.6,
            hot_tool_batches=1,
            min_checkpoint_batches=3,
        )

        self.assertFalse(second.checkpoint_created)
        self.assertEqual(second.compaction_reason, "emergency")
        self.assertEqual(second.checkpoint_generation, first.checkpoint_generation)
        self.assertEqual(second.checkpoint_hash, first.checkpoint_hash)
        self.assertEqual(second.state["checkpoint_message"], frozen)
        self.assertIn(frozen, second.messages)
        self.assertLessEqual(second.estimated_total_tokens, manager.max_tokens)


if __name__ == "__main__":
    unittest.main()
