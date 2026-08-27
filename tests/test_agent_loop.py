from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from mini_coder.agent import AgentRunner
from mini_coder.config import AgentConfig, ApprovalPolicy
from mini_coder.messages import ModelResponse, ToolCall
from mini_coder.model import ModelClient
from mini_coder.tools import create_default_registry


class FakeModel(ModelClient):
    def __init__(self, responses: list[ModelResponse]) -> None:
        self.responses = list(responses)
        self.requests: list[tuple[list[dict], list[dict]]] = []

    def complete(self, messages, tools) -> ModelResponse:
        self.requests.append((list(messages), list(tools)))
        return self.responses.pop(0)


def make_config(workspace: Path, **overrides) -> AgentConfig:
    values = {
        "workspace": workspace,
        "api_key": "test",
        "base_url": None,
        "model": "fake",
        "approval_policy": ApprovalPolicy.AUTO,
        "max_steps": 10,
        "command_timeout_seconds": 5,
        "max_tool_output_chars": 4_000,
        "max_context_chars": 20_000,
        "repeated_call_limit": 3,
    }
    values.update(overrides)
    return AgentConfig(**values)


class AgentLoopTests(unittest.TestCase):
    def test_tool_result_is_returned_to_model_before_final_answer(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            (workspace / "hello.py").write_text("print('hello')\n", encoding="utf-8")
            model = FakeModel(
                [
                    ModelResponse(
                        tool_calls=[
                            ToolCall(
                                id="call-1",
                                name="list_files",
                                arguments={"path": "."},
                                raw_arguments='{"path":"."}',
                            )
                        ]
                    ),
                    ModelResponse(content="Inspected the project; no change was needed."),
                ]
            )
            runner = AgentRunner(
                model=model,
                registry=create_default_registry(),
                config=make_config(workspace),
            )

            result = runner.run("Inspect this project")

            self.assertEqual(result.status, "completed")
            self.assertEqual(result.steps, 2)
            second_request = model.requests[1][0]
            tool_messages = [item for item in second_request if item["role"] == "tool"]
            self.assertEqual(len(tool_messages), 1)
            self.assertIn("hello.py", tool_messages[0]["content"])

    def test_safe_mode_denies_write_without_callback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            model = FakeModel(
                [
                    ModelResponse(
                        tool_calls=[
                            ToolCall(
                                id="call-write",
                                name="write_file",
                                arguments={"path": "new.txt", "content": "data"},
                                raw_arguments='{"path":"new.txt","content":"data"}',
                            )
                        ]
                    ),
                    ModelResponse(content="The write was denied, so I stopped."),
                ]
            )
            runner = AgentRunner(
                model=model,
                registry=create_default_registry(),
                config=make_config(workspace, approval_policy=ApprovalPolicy.SAFE),
            )

            result = runner.run("Create a file")

            self.assertEqual(result.status, "completed")
            self.assertFalse((workspace / "new.txt").exists())
            self.assertIn("User denied", model.requests[1][0][-1]["content"])

    def test_repeated_identical_calls_stop_the_loop(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            call = lambda number: ModelResponse(
                tool_calls=[
                    ToolCall(
                        id=f"call-{number}",
                        name="read_file",
                        arguments={"path": "missing.txt"},
                        raw_arguments='{"path":"missing.txt"}',
                    )
                ]
            )
            model = FakeModel([call(1), call(2), call(3)])
            runner = AgentRunner(
                model=model,
                registry=create_default_registry(),
                config=make_config(workspace),
            )

            result = runner.run("Keep reading the missing file")

            self.assertEqual(result.status, "repeated_call")
            self.assertEqual(result.steps, 3)


if __name__ == "__main__":
    unittest.main()

