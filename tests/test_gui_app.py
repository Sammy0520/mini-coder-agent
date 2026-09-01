from __future__ import annotations

import json
import socket
import tempfile
import threading
import time
import unittest
import urllib.parse
import urllib.error
import urllib.request
from pathlib import Path

import uvicorn

from mini_coder.agent import AgentRunResult
from mini_coder.changes import ChangeTracker
from mini_coder.gui.app import create_app
from mini_coder.gui.controller import RunController
from mini_coder.session import (
    AgentSession,
    SessionStatus,
    SessionStore,
    ToolExecutionRecord,
    ToolExecutionStatus,
)
from mini_coder.verification import VerificationRecord


class HttpFakeRunner:
    def __init__(self, event_callback) -> None:
        self.event_callback = event_callback

    def run(self, task: str) -> AgentRunResult:
        self.event_callback("run_started", {"status": "running"})
        self.event_callback(
            "change_preview",
            {
                "path": "answer.txt",
                "additions": 1,
                "deletions": 0,
                "diff": "--- /dev/null\n+++ b/answer.txt\n@@ -0,0 +1 @@\n+done",
                "diff_truncated": False,
            },
        )
        self.event_callback(
            "verification_completed",
            {"passed": True, "exit_code": 0, "duration_seconds": 0.01},
        )
        return AgentRunResult(
            "completed",
            "Finished through the HTTP integration path.",
            1,
            session_id="http-session",
        )


def _unused_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as stream:
        stream.bind(("127.0.0.1", 0))
        return int(stream.getsockname()[1])


class GuiHttpTests(unittest.TestCase):
    def setUp(self) -> None:
        self.catalog_directory = tempfile.TemporaryDirectory()
        self.controller = RunController(
            runner_factory=lambda request, event, approval: HttpFakeRunner(event)
        )
        self.port = _unused_port()
        config = uvicorn.Config(
            create_app(
                self.controller,
                catalog_path=Path(self.catalog_directory.name) / "workspaces.json",
                skill_path=Path(self.catalog_directory.name) / "skills",
            ),
            host="127.0.0.1",
            port=self.port,
            log_level="error",
        )
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.thread.start()
        deadline = time.monotonic() + 5
        while not self.server.started and time.monotonic() < deadline:
            time.sleep(0.01)
        if not self.server.started:
            self.fail("test GUI server did not start")

    def tearDown(self) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=5)
        self.catalog_directory.cleanup()

    def test_skill_api_lists_builtins_and_manages_custom_skills(self) -> None:
        base = f"http://127.0.0.1:{self.port}"
        with urllib.request.urlopen(f"{base}/api/skills", timeout=3) as response:
            initial = json.load(response)
        self.assertEqual(initial["builtin_count"], 3)
        self.assertEqual(initial["custom_count"], 0)

        body = json.dumps(
            {
                "name": "接口文档助手",
                "description": "根据真实代码整理接口说明和调用示例",
                "instructions": "先阅读路由和数据模型，再记录真实可用的接口与示例。",
            }
        ).encode("utf-8")
        request = urllib.request.Request(
            f"{base}/api/skills",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=3) as response:
            created = json.load(response)
        self.assertFalse(created["builtin"])

        with urllib.request.urlopen(f"{base}/api/skills", timeout=3) as response:
            listing = json.load(response)
        self.assertEqual(listing["custom_count"], 1)

        delete = urllib.request.Request(
            f"{base}/api/skills/{created['id']}",
            method="DELETE",
        )
        with urllib.request.urlopen(delete, timeout=3) as response:
            deleted = json.load(response)
        self.assertTrue(deleted["deleted"])

    def test_static_page_run_api_and_sse_form_one_vertical_slice(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            child_directory = Path(directory) / "example-project"
            child_directory.mkdir()
            base = f"http://127.0.0.1:{self.port}"
            with urllib.request.urlopen(f"{base}/api/health", timeout=3) as response:
                health = json.load(response)
            with urllib.request.urlopen(f"{base}/", timeout=3) as response:
                index = response.read().decode("utf-8")
            with urllib.request.urlopen(f"{base}/api/bootstrap", timeout=3) as response:
                bootstrap = json.load(response)
            directory_query = urllib.parse.urlencode({"path": directory})
            with urllib.request.urlopen(
                f"{base}/api/directories?{directory_query}",
                timeout=3,
            ) as response:
                directory_listing = json.load(response)

            body = json.dumps(
                {
                    "title": "Create answer file",
                    "task": "Create answer.txt",
                    "workspace": directory,
                    "config_path": None,
                    "auto": False,
                }
            ).encode("utf-8")
            request = urllib.request.Request(
                f"{base}/api/runs",
                data=body,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=3) as response:
                started = json.load(response)
            run_id = started["run_id"]

            with urllib.request.urlopen(
                f"{base}/api/runs/{run_id}/events",
                timeout=5,
            ) as response:
                event_stream = response.read().decode("utf-8")
            with urllib.request.urlopen(f"{base}/api/runs/{run_id}", timeout=3) as response:
                snapshot = json.load(response)

            self.assertEqual(health, {"status": "ok"})
            self.assertIn("Mini Coder Agent", index)
            self.assertIn('id="stopRunButton"', index)
            self.assertIn('id="undoButton"', index)
            self.assertIn('id="changesToggleButton"', index)
            self.assertIn('id="turnSummary"', index)
            self.assertIn('id="deleteSessionModal"', index)
            self.assertIn('id="renameSessionModal"', index)
            self.assertIn('id="sessionInfoModal"', index)
            self.assertIn('id="taskStatusBar"', index)
            self.assertIn('data-phase="discover"', index)
            self.assertIn('id="skillModal"', index)
            self.assertIn('id="settingsButton"', index)
            self.assertIn('class="message-bubble markdown-body"', (Path(__file__).parents[1] / "src" / "mini_coder" / "gui" / "static" / "app.js").read_text(encoding="utf-8"))
            self.assertNotIn('id="conversationProject"', index)
            self.assertNotIn('id="usageSummary"', index)
            self.assertEqual(Path(bootstrap["default_workspace"]), Path.cwd().resolve())
            self.assertEqual(
                Path(bootstrap["default_config_path"]),
                Path.cwd().resolve() / "agent.toml",
            )
            self.assertEqual(Path(directory_listing["current"]), Path(directory).resolve())
            self.assertIn(
                str(child_directory.resolve()),
                [item["path"] for item in directory_listing["directories"]],
            )
            self.assertIn("event: run-event", event_stream)
            self.assertIn('"event": "change_preview"', event_stream)
            self.assertIn('"event": "verification_completed"', event_stream)
            self.assertIn('"event": "controller_run_finished"', event_stream)
            self.assertEqual(snapshot["status"], "completed")
            self.assertEqual(snapshot["title"], "Create answer file")
            self.assertEqual(snapshot["result"]["session_id"], "http-session")
            self.assertEqual(Path(snapshot["workspace"]), Path(directory).resolve())

    def test_session_history_and_full_change_api(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            target = workspace / "greeting.py"
            target.write_text('message = "hello"\n', encoding="utf-8")
            tracker = ChangeTracker(workspace)
            prepared = tracker.prepare(
                "edit_file",
                {
                    "path": "greeting.py",
                    "old_text": 'message = "hello"',
                    "new_text": 'message = "hello world"',
                },
                "execution-1",
            )
            change = tracker.apply(prepared)
            session = AgentSession.create(
                task="Update the greeting",
                title="Friendly greeting",
                workspace=workspace,
                model={"model": "fake"},
                workspace_baseline={"entry_points": ["greeting.py"]},
            )
            execution = ToolExecutionRecord.create(
                execution_id="execution-1",
                tool_call_id="call-1",
                step=1,
                name="edit_file",
                arguments={"path": "greeting.py"},
                raw_arguments='{"path":"greeting.py"}',
                risk="write",
            )
            execution.set_status(ToolExecutionStatus.APPROVED)
            execution.set_status(ToolExecutionStatus.RUNNING)
            execution.set_status(ToolExecutionStatus.COMPLETED)
            execution.approval_granted = True
            execution.ok = True
            execution.change_id = change.change_id
            session.tool_executions.append(execution)
            session.changes.append(change)
            session.verification_records.append(
                VerificationRecord.create(
                    tool_execution_id="verify-1",
                    command="python -m unittest",
                    cwd=".",
                    exit_code=0,
                    duration_seconds=0.1,
                    stdout_summary="OK",
                    stderr_summary="",
                    change_revision=session.change_revision,
                    passed=True,
                    timed_out=False,
                )
            )
            session.refresh_verification_status()
            session.set_status(SessionStatus.RUNNING)
            session.set_status(
                SessionStatus.COMPLETED_VERIFIED,
                final_text="Updated the greeting.\n\nOutcome: internal diagnostics",
            )
            SessionStore.for_workspace(workspace).save(session)
            second_workspace = workspace / "second-workspace"
            second_workspace.mkdir()
            second_session = AgentSession.create(
                task="Inspect another folder",
                title="Second folder session",
                workspace=second_workspace,
                model={"model": "fake"},
            )
            SessionStore.for_workspace(second_workspace).save(second_session)

            base = f"http://127.0.0.1:{self.port}"
            workspace_query = urllib.parse.urlencode({"workspace": directory})
            with urllib.request.urlopen(
                f"{base}/api/sessions?{workspace_query}", timeout=3
            ) as response:
                listing = json.load(response)
            second_query = urllib.parse.urlencode({"workspace": second_workspace})
            with urllib.request.urlopen(
                f"{base}/api/sessions?{second_query}", timeout=3
            ):
                pass
            with urllib.request.urlopen(f"{base}/api/sessions", timeout=3) as response:
                global_listing = json.load(response)
            with urllib.request.urlopen(
                f"{base}/api/sessions/{session.session_id}", timeout=3
            ) as response:
                detail = json.load(response)
            with urllib.request.urlopen(
                f"{base}/api/sessions/{session.session_id}/changes/{change.change_id}",
                timeout=3,
            ) as response:
                code = json.load(response)

            self.assertEqual(listing["sessions"][0]["title"], "Friendly greeting")
            self.assertEqual(
                {item["title"] for item in global_listing["sessions"]},
                {"Friendly greeting", "Second folder session"},
            )
            self.assertEqual(detail["conversation"][0]["content"], "Update the greeting")
            reply = detail["conversation"][1]["content"]
            self.assertIn("已经完成了", reply)
            self.assertIn("greeting.py", reply)
            self.assertNotIn("Outcome", reply)
            self.assertEqual(detail["execution"][0]["title"], "先了解了一下项目结构")
            self.assertEqual(detail["execution"][1]["title"], "修改了 greeting.py")
            self.assertEqual(detail["changes"][0]["change_id"], change.change_id)
            self.assertEqual(code["before"].splitlines(), ['message = "hello"'])
            self.assertEqual(code["after"].splitlines(), ['message = "hello world"'])
            self.assertIn('+message = "hello world"', code["diff"])
            self.assertTrue(code["matches_agent_version"])

            undo_request = urllib.request.Request(
                f"{base}/api/sessions/{session.session_id}/undo-last",
                data=b"",
                method="POST",
            )
            with urllib.request.urlopen(undo_request, timeout=3) as response:
                undo = json.load(response)
            with urllib.request.urlopen(
                f"{base}/api/sessions/{session.session_id}", timeout=3
            ) as response:
                after_undo = json.load(response)

            self.assertEqual(undo["path"], "greeting.py")
            self.assertEqual(target.read_text(encoding="utf-8"), 'message = "hello"\n')
            self.assertEqual(after_undo["status"], "interrupted")
            self.assertEqual(after_undo["verification_status"], "stale")
            self.assertEqual(after_undo["changes"][0]["undo_status"], "undone")

    def test_undo_refuses_to_overwrite_a_user_edit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            target = workspace / "notes.txt"
            target.write_text("before\n", encoding="utf-8")
            tracker = ChangeTracker(workspace)
            change = tracker.apply(
                tracker.prepare(
                    "edit_file",
                    {
                        "path": "notes.txt",
                        "old_text": "before",
                        "new_text": "agent version",
                    },
                    "execution-conflict",
                )
            )
            session = AgentSession.create(
                task="Update notes",
                workspace=workspace,
                model={"model": "fake"},
            )
            session.changes.append(change)
            session.refresh_verification_status()
            session.set_status(SessionStatus.RUNNING)
            session.set_status(SessionStatus.COMPLETED_UNVERIFIED, final_text="done")
            SessionStore.for_workspace(workspace).save(session)

            base = f"http://127.0.0.1:{self.port}"
            query = urllib.parse.urlencode({"workspace": directory})
            with urllib.request.urlopen(f"{base}/api/sessions?{query}", timeout=3) as response:
                listing = json.load(response)
            self.assertIn(
                session.session_id,
                [item["session_id"] for item in listing["sessions"]],
            )
            target.write_text("user version\n", encoding="utf-8")
            request = urllib.request.Request(
                f"{base}/api/sessions/{session.session_id}/undo-last",
                data=b"",
                method="POST",
            )

            with self.assertRaises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(request, timeout=3)

            self.assertEqual(caught.exception.code, 409)
            self.assertEqual(target.read_text(encoding="utf-8"), "user version\n")

    def test_delete_session_keeps_project_files_and_blocks_running_session(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            project_file = workspace / "app.py"
            project_file.write_text("print('keep me')\n", encoding="utf-8")
            session = AgentSession.create(
                task="Inspect app.py",
                title="Disposable conversation",
                workspace=workspace,
                model={"model": "fake"},
            )
            session.set_status(SessionStatus.RUNNING)
            session.set_status(SessionStatus.COMPLETED_UNVERIFIED, final_text="done")
            store = SessionStore.for_workspace(workspace)
            store.save(session)
            runtime = workspace / ".mini-coder" / "runtime" / session.session_id
            runtime.mkdir(parents=True)
            (runtime / "scratch.txt").write_text("temporary\n", encoding="utf-8")

            base = f"http://127.0.0.1:{self.port}"
            query = urllib.parse.urlencode({"workspace": directory})
            with urllib.request.urlopen(f"{base}/api/sessions?{query}", timeout=3):
                pass
            request = urllib.request.Request(
                f"{base}/api/sessions/{session.session_id}",
                method="DELETE",
            )
            with urllib.request.urlopen(request, timeout=3) as response:
                deleted = json.load(response)

            self.assertTrue(deleted["deleted"])
            self.assertTrue(deleted["runtime_removed"])
            self.assertTrue(project_file.is_file())
            self.assertFalse(store.path_for(session.session_id).exists())
            self.assertFalse(runtime.exists())

            running = AgentSession.create(
                task="Still running",
                title="Active conversation",
                workspace=workspace,
                model={"model": "fake"},
            )
            running.set_status(SessionStatus.RUNNING)
            store.save(running)
            running_request = urllib.request.Request(
                f"{base}/api/sessions/{running.session_id}",
                method="DELETE",
            )
            with self.assertRaises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(running_request, timeout=3)

            self.assertEqual(caught.exception.code, 409)
            self.assertTrue(store.path_for(running.session_id).exists())

    def test_rename_session_persists_title_and_updates_last_active_time(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            session = AgentSession.create(
                task="Inspect app.py",
                title="Old conversation name",
                workspace=workspace,
                model={"model": "fake"},
            )
            original_updated_at = session.updated_at
            store = SessionStore.for_workspace(workspace)
            store.save(session)

            base = f"http://127.0.0.1:{self.port}"
            query = urllib.parse.urlencode({"workspace": directory})
            with urllib.request.urlopen(f"{base}/api/sessions?{query}", timeout=3):
                pass
            body = json.dumps({"title": "  New conversation name  "}).encode("utf-8")
            request = urllib.request.Request(
                f"{base}/api/sessions/{session.session_id}",
                data=body,
                headers={"Content-Type": "application/json"},
                method="PATCH",
            )
            with urllib.request.urlopen(request, timeout=3) as response:
                renamed = json.load(response)

            restored = store.load(session.session_id)
            self.assertEqual(renamed["title"], "New conversation name")
            self.assertEqual(restored.title, "New conversation name")
            self.assertGreaterEqual(restored.updated_at, original_updated_at)
            self.assertEqual(renamed["turn_count"], 1)

    def test_single_file_undo_only_reverts_the_requested_change(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            tracker = ChangeTracker(workspace)
            first = tracker.apply(
                tracker.prepare(
                    "write_file",
                    {"path": "first.txt", "content": "first\n"},
                    "execution-first",
                )
            )
            second = tracker.apply(
                tracker.prepare(
                    "write_file",
                    {"path": "second.txt", "content": "second\n"},
                    "execution-second",
                )
            )
            session = AgentSession.create(
                task="Create two files",
                workspace=workspace,
                model={"model": "fake"},
            )
            session.changes.extend([first, second])
            session.refresh_verification_status()
            session.set_status(SessionStatus.RUNNING)
            session.set_status(SessionStatus.COMPLETED_UNVERIFIED, final_text="done")
            SessionStore.for_workspace(workspace).save(session)

            base = f"http://127.0.0.1:{self.port}"
            query = urllib.parse.urlencode({"workspace": directory})
            with urllib.request.urlopen(
                f"{base}/api/sessions?{query}", timeout=3
            ) as response:
                listing = json.load(response)
            self.assertIn(
                session.session_id,
                [item["session_id"] for item in listing["sessions"]],
            )
            request = urllib.request.Request(
                f"{base}/api/sessions/{session.session_id}/changes/{first.change_id}/undo",
                data=b"",
                method="POST",
            )
            try:
                with urllib.request.urlopen(request, timeout=3) as response:
                    result = json.load(response)
            except urllib.error.HTTPError as exc:
                self.fail(f"single-file undo failed: {exc.read().decode('utf-8')}")

            self.assertEqual(result["change_id"], first.change_id)
            self.assertFalse((workspace / "first.txt").exists())
            self.assertTrue((workspace / "second.txt").exists())
            saved = SessionStore.for_workspace(workspace).load(session.session_id)
            self.assertEqual(saved.changes[0].undo_status, "undone")
            self.assertEqual(saved.changes[1].undo_status, "active")


if __name__ == "__main__":
    unittest.main()
