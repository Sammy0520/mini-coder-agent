from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from mini_coder.agent import AgentRunner
from mini_coder.config import AgentConfig, ApprovalPolicy
from mini_coder.messages import ModelResponse, ToolCall
from mini_coder.model import ModelClient
from mini_coder.session import SessionStatus, SessionStore
from mini_coder.verification import TaskPhase, VerificationStatus
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
    def test_analysis_only_task_completes_without_meaningless_verification(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            store = SessionStore.for_workspace(workspace)
            runner = AgentRunner(
                model=FakeModel([ModelResponse(content="No code change is needed.")]),
                registry=create_default_registry(),
                config=make_config(workspace),
                session_store=store,
            )

            result = runner.run("Explain whether this empty project needs changes")

            session = store.load(result.session_id or "")
            self.assertEqual(result.status, "completed")
            self.assertEqual(session.status, SessionStatus.COMPLETED_UNVERIFIED)
            self.assertEqual(session.verification_status, VerificationStatus.NOT_REQUIRED)
            self.assertEqual(session.phase, TaskPhase.SUMMARIZE)

    def test_safe_write_shows_diff_before_approval_and_denial_changes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            store = SessionStore.for_workspace(workspace)
            events: list[tuple[str, dict]] = []
            approval_observations: list[str] = []
            model = FakeModel(
                [
                    ModelResponse(
                        tool_calls=[
                            ToolCall(
                                id="call-preview",
                                name="write_file",
                                arguments={"path": "new.txt", "content": "alpha\n"},
                                raw_arguments='{"path":"new.txt","content":"alpha\\n"}',
                            )
                        ]
                    ),
                    ModelResponse(content="The write was denied."),
                ]
            )

            def approve(tool, arguments) -> bool:
                approval_observations.append(events[-1][0])
                return False

            runner = AgentRunner(
                model=model,
                registry=create_default_registry(),
                config=make_config(workspace, approval_policy=ApprovalPolicy.SAFE),
                approval_callback=approve,
                event_callback=lambda name, payload: events.append((name, payload)),
                session_store=store,
            )

            result = runner.run("Create new.txt")

            self.assertEqual(result.status, "denied")
            self.assertEqual(approval_observations, ["change_preview"])
            preview = next(payload for name, payload in events if name == "change_preview")
            self.assertEqual(preview["path"], "new.txt")
            self.assertEqual(preview["additions"], 1)
            self.assertIn("+++ b/new.txt", preview["diff"])
            self.assertFalse((workspace / "new.txt").exists())
            session = store.load(result.session_id or "")
            self.assertEqual(session.changes, [])
            self.assertEqual(session.status, SessionStatus.DENIED)
            self.assertIsNotNone(session.tool_executions[0].prepared_change)

    def test_safe_mode_batches_multiple_writes_from_one_model_response(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            batches: list[list[str]] = []
            model = FakeModel(
                [
                    ModelResponse(
                        tool_calls=[
                            ToolCall(
                                id="call-one",
                                name="write_file",
                                arguments={"path": "one.txt", "content": "one\n"},
                                raw_arguments='{"path":"one.txt","content":"one\\n"}',
                            ),
                            ToolCall(
                                id="call-two",
                                name="write_file",
                                arguments={"path": "two.txt", "content": "two\n"},
                                raw_arguments='{"path":"two.txt","content":"two\\n"}',
                            ),
                        ]
                    ),
                    ModelResponse(content="Created both files."),
                ]
            )

            def approve_batch(items) -> bool:
                batches.append([arguments["path"] for _, arguments in items])
                return True

            runner = AgentRunner(
                model=model,
                registry=create_default_registry(),
                config=make_config(workspace, approval_policy=ApprovalPolicy.SAFE),
                approval_callback=lambda *_: self.fail("individual approval was requested"),
                batch_approval_callback=approve_batch,
            )

            result = runner.run("Create two files")

            self.assertEqual(result.status, "completed")
            self.assertEqual(batches, [["one.txt", "two.txt"]])
            self.assertEqual((workspace / "one.txt").read_text(encoding="utf-8"), "one\n")
            self.assertEqual((workspace / "two.txt").read_text(encoding="utf-8"), "two\n")

    def test_response_write_batch_limit_defers_extra_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            store = SessionStore.for_workspace(workspace)
            calls = [
                ToolCall(
                    id=f"call-{name}",
                    name="write_file",
                    arguments={"path": f"{name}.txt", "content": f"{name}\n"},
                    raw_arguments=f'{{"path":"{name}.txt","content":"{name}\\n"}}',
                )
                for name in ("one", "two", "three")
            ]
            model = FakeModel(
                [
                    ModelResponse(tool_calls=calls),
                    ModelResponse(content="Completed the first small batch."),
                ]
            )
            runner = AgentRunner(
                model=model,
                registry=create_default_registry(),
                config=make_config(workspace, max_response_write_calls=2),
                session_store=store,
            )

            result = runner.run("Create three files in small batches")

            self.assertEqual(result.status, "completed")
            self.assertTrue((workspace / "one.txt").exists())
            self.assertTrue((workspace / "two.txt").exists())
            self.assertFalse((workspace / "three.txt").exists())
            rendered = str(model.requests[-1][0])
            self.assertIn("too many file changes at once", rendered)
            session = store.load(result.session_id or "")
            self.assertEqual(session.failed_tool_call_count, 1)

    def test_multiple_agent_writes_are_ordered_and_summarized_in_session(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            store = SessionStore.for_workspace(workspace)
            model = FakeModel(
                [
                    ModelResponse(
                        tool_calls=[
                            ToolCall(
                                id="call-create",
                                name="write_file",
                                arguments={"path": "tracked.txt", "content": "one\n"},
                                raw_arguments='{"path":"tracked.txt","content":"one\\n"}',
                            )
                        ]
                    ),
                    ModelResponse(
                        tool_calls=[
                            ToolCall(
                                id="call-edit",
                                name="edit_file",
                                arguments={
                                    "path": "tracked.txt",
                                    "old_text": "one",
                                    "new_text": "two",
                                },
                                raw_arguments=(
                                    '{"path":"tracked.txt","old_text":"one","new_text":"two"}'
                                ),
                            )
                        ]
                    ),
                    ModelResponse(content="Created and updated the file."),
                ]
            )
            runner = AgentRunner(
                model=model,
                registry=create_default_registry(),
                config=make_config(workspace),
                session_store=store,
            )

            result = runner.run("Create tracked.txt and change one to two")

            self.assertEqual(result.status, "completed")
            self.assertIn("Local change summary:", result.final_text)
            self.assertIn(
                "tracked.txt: 2 change(s), +2/-1; current hash matches",
                result.final_text,
            )
            session = store.load(result.session_id or "")
            self.assertEqual(len(session.changes), 2)
            self.assertEqual(session.changes[0].after_hash, session.changes[1].before_hash)
            self.assertEqual(
                [item.change_id for item in session.tool_executions],
                [item.change_id for item in session.changes],
            )
            self.assertEqual((workspace / "tracked.txt").read_text(encoding="utf-8"), "two\n")

    def test_external_change_during_approval_is_not_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            path = workspace / "conflict.txt"
            path.write_text("before\n", encoding="utf-8")
            store = SessionStore.for_workspace(workspace)
            model = FakeModel(
                [
                    ModelResponse(
                        tool_calls=[
                            ToolCall(
                                id="call-conflict",
                                name="edit_file",
                                arguments={
                                    "path": "conflict.txt",
                                    "old_text": "before",
                                    "new_text": "agent",
                                },
                                raw_arguments=(
                                    '{"path":"conflict.txt","old_text":"before","new_text":"agent"}'
                                ),
                            )
                        ]
                    ),
                    ModelResponse(content="A conflict prevented the edit."),
                ]
            )

            def approve(tool, arguments) -> bool:
                path.write_text("user change\n", encoding="utf-8")
                return True

            runner = AgentRunner(
                model=model,
                registry=create_default_registry(),
                config=make_config(workspace, approval_policy=ApprovalPolicy.SAFE),
                approval_callback=approve,
                session_store=store,
            )

            result = runner.run("Change before to agent")

            self.assertEqual(result.status, "completed")
            self.assertEqual(path.read_text(encoding="utf-8"), "user change\n")
            self.assertTrue(
                any(
                    "changed after approval" in str(message.get("content") or "")
                    for message in model.requests[1][0]
                )
            )
            session = store.load(result.session_id or "")
            self.assertEqual(session.changes, [])
            self.assertFalse(session.tool_executions[0].ok)

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

    def test_modified_task_without_verification_is_completed_unverified(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            store = SessionStore.for_workspace(workspace)
            model = FakeModel(
                [
                    ModelResponse(
                        tool_calls=[
                            ToolCall(
                                id="call-write-unverified",
                                name="write_file",
                                arguments={"path": "result.txt", "content": "data\n"},
                                raw_arguments=(
                                    '{"path":"result.txt","content":"data\\n"}'
                                ),
                            )
                        ]
                    ),
                    ModelResponse(content="Implemented the requested file."),
                ]
            )
            runner = AgentRunner(
                model=model,
                registry=create_default_registry(),
                config=make_config(workspace),
                session_store=store,
            )

            result = runner.run("Create result.txt")

            session = store.load(result.session_id or "")
            self.assertEqual(result.status, "completed")
            self.assertEqual(session.status, SessionStatus.COMPLETED_UNVERIFIED)
            self.assertEqual(session.verification_status, VerificationStatus.UNVERIFIED)
            self.assertEqual(session.phase, TaskPhase.SUMMARIZE)
            self.assertIn("Current file changes have not been verified", result.final_text)

    def test_successful_real_command_marks_current_changes_verified(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            store = SessionStore.for_workspace(workspace)
            model = FakeModel(
                [
                    ModelResponse(
                        tool_calls=[
                            ToolCall(
                                id="call-write-verified",
                                name="write_file",
                                arguments={"path": "result.txt", "content": "data\n"},
                                raw_arguments=(
                                    '{"path":"result.txt","content":"data\\n"}'
                                ),
                            )
                        ]
                    ),
                    ModelResponse(
                        tool_calls=[
                            ToolCall(
                                id="call-verify-pass",
                                name="run_command",
                                arguments={
                                    "command": 'python -c "print(\'ok\')"',
                                    "purpose": "verify",
                                },
                                raw_arguments="{}",
                            )
                        ]
                    ),
                    ModelResponse(content="Implemented and tested."),
                ]
            )
            runner = AgentRunner(
                model=model,
                registry=create_default_registry(),
                config=make_config(workspace),
                approval_callback=lambda tool, arguments: True,
                session_store=store,
            )

            result = runner.run("Create and test result.txt")

            session = store.load(result.session_id or "")
            self.assertEqual(result.status, "completed")
            self.assertEqual(session.status, SessionStatus.COMPLETED_VERIFIED)
            self.assertEqual(session.verification_status, VerificationStatus.PASSED)
            self.assertEqual(len(session.verification_records), 1)
            self.assertEqual(session.verification_records[0].exit_code, 0)
            self.assertGreaterEqual(session.verification_records[0].duration_seconds, 0)
            command_record = session.tool_executions[-1]
            self.assertEqual(command_record.exit_code, 0)
            self.assertFalse(command_record.timed_out)
            self.assertFalse(command_record.output_truncated)
            self.assertIsNotNone(command_record.duration_seconds)
            self.assertIn("completed_verified", result.final_text)

    def test_expected_nonzero_command_completes_as_verified(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            store = SessionStore.for_workspace(workspace)
            model = FakeModel(
                [
                    ModelResponse(
                        tool_calls=[
                            ToolCall(
                                id="call-write-negative",
                                name="write_file",
                                arguments={"path": "result.txt", "content": "data\n"},
                                raw_arguments="{}",
                            )
                        ]
                    ),
                    ModelResponse(
                        tool_calls=[
                            ToolCall(
                                id="call-expected-rejection",
                                name="run_command",
                                arguments={
                                    "command": 'python -c "raise SystemExit(2)"',
                                    "purpose": "verify",
                                    "expected_exit_codes": [2],
                                },
                                raw_arguments="{}",
                            )
                        ]
                    ),
                    ModelResponse(content="The invalid input was rejected as expected."),
                ]
            )
            runner = AgentRunner(
                model=model,
                registry=create_default_registry(),
                config=make_config(workspace),
                approval_callback=lambda tool, arguments: True,
                session_store=store,
            )

            result = runner.run("Create a file and verify the negative path")

            session = store.load(result.session_id or "")
            self.assertEqual(result.status, "completed")
            self.assertEqual(session.status, SessionStatus.COMPLETED_VERIFIED)
            self.assertEqual(session.verification_status, VerificationStatus.PASSED)
            self.assertEqual(session.verification_records[-1].exit_code, 2)
            self.assertEqual(session.verification_records[-1].expected_exit_codes, (2,))
            self.assertTrue(session.verification_records[-1].expectation_met)
            self.assertEqual(session.failed_tool_call_count, 0)

    def test_failed_verification_overrides_model_completion_claim(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            store = SessionStore.for_workspace(workspace)
            model = FakeModel(
                [
                    ModelResponse(
                        tool_calls=[
                            ToolCall(
                                id="call-write-before-fail",
                                name="write_file",
                                arguments={"path": "result.txt", "content": "bad\n"},
                                raw_arguments="{}",
                            )
                        ]
                    ),
                    ModelResponse(
                        tool_calls=[
                            ToolCall(
                                id="call-verify-fail",
                                name="run_command",
                                arguments={
                                    "command": 'python -c "raise SystemExit(3)"',
                                    "purpose": "verify",
                                },
                                raw_arguments="{}",
                            )
                        ]
                    ),
                    ModelResponse(content="Everything passed."),
                ]
            )
            runner = AgentRunner(
                model=model,
                registry=create_default_registry(),
                config=make_config(workspace),
                approval_callback=lambda tool, arguments: True,
                session_store=store,
            )

            result = runner.run("Create a valid result")

            session = store.load(result.session_id or "")
            self.assertEqual(result.status, "verification_failed")
            self.assertEqual(session.status, SessionStatus.FAILED)
            self.assertEqual(session.verification_status, VerificationStatus.FAILED)
            self.assertIn("latest verification command failed", result.final_text)

    def test_change_after_successful_verification_makes_it_stale(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            store = SessionStore.for_workspace(workspace)
            model = FakeModel(
                [
                    ModelResponse(
                        tool_calls=[
                            ToolCall(
                                id="call-first-write",
                                name="write_file",
                                arguments={"path": "result.txt", "content": "one\n"},
                                raw_arguments="{}",
                            )
                        ]
                    ),
                    ModelResponse(
                        tool_calls=[
                            ToolCall(
                                id="call-first-verify",
                                name="run_command",
                                arguments={
                                    "command": 'python -c "print(\'ok\')"',
                                    "purpose": "verify",
                                },
                                raw_arguments="{}",
                            )
                        ]
                    ),
                    ModelResponse(
                        tool_calls=[
                            ToolCall(
                                id="call-later-edit",
                                name="edit_file",
                                arguments={
                                    "path": "result.txt",
                                    "old_text": "one",
                                    "new_text": "two",
                                },
                                raw_arguments="{}",
                            )
                        ]
                    ),
                    ModelResponse(content="Updated after the earlier test."),
                ]
            )
            runner = AgentRunner(
                model=model,
                registry=create_default_registry(),
                config=make_config(workspace),
                approval_callback=lambda tool, arguments: True,
                session_store=store,
            )

            result = runner.run("Create, test, then update result.txt")

            session = store.load(result.session_id or "")
            self.assertEqual(session.status, SessionStatus.COMPLETED_UNVERIFIED)
            self.assertEqual(session.verification_status, VerificationStatus.STALE)
            self.assertIsNotNone(session.verification_records[0].invalidated_at)
            self.assertIn("verification is stale", result.final_text)

    def test_readme_change_does_not_invalidate_unittest_verification(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            store = SessionStore.for_workspace(workspace)
            model = FakeModel(
                [
                    ModelResponse(
                        tool_calls=[
                            ToolCall(
                                id="call-source",
                                name="write_file",
                                arguments={"path": "result.py", "content": "value = 1\n"},
                                raw_arguments="{}",
                            )
                        ]
                    ),
                    ModelResponse(
                        tool_calls=[
                            ToolCall(
                                id="call-tests",
                                name="run_command",
                                arguments={
                                    "command": 'python -c "print(\'ok\')"',
                                    "purpose": "verify",
                                },
                                raw_arguments="{}",
                            )
                        ]
                    ),
                    ModelResponse(
                        tool_calls=[
                            ToolCall(
                                id="call-readme",
                                name="write_file",
                                arguments={"path": "README.md", "content": "# Usage\n"},
                                raw_arguments="{}",
                            )
                        ]
                    ),
                    ModelResponse(content="Code and documentation are complete."),
                ]
            )
            runner = AgentRunner(
                model=model,
                registry=create_default_registry(),
                config=make_config(workspace),
                approval_callback=lambda tool, arguments: True,
                session_store=store,
            )

            result = runner.run("Create tested code and document it")

            session = store.load(result.session_id or "")
            self.assertEqual(result.status, "completed")
            self.assertEqual(session.status, SessionStatus.COMPLETED_VERIFIED)
            self.assertEqual(session.verification_status, VerificationStatus.PASSED)
            self.assertTrue(session.verification_records[0].is_current)

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

    def test_workspace_overview_and_tool_efficiency_metrics_are_persisted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            (workspace / "sample.txt").write_text(
                "\n".join(f"line {index}" for index in range(1, 11)),
                encoding="utf-8",
            )
            (workspace / "pyproject.toml").write_text(
                '[project]\nname="sample"\n',
                encoding="utf-8",
            )
            store = SessionStore.for_workspace(workspace)
            model = FakeModel(
                [
                    ModelResponse(
                        tool_calls=[
                            ToolCall(
                                id="call-read-first",
                                name="read_file",
                                arguments={"path": "sample.txt", "start_line": 1, "max_lines": 5},
                                raw_arguments="{}",
                            )
                        ]
                    ),
                    ModelResponse(
                        tool_calls=[
                            ToolCall(
                                id="call-read-overlap",
                                name="read_file",
                                arguments={"path": "sample.txt", "start_line": 3, "max_lines": 5},
                                raw_arguments="{}",
                            )
                        ]
                    ),
                    ModelResponse(
                        tool_calls=[
                            ToolCall(
                                id="call-read-invalid",
                                name="read_file",
                                arguments={"path": "sample.txt", "surprise": True},
                                raw_arguments="{}",
                            )
                        ]
                    ),
                    ModelResponse(content="Inspection complete."),
                ]
            )
            runner = AgentRunner(
                model=model,
                registry=create_default_registry(),
                config=make_config(workspace),
                session_store=store,
            )

            result = runner.run("Inspect sample.txt efficiently")

            session = store.load(result.session_id or "")
            first_request = model.requests[0][0]
            overview = next(item for item in first_request if item.get("role") == "developer")
            self.assertIn("pyproject.toml", overview["content"])
            self.assertEqual(session.repeated_read_hint_count, 0)
            self.assertEqual(session.observation_cache_hit_count, 1)
            self.assertEqual(session.invalid_tool_call_count, 1)
            self.assertEqual(session.failed_tool_call_count, 1)
            self.assertIn('"partial_cache_hit": true', session.tool_executions[1].result_content or "")
            self.assertIn("invalid_arguments", session.tool_executions[2].result_content or "")
            self.assertIn("observation cache hits: 1", result.final_text)

    def test_repeated_search_uses_observation_cache_and_emits_metric(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            (workspace / "app.py").write_text("value = 1\n", encoding="utf-8")
            store = SessionStore.for_workspace(workspace)
            model = FakeModel(
                [
                    ModelResponse(
                        tool_calls=[
                            ToolCall(
                                id="search-1",
                                name="search_text",
                                arguments={"query": "value"},
                                raw_arguments="{}",
                            )
                        ]
                    ),
                    ModelResponse(
                        tool_calls=[
                            ToolCall(
                                id="search-2",
                                name="search_text",
                                arguments={"query": "VALUE"},
                                raw_arguments="{}",
                            )
                        ]
                    ),
                    ModelResponse(content="Found it."),
                ]
            )
            events: list[tuple[str, dict]] = []
            runner = AgentRunner(
                model=model,
                registry=create_default_registry(),
                config=make_config(workspace),
                session_store=store,
                event_callback=lambda name, payload: events.append((name, payload)),
            )

            result = runner.run("Find value twice")
            session = store.load(result.session_id or "")

            self.assertEqual(session.observation_cache_hit_count, 1)
            self.assertIn('"cache_replay": "summary"', session.tool_executions[-1].result_content or "")
            self.assertTrue(any(name == "observation_cache_hit" for name, _ in events))

    def test_acceptance_context_is_injected_and_identical_verification_is_reused(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            store = SessionStore.for_workspace(workspace)
            verify_arguments = {
                "command": 'python -c "print(\'ok\')"',
                "purpose": "verify",
                "verification_domains": ["python"],
            }
            model = FakeModel(
                [
                    ModelResponse(
                        tool_calls=[
                            ToolCall(
                                id="write",
                                name="write_file",
                                arguments={"path": "app.py", "content": "value = 1\n"},
                                raw_arguments="{}",
                            )
                        ]
                    ),
                    ModelResponse(
                        tool_calls=[
                            ToolCall(
                                id="verify-1",
                                name="run_command",
                                arguments=verify_arguments,
                                raw_arguments="{}",
                            )
                        ]
                    ),
                    ModelResponse(
                        tool_calls=[
                            ToolCall(
                                id="verify-2",
                                name="run_command",
                                arguments=verify_arguments,
                                raw_arguments="{}",
                            )
                        ]
                    ),
                    ModelResponse(content="Done."),
                ]
            )
            events: list[tuple[str, dict]] = []
            runner = AgentRunner(
                model=model,
                registry=create_default_registry(),
                config=make_config(workspace),
                session_store=store,
                approval_callback=lambda tool, arguments: True,
                event_callback=lambda name, payload: events.append((name, payload)),
            )

            result = runner.run("Create app.py; verify it; finish once checks pass.")
            session = store.load(result.session_id or "")

            self.assertEqual(result.status, "completed")
            self.assertEqual(
                session.verification_status,
                VerificationStatus.PASSED,
                msg=[
                    (item.name, item.status.value, item.error, item.result_content)
                    for item in session.tool_executions
                ],
            )
            self.assertEqual(len(session.verification_records), 2)
            self.assertIn("cached_verification", session.tool_executions[-1].result_content or "")
            self.assertTrue(any(name == "verification_reused" for name, _ in events))
            rendered_requests = [str(messages) for messages, _ in model.requests]
            self.assertTrue(any("Runtime acceptance checklist" in item for item in rendered_requests))
            self.assertIn("Acceptance gate: GREEN", rendered_requests[-1])

    def test_volatile_progress_is_appended_after_cacheable_history(self) -> None:
        history = [
            {"role": "system", "content": "stable"},
            {"role": "user", "content": "task"},
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
            {"role": "tool", "tool_call_id": "call-1", "content": "result"},
        ]
        progress = {"role": "developer", "content": "volatile progress"}

        prepared = AgentRunner._insert_progress_context(history, progress)

        self.assertEqual(prepared[:-1], history)
        self.assertEqual(prepared[-1], progress)

    def test_cache_metrics_support_both_provider_accounting_styles(self) -> None:
        included = AgentRunner._cache_metrics(
            {"input_tokens": 10_000, "cached_tokens": 8_000}
        )
        separate = AgentRunner._cache_metrics(
            {"input_tokens": 2_000, "cached_tokens": 8_000}
        )

        self.assertEqual(included["accounting"], "cached_included_in_input")
        self.assertEqual(included["uncached_input_tokens"], 2_000)
        self.assertEqual(included["cache_reuse_ratio"], 0.8)
        self.assertEqual(separate["accounting"], "cached_reported_separately")
        self.assertEqual(separate["effective_input_tokens"], 10_000)
        self.assertEqual(separate["cache_reuse_ratio"], 0.8)


if __name__ == "__main__":
    unittest.main()
