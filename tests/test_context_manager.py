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

