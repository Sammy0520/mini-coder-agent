from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from mini_coder.changes import ChangeTracker
from mini_coder.cli import (
    _load_resume_session,
    _event_handler,
    _resolve_uncertain_tools,
    _validate_resume_model,
    build_parser,
    main,
)
from mini_coder.config import AgentConfig, WireAPI
from mini_coder.exceptions import ConfigurationError, SessionError
from mini_coder.messages import ModelResponse
from mini_coder.model import ModelClient
from mini_coder.session import (
    AgentSession,
    SessionStatus,
    SessionStore,
    ToolExecutionRecord,
    ToolExecutionStatus,
)
from mini_coder.verification import VerificationRecord, VerificationStatus


class FinalModel(ModelClient):
    def complete(self, messages, tools) -> ModelResponse:
        return ModelResponse(content="Completed without tools.")


class CliSessionTests(unittest.TestCase):
    def test_event_log_failure_warns_once_without_raising(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            blocking_parent = root / "not-a-directory"
            blocking_parent.write_text("file", encoding="utf-8")
            handler = _event_handler(blocking_parent / "events.jsonl")
            errors = io.StringIO()

            with redirect_stderr(errors):
                handler("run_started", {"session_id": "session-log"})
                handler("run_completed", {"session_id": "session-log"})

            rendered = errors.getvalue()
            self.assertEqual(rendered.count("event log could not be written"), 1)

    def test_cli_undo_last_is_local_persisted_and_does_not_require_api_key(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            workspace.mkdir()
            tracker = ChangeTracker(workspace)
            path = workspace / "undo.txt"
            path.write_text("before\n", encoding="utf-8")
            change = tracker.apply(
                tracker.prepare(
                    "edit_file",
                    {"path": "undo.txt", "old_text": "before", "new_text": "after"},
                    "execution-cli-undo",
                )
            )
            session = AgentSession.create(
                task="Edit undo.txt",
                workspace=workspace,
                model={"model": "fake"},
                messages=[
                    {"role": "system", "content": "system"},
                    {"role": "user", "content": "Edit undo.txt"},
                ],
            )
            session.changes.append(change)
            session.change_revision = 1
            session.verification_records.append(
                VerificationRecord.create(
                    tool_execution_id="execution-cli-verify",
                    command="python -m unittest",
                    cwd=".",
                    exit_code=0,
                    duration_seconds=0.25,
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
            session_path = store.save(session)
            log_path = root / "undo-events.jsonl"
            output = io.StringIO()

            with patch.dict("os.environ", {}, clear=True), redirect_stdout(output):
                exit_code = main(
                    [
                        "--resume",
                        str(session_path),
                        "--undo-last",
                        "--log",
                        str(log_path),
                    ]
                )

            self.assertEqual(exit_code, 0)
            self.assertEqual(path.read_text(encoding="utf-8"), "before\n")
            restored = store.load(session.session_id)
            self.assertEqual(restored.changes[0].undo_status, "undone")
            self.assertEqual(len(restored.undo_history), 1)
            self.assertEqual(restored.status, SessionStatus.INTERRUPTED)
            self.assertEqual(restored.verification_status, VerificationStatus.STALE)
            self.assertIsNotNone(restored.verification_records[0].invalidated_at)
            event = json.loads(log_path.read_text(encoding="utf-8").splitlines()[-1])
            self.assertEqual(event["event"], "change_undone")
            self.assertIn("Undid tracked change", output.getvalue())

    def test_cli_show_changes_prints_diff_without_loading_model_config(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            tracker = ChangeTracker(workspace)
            change = tracker.apply(
                tracker.prepare(
                    "write_file",
                    {"path": "shown.txt", "content": "shown\n"},
                    "execution-show",
                )
            )
            session = AgentSession.create(
                task="Create shown.txt",
                workspace=workspace,
                model={"model": "fake"},
                messages=[
                    {"role": "system", "content": "system"},
                    {"role": "user", "content": "Create shown.txt"},
                ],
            )
            session.changes.append(change)
            session.invalidate_verification("file changed: shown.txt")
            store = SessionStore.for_workspace(workspace)
            session_path = store.save(session)
            output = io.StringIO()

            with patch.dict("os.environ", {}, clear=True), redirect_stdout(output):
                exit_code = main(["--resume", str(session_path), "--show-changes"])

            self.assertEqual(exit_code, 0)
            rendered = output.getvalue()
            self.assertIn("shown.txt [active] +1/-0", rendered)
            self.assertIn("+++ b/shown.txt", rendered)

    def test_resolves_uncertain_tool_only_after_explicit_user_choice(self) -> None:
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
                                "id": "call-uncertain",
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
            execution = ToolExecutionRecord.create(
                execution_id="execution-uncertain",
                tool_call_id="call-uncertain",
                step=1,
                name="write_file",
                arguments={"path": "result.txt", "content": "data"},
                raw_arguments='{"path":"result.txt","content":"data"}',
                risk="write",
            )
            execution.set_status(ToolExecutionStatus.APPROVED)
            execution.set_status(ToolExecutionStatus.RUNNING)
            execution.set_status(ToolExecutionStatus.UNCERTAIN)
            session.tool_executions.append(execution)
            session.set_status(SessionStatus.INTERRUPTED, stop_reason="uncertain_tool_execution")
            store.save(session)
            events: list[tuple[str, dict]] = []

            _resolve_uncertain_tools(
                session,
                ["execution-uncertain=completed"],
                store,
                lambda name, payload: events.append((name, payload)),
            )

            restored = store.load(session.session_id)
            self.assertEqual(
                restored.tool_executions[0].status,
                ToolExecutionStatus.COMPLETED,
            )
            self.assertTrue(restored.tool_executions[0].ok)
            self.assertEqual(restored.messages[-1]["role"], "tool")
            self.assertEqual(restored.messages[-1]["tool_call_id"], "call-uncertain")
            self.assertEqual(events[0][0], "uncertain_resolved")

    def test_rejects_resolution_for_a_non_uncertain_tool(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            store = SessionStore.for_workspace(workspace)
            session = AgentSession.create(
                task="Inspect",
                workspace=workspace,
                model={"model": "fake"},
                messages=[
                    {"role": "system", "content": "system"},
                    {"role": "user", "content": "Inspect"},
                ],
            )
            execution = ToolExecutionRecord.create(
                execution_id="execution-requested",
                tool_call_id="call-requested",
                step=1,
                name="read_file",
                arguments={"path": "README.md"},
                raw_arguments='{"path":"README.md"}',
                risk="read",
            )
            session.tool_executions.append(execution)

            with self.assertRaisesRegex(SessionError, "not uncertain"):
                _resolve_uncertain_tools(
                    session,
                    ["execution-requested=failed"],
                    store,
                    lambda name, payload: None,
                )

    def test_parser_accepts_resume_without_a_task(self) -> None:
        args = build_parser().parse_args(["--resume", "session-1"])

        self.assertEqual(args.resume, "session-1")
        self.assertEqual(args.task, [])
        self.assertIsNone(args.workspace)

    def test_loads_resume_from_explicit_path_and_uses_saved_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            workspace.mkdir()
            store = SessionStore(root / "sessions")
            session = AgentSession.create(
                task="Inspect",
                workspace=workspace,
                model={"provider": "test", "model": "fake", "wire_api": "responses"},
                messages=[
                    {"role": "system", "content": "system"},
                    {"role": "user", "content": "Inspect"},
                ],
            )
            path = store.save(session)

            restored, restored_store = _load_resume_session(str(path), None)

            self.assertIsNotNone(restored)
            self.assertEqual(restored.session_id, session.session_id)
            self.assertEqual(restored.workspace, str(workspace.resolve()))
            self.assertEqual(restored_store.root, path.parent.resolve())

    def test_resume_rejects_explicit_workspace_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            other = root / "other"
            workspace.mkdir()
            other.mkdir()
            store = SessionStore(root / "sessions")
            path = store.save(
                AgentSession.create(
                    task="Inspect",
                    workspace=workspace,
                    model={"model": "fake"},
                    messages=[
                        {"role": "system", "content": "system"},
                        {"role": "user", "content": "Inspect"},
                    ],
                )
            )

            with self.assertRaisesRegex(SessionError, "does not match"):
                _load_resume_session(str(path), str(other))

    def test_resume_rejects_model_or_wire_api_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            session = AgentSession.create(
                task="Inspect",
                workspace=workspace,
                model={
                    "provider": "provider-a",
                    "model": "model-a",
                    "wire_api": "responses",
                },
                messages=[
                    {"role": "system", "content": "system"},
                    {"role": "user", "content": "Inspect"},
                ],
            )
            config = AgentConfig(
                workspace=workspace,
                api_key="test",
                base_url=None,
                model="model-b",
                model_provider="provider-a",
                wire_api=WireAPI.CHAT_COMPLETIONS,
            )

            with self.assertRaisesRegex(ConfigurationError, "different model/provider"):
                _validate_resume_model(session, config)

    def test_main_persists_a_new_session_without_storing_api_key(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            workspace.mkdir()
            config_path = root / "agent.toml"
            config_path.write_text('model = "fake"\n', encoding="utf-8")
            log_path = root / "events.jsonl"
            output = io.StringIO()

            with (
                patch.dict("os.environ", {"CODING_AGENT_API_KEY": "cli-test-secret"}, clear=True),
                patch("mini_coder.cli.OpenAICompatibleClient", return_value=FinalModel()),
                redirect_stdout(output),
            ):
                exit_code = main(
                    [
                        "--workspace",
                        str(workspace),
                        "--config",
                        str(config_path),
                        "--log",
                        str(log_path),
                        "Inspect",
                    ]
                )

            self.assertEqual(exit_code, 0)
            session_files = list((workspace / ".mini-coder" / "sessions").glob("*.json"))
            self.assertEqual(len(session_files), 1)
            raw = session_files[0].read_text(encoding="utf-8")
            self.assertNotIn("cli-test-secret", raw)
            restored = SessionStore(session_files[0].parent).load(session_files[0])
            self.assertEqual(restored.status.value, "completed_unverified")
            self.assertIn(restored.session_id, output.getvalue())
            events = [
                json.loads(line)
                for line in log_path.read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(events[-1]["event"], "run_completed")
            self.assertEqual(events[-1]["event_schema_version"], 1)
            self.assertIn("run_id", events[-1])
            self.assertIn("timestamp", events[-1])
            self.assertEqual(events[-1]["result_status"], "completed")
            self.assertEqual(events[-1]["session_status"], restored.status.value)
            self.assertEqual(events[-1]["session_id"], restored.session_id)


if __name__ == "__main__":
    unittest.main()
