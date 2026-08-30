from __future__ import annotations

import unittest

from mini_coder.context import ContextManager


class ContextManagerTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
