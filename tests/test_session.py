from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mini_coder.agent import AgentRunner
from mini_coder.changes import ChangeTracker
from mini_coder.config import AgentConfig, ApprovalPolicy
from mini_coder.exceptions import ModelError, SessionError
from mini_coder.messages import ModelResponse, ToolCall
from mini_coder.model import ModelClient
from mini_coder.session import (
    CURRENT_SESSION_SCHEMA,
    AgentSession,
    SessionStatus,
    SessionStore,
    ToolExecutionRecord,
    ToolExecutionStatus,
)
from mini_coder.tools import create_default_registry
from mini_coder.verification import VerificationRecord, VerificationStatus


class SequenceModel(ModelClient):
    def __init__(self, items: list[ModelResponse | BaseException]) -> None:
        self.items = list(items)
        self.requests: list[list[dict]] = []

    def complete(self, messages, tools) -> ModelResponse:
        self.requests.append(list(messages))
        item = self.items.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


def make_config(workspace: Path, **overrides) -> AgentConfig:
    values = {
        "workspace": workspace,
        "api_key": "test-secret",
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


def make_session(workspace: Path) -> AgentSession:
    return AgentSession.create(
        task="Inspect the project",
        workspace=workspace,
        model={
            "provider": "test-provider",
            "model": "test-model",
            "wire_api": "responses",
            "reasoning_effort": "high",
            "verbosity": "medium",
            "approval_policy": "safe",
            "max_steps": 10,
        },
        messages=[
            {"role": "system", "content": "system"},
            {"role": "user", "content": "Inspect the project"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call-1",
                        "type": "function",
                        "function": {"name": "list_files", "arguments": "{}"},
                    }
                ],
                "_provider_items": [
                    {
                        "id": "provider-call-1",
                        "type": "function_call",
                        "call_id": "call-1",
                        "name": "list_files",
                        "arguments": "{}",
                    }
                ],
            },
        ],
    )


def make_completed_verified_session(workspace: Path) -> AgentSession:
    path = workspace / "app.py"
    path.write_text('message = "before"\n', encoding="utf-8")
    tracker = ChangeTracker(workspace)
    change = tracker.apply(
        tracker.prepare(
            "edit_file",
            {
                "path": "app.py",
                "old_text": 'message = "before"',
                "new_text": 'message = "old"',
            },
            "execution-initial-change",
        )
    )
    session = AgentSession.create(
        task="Create the initial app",
        workspace=workspace,
        model={"model": "fake"},
        messages=[
            {"role": "system", "content": "system"},
            {"role": "user", "content": "Create the initial app"},
            {"role": "assistant", "content": "Created and verified the initial app."},
        ],
    )
    session.changes.append(change)
    session.change_revision = 1
    session.verification_records.append(
        VerificationRecord.create(
            tool_execution_id="execution-initial-verification",
            command="python -m py_compile app.py",
            cwd=".",
            exit_code=0,
            duration_seconds=0.1,
            stdout_summary="",
            stderr_summary="",
            change_revision=1,
            passed=True,
            timed_out=False,
            scope_paths=("app.py",),
            scope_domains=("python",),
        )
    )
    session.refresh_verification_status()
    session.set_status(SessionStatus.RUNNING)
    session.set_status(SessionStatus.COMPLETED_VERIFIED)
    return session


class SessionModelTests(unittest.TestCase):
    def test_round_trip_preserves_messages_provider_items_and_tool_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            session = make_session(workspace)
            execution = ToolExecutionRecord.create(
                execution_id="execution-1",
                tool_call_id="call-1",
                step=1,
                name="list_files",
                arguments={"path": "."},
                raw_arguments='{"path":"."}',
                risk="read",
            )
            execution.set_status(ToolExecutionStatus.APPROVED)
            execution.set_status(ToolExecutionStatus.RUNNING)
            session.tool_executions.append(execution)
            session.current_step = 1
            session.total_usage = {"input_tokens": 20, "output_tokens": 5}
            session.context_state = {
                "version": 2,
                "generation": 1,
                "checkpoint_hash": "abc123",
            }
            session.set_status(SessionStatus.RUNNING)
            session.set_status(SessionStatus.INTERRUPTED, stop_reason="keyboard_interrupt")

            restored = AgentSession.from_dict(session.to_dict())

            self.assertEqual(restored.session_id, session.session_id)
            self.assertEqual(restored.title, "Inspect the project")
            self.assertEqual(restored.status, SessionStatus.INTERRUPTED)
            self.assertEqual(restored.messages, session.messages)
            self.assertEqual(
                restored.messages[2]["_provider_items"][0]["call_id"],
                "call-1",
            )
            self.assertEqual(restored.tool_executions[0].status, ToolExecutionStatus.RUNNING)
            self.assertEqual(restored.total_usage["input_tokens"], 20)
            self.assertEqual(restored.context_state, session.context_state)

    def test_model_summary_rejects_api_key_fields(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(SessionError):
                AgentSession.create(
                    task="Inspect",
                    workspace=directory,
                    model={"model": "fake", "api_key": "test-secret"},
                )

    def test_rejects_unsupported_schema_version(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data = make_session(Path(directory)).to_dict()
            data["schema_version"] = CURRENT_SESSION_SCHEMA + 1

            with self.assertRaisesRegex(SessionError, "unsupported session schema"):
                AgentSession.from_dict(data)

    def test_migrates_schema_v1_session_without_change_history(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data = make_session(Path(directory)).to_dict()
            data["schema_version"] = 1
            data.pop("changes")
            data.pop("undo_history")
            for execution in data["tool_executions"]:
                execution.pop("prepared_change", None)
                execution.pop("change_id", None)

            restored = AgentSession.from_dict(data)

            self.assertEqual(restored.schema_version, CURRENT_SESSION_SCHEMA)
            self.assertEqual(restored.changes, [])
            self.assertEqual(restored.undo_history, [])

    def test_migrates_schema_v2_session_to_verification_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data = make_session(Path(directory)).to_dict()
            data["schema_version"] = 2
            for name in (
                "phase",
                "verification_status",
                "verification_records",
                "change_revision",
                "run_duration_seconds",
                "retry_count",
            ):
                data.pop(name)

            restored = AgentSession.from_dict(data)

            self.assertEqual(restored.schema_version, CURRENT_SESSION_SCHEMA)
            self.assertEqual(restored.phase.value, "analyze")
            self.assertEqual(restored.verification_status.value, "not_required")
            self.assertEqual(restored.verification_records, [])

    def test_migrates_schema_v3_session_to_budget_counters(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data = make_session(Path(directory)).to_dict()
            data["schema_version"] = 3
            data["total_usage"] = {"input_tokens": 12}
            for name in (
                "model_call_count",
                "usage_missing_count",
                "tool_output_chars",
            ):
                data.pop(name)

            restored = AgentSession.from_dict(data)

            self.assertEqual(restored.schema_version, CURRENT_SESSION_SCHEMA)
            self.assertEqual(restored.model_call_count, 1)
            self.assertEqual(restored.usage_missing_count, 1)
            self.assertEqual(restored.tool_output_chars, 0)
            self.assertEqual(restored.workspace_baseline, {})
            self.assertEqual(restored.failed_tool_call_count, 0)
            self.assertEqual(restored.invalid_tool_call_count, 0)
            self.assertEqual(restored.repeated_read_hint_count, 0)

    def test_migrates_schema_v4_session_to_workspace_metrics(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data = make_session(Path(directory)).to_dict()
            data["schema_version"] = 4
            for name in (
                "workspace_baseline",
                "failed_tool_call_count",
                "invalid_tool_call_count",
                "repeated_read_hint_count",
            ):
                data.pop(name)

            restored = AgentSession.from_dict(data)

            self.assertEqual(restored.schema_version, CURRENT_SESSION_SCHEMA)
            self.assertEqual(restored.workspace_baseline, {})
            self.assertEqual(restored.failed_tool_call_count, 0)
            self.assertEqual(restored.invalid_tool_call_count, 0)
            self.assertEqual(restored.repeated_read_hint_count, 0)

    def test_migrates_schema_v5_session_to_derived_title(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data = make_session(Path(directory)).to_dict()
            data["schema_version"] = 5
            data.pop("title")

            restored = AgentSession.from_dict(data)

            self.assertEqual(restored.schema_version, CURRENT_SESSION_SCHEMA)
            self.assertEqual(restored.title, "Inspect the project")

    def test_migrates_schema_v7_session_to_observation_cache_metrics(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data = make_session(Path(directory)).to_dict()
            data["schema_version"] = 7
            data.pop("observation_cache_hit_count")

            restored = AgentSession.from_dict(data)

            self.assertEqual(restored.schema_version, CURRENT_SESSION_SCHEMA)
            self.assertEqual(restored.observation_cache_hit_count, 0)

    def test_migrates_schema_v8_session_to_parallel_tool_metrics(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data = make_session(Path(directory)).to_dict()
            data["schema_version"] = 8
            for name in (
                "parallel_tool_batches",
                "parallel_tool_calls",
                "parallel_tool_overlap_seconds",
                "parallel_tool_peak_concurrency",
            ):
                data.pop(name)

            restored = AgentSession.from_dict(data)

            self.assertEqual(restored.schema_version, CURRENT_SESSION_SCHEMA)
            self.assertEqual(restored.parallel_tool_batches, 0)
            self.assertEqual(restored.parallel_tool_overlap_seconds, 0.0)

    def test_migrates_schema_v9_session_to_empty_context_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data = make_session(Path(directory)).to_dict()
            data["schema_version"] = 9
            data.pop("context_state")

            restored = AgentSession.from_dict(data)

            self.assertEqual(restored.schema_version, CURRENT_SESSION_SCHEMA)
            self.assertEqual(restored.context_state, {})

    def test_explicit_session_title_round_trips(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            session = AgentSession.create(
                task="Build a small application",
                title="Zero-to-one demo",
                workspace=directory,
                model={"model": "fake"},
            )

            restored = AgentSession.from_dict(session.to_dict())

            self.assertEqual(restored.title, "Zero-to-one demo")

    def test_rejects_invalid_tool_execution_status(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data = make_session(Path(directory)).to_dict()
            data["tool_executions"] = [
                {
                    "execution_id": "execution-1",
                    "tool_call_id": "call-1",
                    "step": 1,
                    "name": "read_file",
                    "arguments": {"path": "README.md"},
                    "raw_arguments": '{"path":"README.md"}',
                    "risk": "read",
                    "status": "made-up",
                    "result_content": None,
                    "ok": None,
                    "error": None,
                    "created_at": "2026-08-27T00:00:00Z",
                    "updated_at": "2026-08-27T00:00:00Z",
                }
            ]

            with self.assertRaisesRegex(SessionError, "unsupported tool execution status"):
                AgentSession.from_dict(data)

    def test_rejects_invalid_state_transitions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            session = make_session(Path(directory))
            with self.assertRaisesRegex(SessionError, "invalid session status transition"):
                session.set_status(SessionStatus.COMPLETED_UNVERIFIED)

            execution = ToolExecutionRecord.create(
                execution_id="execution-transition",
                tool_call_id="call-transition",
                step=1,
                name="read_file",
                arguments={"path": "README.md"},
                raw_arguments='{"path":"README.md"}',
                risk="read",
            )
            execution.set_status(ToolExecutionStatus.FAILED)
            with self.assertRaisesRegex(SessionError, "invalid tool execution status transition"):
                execution.set_status(ToolExecutionStatus.RUNNING)

    def test_completed_verified_requires_consistent_local_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            session = make_session(Path(directory))
            session.set_status(SessionStatus.RUNNING)

            with self.assertRaisesRegex(SessionError, "current successful verification"):
                session.set_status(SessionStatus.COMPLETED_VERIFIED)

            data = session.to_dict()
            data["verification_status"] = "passed"
            with self.assertRaisesRegex(SessionError, "inconsistent"):
                AgentSession.from_dict(data)


class SessionStoreTests(unittest.TestCase):
    def test_save_and_load_from_workspace_store(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            store = SessionStore.for_workspace(workspace)
            session = make_session(workspace)

            path = store.save(session)
            restored = store.load(session.session_id)

            expected = (
                workspace.resolve()
                / ".mini-coder"
                / "sessions"
                / f"{session.session_id}.json"
            )
            self.assertEqual(path, expected)
            self.assertEqual(restored.to_dict(), session.to_dict())

    def test_atomic_replace_failure_preserves_previous_session(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            store = SessionStore.for_workspace(workspace)
            session = make_session(workspace)
            path = store.save(session)
            original = path.read_text(encoding="utf-8")
            session.set_status(SessionStatus.RUNNING)

            with patch("mini_coder.session.store.os.replace", side_effect=OSError("blocked")):
                with self.assertRaisesRegex(SessionError, "cannot save session"):
                    store.save(session)

            self.assertEqual(path.read_text(encoding="utf-8"), original)
            self.assertEqual(list(store.root.glob("*.tmp")), [])

    def test_atomic_replace_retries_brief_permission_errors(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            store = SessionStore.for_workspace(workspace)
            session = make_session(workspace)
            real_replace = os.replace
            attempts = 0

            def flaky_replace(source: str | Path, destination: str | Path) -> None:
                nonlocal attempts
                attempts += 1
                if attempts < 3:
                    raise PermissionError(5, "temporarily locked")
                real_replace(source, destination)

            with (
                patch("mini_coder.session.store.os.replace", side_effect=flaky_replace),
                patch("mini_coder.session.store.time.sleep") as sleep,
            ):
                path = store.save(session)

            self.assertTrue(path.is_file())
            self.assertEqual(attempts, 3)
            self.assertEqual(sleep.call_count, 2)

    def test_load_rejects_corrupt_json(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SessionStore(Path(directory))
            path = store.path_for("broken")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("{not-json", encoding="utf-8")

            with self.assertRaisesRegex(SessionError, "not valid JSON"):
                store.load("broken")

    def test_load_rejects_file_name_that_does_not_match_session_id(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            store = SessionStore(workspace / "sessions")
            session = make_session(workspace)
            mismatched = store.root / "different.json"
            mismatched.parent.mkdir(parents=True)
            mismatched.write_text(
                json.dumps(session.to_dict(), ensure_ascii=False),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(SessionError, "does not match file name"):
                store.load(mismatched)

    def test_delete_removes_only_the_requested_session_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            store = SessionStore.for_workspace(workspace)
            first = make_session(workspace)
            second = make_session(workspace)
            store.save(first)
            store.save(second)

            deleted = store.delete(first.session_id)

            self.assertEqual(deleted.session_id, first.session_id)
            self.assertFalse(store.path_for(first.session_id).exists())
            self.assertTrue(store.path_for(second.session_id).exists())
            with self.assertRaisesRegex(SessionError, "does not exist"):
                store.delete(first.session_id)


class SessionRunnerTests(unittest.TestCase):
    def test_user_cancellation_is_persisted_as_resumable_interruption(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            store = SessionStore.for_workspace(workspace)
            cancelled_runner = AgentRunner(
                model=SequenceModel([]),
                registry=create_default_registry(),
                config=make_config(workspace),
                session_store=store,
                cancellation_callback=lambda: True,
            )

            cancelled = cancelled_runner.run("Inspect the project")
            saved = store.load(cancelled.session_id or "")

            self.assertEqual(cancelled.status, "cancelled")
            self.assertEqual(saved.status, SessionStatus.INTERRUPTED)
            self.assertEqual(saved.stop_reason, "user_cancelled")

            resumed_runner = AgentRunner(
                model=SequenceModel([ModelResponse(content="Finished after resuming.")]),
                registry=create_default_registry(),
                config=make_config(workspace),
                session_store=store,
            )
            resumed = resumed_runner.run("Continue from the saved point", session=saved)

            self.assertEqual(resumed.status, "completed")
            self.assertEqual(store.load(saved.session_id).turn_count, 2)

    def test_completed_session_accepts_follow_up_with_compact_memory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            store = SessionStore.for_workspace(workspace)
            first_model = SequenceModel(
                [ModelResponse(content="First turn complete.", usage={"total_tokens": 10})]
            )
            first_runner = AgentRunner(
                model=first_model,
                registry=create_default_registry(),
                config=make_config(workspace),
                session_store=store,
            )

            first = first_runner.run("Create the initial version")
            session = store.load(first.session_id or "")
            session.messages.insert(
                -1,
                {"role": "tool", "tool_call_id": "old-call", "content": "old output " * 500},
            )
            session.context_state = {
                "version": 2,
                "generation": 9,
                "checkpoint_hash": "old-turn-checkpoint",
            }
            store.save(session)
            second_model = SequenceModel(
                [ModelResponse(content="Second turn complete.", usage={"total_tokens": 12})]
            )
            second_runner = AgentRunner(
                model=second_model,
                registry=create_default_registry(),
                config=make_config(workspace),
                session_store=store,
            )

            result = second_runner.run(
                "Now explain the previous implementation", session=session
            )
            restored = store.load(session.session_id)
            rendered_request = str(second_model.requests[0])

            self.assertEqual(result.status, "completed")
            self.assertEqual(restored.turn_count, 2)
            self.assertEqual(restored.task, "Create the initial version")
            self.assertEqual(
                [item["role"] for item in restored.conversation],
                ["user", "assistant", "user", "assistant"],
            )
            self.assertIn("Previous turn state", rendered_request)
            self.assertIn("Now explain the previous implementation", rendered_request)
            self.assertNotIn("old output", rendered_request)
            self.assertIn("turn_state", restored.working_memory)
            self.assertNotIn("task_brief", restored.working_memory)
            self.assertNotIn("task_ledger", restored.working_memory)
            self.assertEqual(restored.total_usage["total_tokens"], 22)
            self.assertEqual([item["turn"] for item in restored.model_call_records], [1, 2])
            self.assertFalse(restored.model_call_records[-1]["compacted"])
            self.assertEqual(restored.context_state.get("generation"), 0)
            self.assertEqual(
                restored.context_state.get("boundary_reason"), "new_turn"
            )
            self.assertNotIn("old-turn-checkpoint", str(restored.context_state))

    def test_mutating_follow_up_without_change_is_corrected_then_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            store = SessionStore.for_workspace(workspace)
            session = make_completed_verified_session(workspace)
            store.save(session)
            events: list[tuple[str, dict]] = []
            model = SequenceModel(
                [
                    ModelResponse(content="I found the greeting that needs changing."),
                    ModelResponse(content="I still did not modify the file."),
                ]
            )
            runner = AgentRunner(
                model=model,
                registry=create_default_registry(),
                config=make_config(workspace),
                event_callback=lambda name, payload: events.append((name, payload)),
                session_store=store,
            )

            result = runner.run("Fix the greeting in app.py", session=session)

            restored = store.load(session.session_id)
            self.assertEqual(result.status, "incomplete")
            self.assertEqual(restored.status, SessionStatus.FAILED)
            self.assertEqual(restored.stop_reason, "mutation_not_implemented")
            self.assertEqual(restored.verification_status, VerificationStatus.STALE)
            self.assertEqual(len(model.requests), 2)
            self.assertIn("Runtime completion gate", str(model.requests[1]))
            self.assertTrue(
                any(
                    name == "completion_blocked_no_turn_change"
                    for name, _ in events
                )
            )
            self.assertEqual(
                restored.verification_records[0].invalidation_reason,
                "new_user_turn",
            )

    def test_mutating_follow_up_change_without_new_check_is_unverified(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            store = SessionStore.for_workspace(workspace)
            session = make_completed_verified_session(workspace)
            store.save(session)
            model = SequenceModel(
                [
                    ModelResponse(
                        tool_calls=[
                            ToolCall(
                                id="call-follow-up-edit",
                                name="edit_file",
                                arguments={
                                    "path": "app.py",
                                    "old_text": 'message = "old"',
                                    "new_text": 'message = "new"',
                                },
                                raw_arguments=(
                                    '{"path":"app.py","old_text":"message = '
                                    '\\"old\\"","new_text":"message = '
                                    '\\"new\\""}'
                                ),
                            )
                        ]
                    ),
                    ModelResponse(content="Updated the greeting."),
                ]
            )
            runner = AgentRunner(
                model=model,
                registry=create_default_registry(),
                config=make_config(workspace),
                session_store=store,
            )

            result = runner.run("Fix the greeting in app.py", session=session)

            restored = store.load(session.session_id)
            self.assertEqual(result.status, "completed")
            self.assertEqual(restored.status, SessionStatus.COMPLETED_UNVERIFIED)
            self.assertEqual(restored.verification_status, VerificationStatus.STALE)
            self.assertEqual(restored.change_revision, 2)
            self.assertEqual(
                (workspace / "app.py").read_text(encoding="utf-8"),
                'message = "new"\n',
            )

    def test_mutating_follow_up_change_and_new_check_is_verified(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            store = SessionStore.for_workspace(workspace)
            session = make_completed_verified_session(workspace)
            store.save(session)
            model = SequenceModel(
                [
                    ModelResponse(
                        tool_calls=[
                            ToolCall(
                                id="call-follow-up-edit-verified",
                                name="edit_file",
                                arguments={
                                    "path": "app.py",
                                    "old_text": 'message = "old"',
                                    "new_text": 'message = "new"',
                                },
                                raw_arguments=(
                                    '{"path":"app.py","old_text":"message = '
                                    '\\"old\\"","new_text":"message = '
                                    '\\"new\\""}'
                                ),
                            )
                        ]
                    ),
                    ModelResponse(
                        tool_calls=[
                            ToolCall(
                                id="call-follow-up-verify",
                                name="run_command",
                                arguments={
                                    "command": "python -m py_compile app.py",
                                    "cwd": ".",
                                    "purpose": "verify",
                                    "verification_paths": ["app.py"],
                                },
                                raw_arguments=(
                                    '{"command":"python -m py_compile app.py",'
                                    '"cwd":".","purpose":"verify",'
                                    '"verification_paths":["app.py"]}'
                                ),
                            )
                        ]
                    ),
                    ModelResponse(content="Updated and verified the greeting."),
                ]
            )
            runner = AgentRunner(
                model=model,
                registry=create_default_registry(),
                config=make_config(workspace),
                session_store=store,
            )

            result = runner.run("Fix the greeting in app.py", session=session)

            restored = store.load(session.session_id)
            self.assertEqual(result.status, "completed")
            self.assertEqual(restored.status, SessionStatus.COMPLETED_VERIFIED)
            self.assertEqual(restored.verification_status, VerificationStatus.PASSED)
            self.assertEqual(restored.change_revision, 2)
            self.assertEqual(
                restored.verification_records[-1].change_revision,
                restored.change_revision,
            )

    def test_resume_invalidates_verification_after_external_file_change(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            path = workspace / "tracked.txt"
            path.write_text("before\n", encoding="utf-8")
            tracker = ChangeTracker(workspace)
            change = tracker.apply(
                tracker.prepare(
                    "edit_file",
                    {"path": "tracked.txt", "old_text": "before", "new_text": "agent"},
                    "execution-external-change",
                )
            )
            session = AgentSession.create(
                task="Update and test tracked.txt",
                workspace=workspace,
                model={"model": "fake"},
                messages=[
                    {"role": "system", "content": "system"},
                    {"role": "user", "content": "Update and test tracked.txt"},
                    {"role": "assistant", "content": "Completed and tested."},
                ],
            )
            session.changes.append(change)
            session.change_revision = 1
            session.verification_records.append(
                VerificationRecord.create(
                    tool_execution_id="execution-verify-before-external-change",
                    command="python -m unittest",
                    cwd=".",
                    exit_code=0,
                    duration_seconds=0.1,
                    stdout_summary="",
                    stderr_summary="OK",
                    change_revision=1,
                    passed=True,
                    timed_out=False,
                )
            )
            session.refresh_verification_status()
            session.set_status(SessionStatus.RUNNING)
            session.set_status(SessionStatus.COMPLETED_VERIFIED)
            store = SessionStore.for_workspace(workspace)
            store.save(session)
            path.write_text("external\n", encoding="utf-8")
            model = SequenceModel(
                [ModelResponse(content="The external edit has not been verified.")]
            )
            runner = AgentRunner(
                model=model,
                registry=create_default_registry(),
                config=make_config(workspace),
                session_store=store,
            )

            result = runner.run("", session=store.load(session.session_id))

            restored = store.load(session.session_id)
            self.assertEqual(result.status, "completed")
            self.assertEqual(restored.status, SessionStatus.COMPLETED_UNVERIFIED)
            self.assertEqual(restored.verification_status, VerificationStatus.STALE)
            self.assertEqual(restored.change_revision, 2)
            self.assertTrue(
                any(
                    "Local runtime notice" in str(message.get("content") or "")
                    for message in model.requests[0]
                )
            )

    def test_resume_uses_persisted_approved_change_and_detects_conflict(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            path = workspace / "approved.txt"
            path.write_text("before\n", encoding="utf-8")
            store = SessionStore.for_workspace(workspace)
            session = AgentSession.create(
                task="Edit approved.txt",
                workspace=workspace,
                model={"model": "fake"},
                messages=[
                    {"role": "system", "content": "system"},
                    {"role": "user", "content": "Edit approved.txt"},
                    {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [
                            {
                                "id": "call-approved",
                                "type": "function",
                                "function": {
                                    "name": "edit_file",
                                    "arguments": (
                                        '{"path":"approved.txt","old_text":"before",'
                                        '"new_text":"agent"}'
                                    ),
                                },
                            }
                        ],
                    },
                ],
            )
            session.set_status(SessionStatus.RUNNING)
            session.current_step = 1
            execution = ToolExecutionRecord.create(
                execution_id="execution-approved",
                tool_call_id="call-approved",
                step=1,
                name="edit_file",
                arguments={"path": "approved.txt", "old_text": "before", "new_text": "agent"},
                raw_arguments=(
                    '{"path":"approved.txt","old_text":"before","new_text":"agent"}'
                ),
                risk="write",
            )
            execution.prepared_change = ChangeTracker(workspace).prepare(
                "edit_file",
                execution.arguments or {},
                execution.execution_id,
            )
            execution.approval_granted = True
            execution.set_status(ToolExecutionStatus.APPROVED)
            session.tool_executions.append(execution)
            session.set_status(SessionStatus.INTERRUPTED, stop_reason="process_ended")
            store.save(session)
            path.write_text("user change\n", encoding="utf-8")
            restored = store.load(session.session_id)
            model = SequenceModel([ModelResponse(content="The saved edit conflicted.")])
            runner = AgentRunner(
                model=model,
                registry=create_default_registry(),
                config=make_config(workspace),
                session_store=store,
            )

            result = runner.run("", session=restored)

            self.assertEqual(result.status, "completed")
            self.assertEqual(path.read_text(encoding="utf-8"), "user change\n")
            self.assertEqual(restored.tool_executions[0].status, ToolExecutionStatus.FAILED)
            self.assertIn("changed after approval", restored.tool_executions[0].error or "")
            self.assertEqual(restored.changes, [])

    def test_model_error_redacts_configured_api_key_from_session_and_events(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            store = SessionStore.for_workspace(workspace)
            events: list[tuple[str, dict]] = []
            model = SequenceModel([ModelError("request failed with test-secret")])
            runner = AgentRunner(
                model=model,
                registry=create_default_registry(),
                config=make_config(workspace),
                event_callback=lambda name, payload: events.append((name, payload)),
                session_store=store,
            )

            result = runner.run("Inspect the project")

            self.assertEqual(result.status, "model_error")
            session_path = next(store.root.glob("*.json"))
            raw = session_path.read_text(encoding="utf-8")
            self.assertNotIn("test-secret", raw)
            self.assertIn("[REDACTED]", raw)
            model_errors = [payload for name, payload in events if name == "model_error"]
            self.assertEqual(len(model_errors), 1)
            self.assertNotIn("test-secret", model_errors[0]["error"])

    def test_resume_does_not_replay_a_completed_write(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            store = SessionStore.for_workspace(workspace)
            session = AgentSession.create(
                task="Write result.txt",
                workspace=workspace,
                model={"model": "fake"},
                messages=[
                    {"role": "system", "content": "system"},
                    {"role": "user", "content": "Write result.txt"},
                    {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [
                            {
                                "id": "call-completed-write",
                                "type": "function",
                                "function": {
                                    "name": "write_file",
                                    "arguments": '{"path":"result.txt","content":"first"}',
                                },
                            }
                        ],
                    },
                    {
                        "role": "tool",
                        "tool_call_id": "call-completed-write",
                        "content": "Wrote result.txt",
                    },
                ],
            )
            session.set_status(SessionStatus.RUNNING)
            session.current_step = 1
            execution = ToolExecutionRecord.create(
                execution_id="execution-completed-write",
                tool_call_id="call-completed-write",
                step=1,
                name="write_file",
                arguments={"path": "result.txt", "content": "first"},
                raw_arguments='{"path":"result.txt","content":"first"}',
                risk="write",
            )
            execution.approval_granted = True
            execution.set_status(ToolExecutionStatus.APPROVED)
            execution.set_status(ToolExecutionStatus.RUNNING)
            execution.result_content = "Wrote result.txt"
            execution.ok = True
            execution.set_status(ToolExecutionStatus.COMPLETED)
            session.tool_executions.append(execution)
            session.set_status(SessionStatus.INTERRUPTED, stop_reason="process_ended")
            store.save(session)
            registry = create_default_registry()
            model = SequenceModel([ModelResponse(content="The file was written.")])
            runner = AgentRunner(
                model=model,
                registry=registry,
                config=make_config(workspace),
                session_store=store,
            )

            with patch.object(
                registry,
                "execute",
                side_effect=AssertionError("completed write was replayed"),
            ) as execute:
                result = runner.run("", session=session)

            self.assertEqual(result.status, "completed")
            execute.assert_not_called()
            self.assertEqual(len(session.tool_executions), 1)

    def test_resume_rejects_a_missing_saved_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            workspace = parent / "workspace"
            workspace.mkdir()
            session = AgentSession.create(
                task="Inspect the project",
                workspace=workspace,
                model={"model": "fake"},
                messages=[
                    {"role": "system", "content": "system"},
                    {"role": "user", "content": "Inspect the project"},
                ],
            )
            session.set_status(SessionStatus.RUNNING)
            session.set_status(SessionStatus.INTERRUPTED, stop_reason="process_ended")
            runner = AgentRunner(
                model=SequenceModel([]),
                registry=create_default_registry(),
                config=make_config(workspace),
            )
            workspace.rmdir()

            with self.assertRaisesRegex(SessionError, "workspace no longer exists"):
                runner.run("", session=session)

    def test_keyboard_interrupt_persists_and_resume_does_not_repeat_completed_tool(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            (workspace / "hello.txt").write_text("hello\n", encoding="utf-8")
            store = SessionStore.for_workspace(workspace)
            first_model = SequenceModel(
                [
                    ModelResponse(
                        tool_calls=[
                            ToolCall(
                                id="call-read",
                                name="read_file",
                                arguments={"path": "hello.txt"},
                                raw_arguments='{"path":"hello.txt"}',
                            )
                        ]
                    ),
                    KeyboardInterrupt(),
                ]
            )
            runner = AgentRunner(
                model=first_model,
                registry=create_default_registry(),
                config=make_config(workspace),
                session_store=store,
            )

            with self.assertRaises(KeyboardInterrupt):
                runner.run("Read hello.txt and summarize it")

            session_files = list(store.root.glob("*.json"))
            self.assertEqual(len(session_files), 1)
            interrupted = store.load(session_files[0])
            self.assertEqual(interrupted.status, SessionStatus.INTERRUPTED)
            self.assertEqual(interrupted.current_step, 1)
            self.assertEqual(len(interrupted.tool_executions), 1)
            self.assertEqual(
                interrupted.tool_executions[0].status,
                ToolExecutionStatus.COMPLETED,
            )
            self.assertEqual(
                len([message for message in interrupted.messages if message["role"] == "tool"]),
                1,
            )
            self.assertNotIn("test-secret", session_files[0].read_text(encoding="utf-8"))

            resumed_model = SequenceModel([ModelResponse(content="The file contains hello.")])
            resumed_runner = AgentRunner(
                model=resumed_model,
                registry=create_default_registry(),
                config=make_config(workspace),
                session_store=store,
            )
            result = resumed_runner.run("", session=interrupted)

            self.assertEqual(result.status, "completed")
            self.assertEqual(result.steps, 2)
            self.assertEqual(len(interrupted.tool_executions), 1)
            sent_tool_messages = [
                message for message in resumed_model.requests[0] if message["role"] == "tool"
            ]
            self.assertEqual(len(sent_tool_messages), 1)
            completed = store.load(interrupted.session_id)
            self.assertEqual(completed.status, SessionStatus.COMPLETED_UNVERIFIED)

    def test_resume_executes_requested_but_not_started_read_tool(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            (workspace / "hello.txt").write_text("hello\n", encoding="utf-8")
            store = SessionStore.for_workspace(workspace)
            session = AgentSession.create(
                task="Read hello.txt",
                workspace=workspace,
                model={"model": "fake"},
                messages=[
                    {"role": "system", "content": "system"},
                    {"role": "user", "content": "Read hello.txt"},
                    {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [
                            {
                                "id": "call-pending",
                                "type": "function",
                                "function": {
                                    "name": "read_file",
                                    "arguments": '{"path":"hello.txt"}',
                                },
                            }
                        ],
                    },
                ],
            )
            session.set_status(SessionStatus.RUNNING)
            session.current_step = 1
            session.tool_executions.append(
                ToolExecutionRecord.create(
                    execution_id="execution-pending",
                    tool_call_id="call-pending",
                    step=1,
                    name="read_file",
                    arguments={"path": "hello.txt"},
                    raw_arguments='{"path":"hello.txt"}',
                    risk="read",
                )
            )
            session.set_status(SessionStatus.INTERRUPTED, stop_reason="process_ended")
            store.save(session)
            model = SequenceModel([ModelResponse(content="Read successfully.")])
            runner = AgentRunner(
                model=model,
                registry=create_default_registry(),
                config=make_config(workspace),
                session_store=store,
            )

            result = runner.run("", session=session)

            self.assertEqual(result.status, "completed")
            self.assertEqual(session.tool_executions[0].status, ToolExecutionStatus.COMPLETED)
            self.assertIn("hello", session.tool_executions[0].result_content or "")
            self.assertEqual(
                len([message for message in model.requests[0] if message["role"] == "tool"]),
                1,
            )

    def test_resume_marks_running_tool_uncertain_without_calling_model(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            store = SessionStore.for_workspace(workspace)
            session = AgentSession.create(
                task="Write a file",
                workspace=workspace,
                model={"model": "fake"},
                messages=[
                    {"role": "system", "content": "system"},
                    {"role": "user", "content": "Write a file"},
                    {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [
                            {
                                "id": "call-write",
                                "type": "function",
                                "function": {
                                    "name": "write_file",
                                    "arguments": '{"path":"result.txt","content":"data"}',
                                },
                            }
                        ],
                    },
                ],
            )
            session.set_status(SessionStatus.RUNNING)
            session.current_step = 1
            execution = ToolExecutionRecord.create(
                execution_id="execution-running",
                tool_call_id="call-write",
                step=1,
                name="write_file",
                arguments={"path": "result.txt", "content": "data"},
                raw_arguments='{"path":"result.txt","content":"data"}',
                risk="write",
            )
            execution.set_status(ToolExecutionStatus.APPROVED)
            execution.set_status(ToolExecutionStatus.RUNNING)
            session.tool_executions.append(execution)
            store.save(session)
            model = SequenceModel([])
            runner = AgentRunner(
                model=model,
                registry=create_default_registry(),
                config=make_config(workspace),
                session_store=store,
            )

            result = runner.run("", session=session)

            self.assertEqual(result.status, "resume_attention")
            self.assertEqual(execution.status, ToolExecutionStatus.UNCERTAIN)
            self.assertEqual(model.requests, [])
            self.assertFalse((workspace / "result.txt").exists())
            restored = store.load(session.session_id)
            self.assertEqual(restored.status, SessionStatus.INTERRUPTED)
            self.assertEqual(restored.stop_reason, "uncertain_tool_execution")

    def test_denied_approval_is_persisted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            store = SessionStore.for_workspace(workspace)
            model = SequenceModel(
                [
                    ModelResponse(
                        tool_calls=[
                            ToolCall(
                                id="call-write",
                                name="write_file",
                                arguments={"path": "result.txt", "content": "data"},
                                raw_arguments='{"path":"result.txt","content":"data"}',
                            )
                        ]
                    ),
                    ModelResponse(content="The write was denied."),
                ]
            )
            runner = AgentRunner(
                model=model,
                registry=create_default_registry(),
                config=make_config(workspace, approval_policy=ApprovalPolicy.SAFE),
                approval_callback=lambda tool, arguments: False,
                session_store=store,
            )

            result = runner.run("Write result.txt")

            self.assertEqual(result.status, "denied")
            restored = store.load(result.session_id or "")
            self.assertEqual(restored.status, SessionStatus.DENIED)
            execution = restored.tool_executions[0]
            self.assertEqual(execution.status, ToolExecutionStatus.DENIED)
            self.assertFalse(execution.approval_granted)
            self.assertFalse((workspace / "result.txt").exists())


if __name__ == "__main__":
    unittest.main()
