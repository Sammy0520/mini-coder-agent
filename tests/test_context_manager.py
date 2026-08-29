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

    def test_old_reasoning_and_large_tool_output_are_not_replayed_in_full(self) -> None:
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

        self.assertNotIn("secret-state", str(prepared))
        self.assertIn("latest-state", str(prepared))
        self.assertIn("older tool output compacted", prepared[3]["content"])
        self.assertEqual(prepared[-1]["content"], "B" * 8_000)
        included_ids = {
            call["id"]
            for message in prepared
            for call in message.get("tool_calls", [])
        }
        for message in prepared:
            if message.get("role") == "tool":
                self.assertIn(message["tool_call_id"], included_ids)


if __name__ == "__main__":
    unittest.main()
