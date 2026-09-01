from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from mini_coder.agent import AgentRunner, _SpeculativeFinish
from mini_coder.config import AgentConfig, ApprovalPolicy
from mini_coder.messages import ModelResponse, ToolCall
from mini_coder.model import ModelClient
from mini_coder.session import AgentSession, SessionStatus, SessionStore, VerificationStatus
from mini_coder.tools import create_default_registry


class FakeModel(ModelClient):
    def __init__(self, responses: list[ModelResponse], *, delay: float = 0.0) -> None:
        self.responses = list(responses)
        self.delay = delay
        self.requests: list[tuple[list[dict], list[dict]]] = []
        self.started_at: list[float] = []
        self.completed_at: list[float] = []

    def complete(self, messages, tools) -> ModelResponse:
        self.requests.append((list(messages), list(tools)))
        self.started_at.append(time.monotonic())
        if self.delay:
            time.sleep(self.delay)
        response = self.responses.pop(0)
        self.completed_at.append(time.monotonic())
        return response


def call(call_id: str, name: str, arguments: dict) -> ToolCall:
    return ToolCall(
        id=call_id,
        name=name,
        arguments=arguments,
        raw_arguments="{}",
    )


def write_call() -> ToolCall:
    return call(
        "write",
        "write_file",
        {"path": "result.txt", "content": "ready\n"},
    )


def verify_call(*, delay: float, exit_code: int = 0) -> ToolCall:
    code = f"import time; time.sleep({delay}); raise SystemExit({exit_code})"
    return call(
        "verify",
        "run_command",
        {
            "command": f'python -c "{code}"',
            "purpose": "verify",
            "verification_paths": ["result.txt"],
        },
    )


def config(workspace: Path, *, delay_ms: int) -> AgentConfig:
    return AgentConfig(
        workspace=workspace,
        api_key="secret-token",
        base_url=None,
        model="fake",
        approval_policy=ApprovalPolicy.AUTO,
        max_steps=6,
        command_timeout_seconds=5,
        max_context_chars=20_000,
        speculative_finish_enabled=True,
        speculative_finish_delay_ms=delay_ms,
    )


class SpeculativeFinishTests(unittest.TestCase):
    def test_slow_final_verification_overlaps_and_commits_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            store = SessionStore.for_workspace(workspace)
            main = FakeModel(
                [
                    ModelResponse(tool_calls=[write_call()]),
                    ModelResponse(tool_calls=[verify_call(delay=0.24)]),
                ]
            )
            finalizer = FakeModel(
                [
                    ModelResponse(
                        content="Implemented the requested file.",
                        usage={"input_tokens": 40, "output_tokens": 8, "total_tokens": 48},
                    )
                ],
                delay=0.16,
            )
            started = time.monotonic()
            result = AgentRunner(
                model=main,
                speculative_model=finalizer,
                registry=create_default_registry(),
                config=config(workspace, delay_ms=20),
                approval_callback=lambda tool, arguments: True,
                session_store=store,
                response_language="en",
            ).run("Create result.txt and verify it")
            elapsed = time.monotonic() - started

            session = store.load(result.session_id or "")
            metrics = session.working_memory["turn_state"]["speculative_finish_metrics"]
            self.assertEqual(result.status, "completed")
            self.assertEqual(session.status, SessionStatus.COMPLETED_VERIFIED)
            self.assertEqual(session.verification_status, VerificationStatus.PASSED)
            self.assertEqual(session.stop_reason, "speculative_finalizer_committed")
            self.assertEqual(len(main.requests), 2)
            self.assertEqual(len(finalizer.requests), 1)
            self.assertEqual(finalizer.requests[0][1], [])
            self.assertEqual(metrics["attempts"], 1)
            self.assertEqual(metrics["accepted"], 1)
            self.assertGreater(metrics["overlapped_seconds"], 0.08)
            self.assertIn("Local verification passed:", result.final_text)
            self.assertGreater(elapsed, 0.0)

    def test_fast_verification_skips_speculation_and_uses_normal_final_call(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            store = SessionStore.for_workspace(workspace)
            main = FakeModel(
                [
                    ModelResponse(tool_calls=[write_call()]),
                    ModelResponse(tool_calls=[verify_call(delay=0.0)]),
                    ModelResponse(content="Normal final response."),
                ]
            )
            finalizer = FakeModel([ModelResponse(content="unused")])
            result = AgentRunner(
                model=main,
                speculative_model=finalizer,
                registry=create_default_registry(),
                config=config(workspace, delay_ms=1_000),
                approval_callback=lambda tool, arguments: True,
                session_store=store,
            ).run("Create result.txt and verify it")

            session = store.load(result.session_id or "")
            self.assertEqual(result.status, "completed")
            self.assertEqual(len(main.requests), 3)
            self.assertEqual(len(finalizer.requests), 0)
            self.assertNotIn(
                "speculative_finish_metrics",
                session.working_memory["turn_state"],
            )

    def test_failed_verification_discards_candidate_and_preserves_failure_gate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            store = SessionStore.for_workspace(workspace)
            main = FakeModel(
                [
                    ModelResponse(tool_calls=[write_call()]),
                    ModelResponse(tool_calls=[verify_call(delay=0.12, exit_code=3)]),
                    ModelResponse(content="Fallback response."),
                ]
            )
            finalizer = FakeModel([ModelResponse(content="Candidate summary.")], delay=0.05)
            result = AgentRunner(
                model=main,
                speculative_model=finalizer,
                registry=create_default_registry(),
                config=config(workspace, delay_ms=10),
                approval_callback=lambda tool, arguments: True,
                session_store=store,
            ).run("Create result.txt and verify it")

            session = store.load(result.session_id or "")
            metrics = session.working_memory["turn_state"]["speculative_finish_metrics"]
            self.assertEqual(result.status, "verification_failed")
            self.assertEqual(session.status, SessionStatus.FAILED)
            self.assertEqual(session.verification_status, VerificationStatus.FAILED)
            self.assertEqual(len(main.requests), 3)
            self.assertEqual(metrics["discarded"], 1)
            self.assertEqual(metrics["last_discard_reason"], "verification_not_passed")
            self.assertNotIn("Candidate summary", result.final_text)

    def test_candidate_with_tool_call_is_discarded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            store = SessionStore.for_workspace(workspace)
            main = FakeModel(
                [
                    ModelResponse(tool_calls=[write_call()]),
                    ModelResponse(tool_calls=[verify_call(delay=0.12)]),
                    ModelResponse(content="Safe fallback."),
                ]
            )
            finalizer = FakeModel(
                [
                    ModelResponse(
                        content="Candidate",
                        tool_calls=[call("bad", "read_file", {"path": "result.txt"})],
                    )
                ]
            )
            result = AgentRunner(
                model=main,
                speculative_model=finalizer,
                registry=create_default_registry(),
                config=config(workspace, delay_ms=10),
                approval_callback=lambda tool, arguments: True,
                session_store=store,
            ).run("Create result.txt and verify it")

            session = store.load(result.session_id or "")
            metrics = session.working_memory["turn_state"]["speculative_finish_metrics"]
            self.assertEqual(result.status, "completed")
            self.assertEqual(metrics["last_discard_reason"], "candidate_requested_tools")
            self.assertIn("Safe fallback", result.final_text)

    def test_candidate_cannot_claim_unconfirmed_verification(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            store = SessionStore.for_workspace(workspace)
            main = FakeModel(
                [
                    ModelResponse(tool_calls=[write_call()]),
                    ModelResponse(tool_calls=[verify_call(delay=0.12)]),
                    ModelResponse(content="Truthful fallback."),
                ]
            )
            finalizer = FakeModel([ModelResponse(content="All tests passed.")])
            result = AgentRunner(
                model=main,
                speculative_model=finalizer,
                registry=create_default_registry(),
                config=config(workspace, delay_ms=10),
                approval_callback=lambda tool, arguments: True,
                session_store=store,
            ).run("Create result.txt and verify it")

            session = store.load(result.session_id or "")
            metrics = session.working_memory["turn_state"]["speculative_finish_metrics"]
            self.assertEqual(result.status, "completed")
            self.assertEqual(
                metrics["last_discard_reason"],
                "candidate_claimed_unconfirmed_verification",
            )
            self.assertIn("Truthful fallback", result.final_text)
            self.assertNotIn("All tests passed", result.final_text)

    def test_revision_change_rejects_an_otherwise_valid_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            runner = AgentRunner(
                model=FakeModel([]),
                speculative_model=FakeModel([]),
                registry=create_default_registry(),
                config=config(workspace, delay_ms=10),
            )
            session = AgentSession.create(
                task="test revision gate",
                workspace=workspace,
                model=runner._model_summary(),
                messages=[],
            )
            session.change_revision = 2
            session.verification_status = VerificationStatus.PASSED
            runner._pending_speculative_finish = _SpeculativeFinish(
                speculation_id="spec",
                change_revision=1,
                verification_command="python -m unittest",
                response=ModelResponse(content="Valid summary."),
                model_error=None,
                model_seconds=0.1,
                verification_seconds=0.2,
                wall_seconds=0.2,
            )

            result = runner._commit_or_discard_speculative_finish(
                step=1,
                messages=[],
                usage={},
                session=session,
            )

            metrics = session.working_memory["turn_state"]["speculative_finish_metrics"]
            self.assertIsNone(result)
            self.assertEqual(metrics["last_discard_reason"], "change_revision_changed")


if __name__ == "__main__":
    unittest.main()
