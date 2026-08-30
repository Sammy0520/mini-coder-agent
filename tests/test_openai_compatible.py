from __future__ import annotations

import unittest
from types import SimpleNamespace

from mini_coder.config import WireAPI
from mini_coder.model.openai_compatible import OpenAICompatibleClient


class OpenAICompatibleParsingTests(unittest.TestCase):
    def test_parses_responses_function_call_and_preserves_output_items(self) -> None:
        function_call = SimpleNamespace(
            type="function_call",
            id="fc-1",
            call_id="call-1",
            name="read_file",
            arguments='{"path":"main.py"}',
            status="completed",
        )
        response = SimpleNamespace(
            id="resp-1",
            model="gpt-5.6-sol-2026-08-01",
            service_tier="default",
            output=[function_call],
            output_text="",
            status="completed",
            usage=SimpleNamespace(input_tokens=20, output_tokens=5, total_tokens=25),
        )

        parsed = OpenAICompatibleClient._parse_response(response)

        self.assertEqual(parsed.tool_calls[0].id, "call-1")
        self.assertEqual(parsed.tool_calls[0].arguments, {"path": "main.py"})
        self.assertEqual(parsed.usage["total_tokens"], 25)
        self.assertEqual(parsed.provider_items[0]["type"], "function_call")
        self.assertEqual(parsed.provider_metadata["response_id"], "resp-1")
        self.assertEqual(parsed.provider_metadata["model"], "gpt-5.6-sol-2026-08-01")
        self.assertEqual(parsed.provider_metadata["service_tier"], "default")

    def test_responses_history_replays_output_and_function_result(self) -> None:
        messages = [
            {"role": "system", "content": "system"},
            {"role": "user", "content": "task"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call-1",
                        "type": "function",
                        "function": {"name": "read_file", "arguments": '{"path":"main.py"}'},
                    }
                ],
                "_provider_items": [
                    {"type": "reasoning", "id": "rs-1", "encrypted_content": "opaque"},
                    {
                        "type": "function_call",
                        "id": "fc-1",
                        "call_id": "call-1",
                        "name": "read_file",
                        "arguments": '{"path":"main.py"}',
                    },
                ],
            },
            {"role": "tool", "tool_call_id": "call-1", "content": '{"ok":true}'},
        ]

        converted = OpenAICompatibleClient._to_responses_input(messages)

        self.assertEqual(converted[2]["type"], "reasoning")
        self.assertEqual(converted[3]["type"], "function_call")
        self.assertEqual(converted[4]["type"], "function_call_output")
        self.assertEqual(converted[4]["call_id"], "call-1")

    def test_responses_request_receives_reasoning_and_verbosity(self) -> None:
        captured = {}

        class Responses:
            @staticmethod
            def create(**kwargs):
                captured.update(kwargs)
                return SimpleNamespace(
                    output=[],
                    output_text="done",
                    status="completed",
                    usage=SimpleNamespace(input_tokens=1, output_tokens=1, total_tokens=2),
                )

        client = object.__new__(OpenAICompatibleClient)
        client._client = SimpleNamespace(responses=Responses())
        client._model = "gpt-5.6-sol"
        client._wire_api = WireAPI.RESPONSES
        client._reasoning_effort = "xhigh"
        client._verbosity = "high"

        result = client._complete_responses(
            [{"role": "user", "content": "task"}],
            [
                {
                    "type": "function",
                    "function": {
                        "name": "read_file",
                        "description": "Read a file",
                        "parameters": {"type": "object", "properties": {}},
                    },
                }
            ],
        )

        self.assertEqual(result.content, "done")
        self.assertEqual(captured["model"], "gpt-5.6-sol")
        self.assertEqual(captured["reasoning"], {"effort": "xhigh"})
        self.assertEqual(captured["text"], {"verbosity": "high"})
        self.assertEqual(captured["tools"][0]["name"], "read_file")

    def test_responses_streams_with_cache_key_and_reports_cache_usage(self) -> None:
        captured = {}
        response = SimpleNamespace(
            output=[],
            output_text="done",
            status="completed",
            usage=SimpleNamespace(
                input_tokens=5000,
                output_tokens=100,
                total_tokens=5100,
                input_tokens_details=SimpleNamespace(
                    cached_tokens=4096,
                    cache_write_tokens=904,
                ),
                output_tokens_details=SimpleNamespace(reasoning_tokens=40),
            ),
        )

        class Stream:
            def get_final_response(self):
                return response

        class Manager:
            def __enter__(self):
                return Stream()

            def __exit__(self, *_args):
                return False

        class Responses:
            @staticmethod
            def stream(**kwargs):
                captured.update(kwargs)
                return Manager()

            @staticmethod
            def create(**_kwargs):
                raise AssertionError("non-streaming fallback should not be used")

        client = object.__new__(OpenAICompatibleClient)
        client._client = SimpleNamespace(responses=Responses())
        client._model = "gpt-5.6-sol"
        client._wire_api = WireAPI.RESPONSES
        client._reasoning_effort = "xhigh"
        client._verbosity = "high"
        client._streaming = True
        client._prompt_cache_enabled = True
        client._prompt_cache_key = "mini-coder-agent-v1"

        result = client._complete_responses(
            [{"role": "user", "content": "task"}],
            [],
        )

        self.assertEqual(captured["prompt_cache_key"], "mini-coder-agent-v1")
        self.assertEqual(result.usage["cached_tokens"], 4096)
        self.assertEqual(result.usage["cache_write_tokens"], 904)
        self.assertEqual(result.usage["reasoning_tokens"], 40)

    def test_parses_function_tool_call_without_importing_sdk(self) -> None:
        completion = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    finish_reason="tool_calls",
                    message=SimpleNamespace(
                        content=None,
                        tool_calls=[
                            SimpleNamespace(
                                id="call-1",
                                function=SimpleNamespace(
                                    name="read_file", arguments='{"path":"main.py"}'
                                ),
                            )
                        ],
                    ),
                )
            ],
            usage=SimpleNamespace(prompt_tokens=10, completion_tokens=4, total_tokens=14),
        )

        response = OpenAICompatibleClient._parse_completion(completion)

        self.assertEqual(response.tool_calls[0].name, "read_file")
        self.assertEqual(response.tool_calls[0].arguments, {"path": "main.py"})
        self.assertEqual(response.usage["total_tokens"], 14)

    def test_malformed_arguments_become_a_local_tool_error(self) -> None:
        completion = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    finish_reason="tool_calls",
                    message=SimpleNamespace(
                        content="",
                        tool_calls=[
                            SimpleNamespace(
                                id="call-1",
                                function=SimpleNamespace(name="read_file", arguments="{bad"),
                            )
                        ],
                    ),
                )
            ],
            usage=None,
        )

        response = OpenAICompatibleClient._parse_completion(completion)

        self.assertIsNone(response.tool_calls[0].arguments)
        self.assertIsNotNone(response.tool_calls[0].parse_error)


if __name__ == "__main__":
    unittest.main()
