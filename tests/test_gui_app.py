from __future__ import annotations

import json
import socket
import tempfile
import threading
import time
import unittest
import urllib.parse
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
        self.controller = RunController(
            runner_factory=lambda request, event, approval: HttpFakeRunner(event)
        )
        self.port = _unused_port()
        config = uvicorn.Config(
            create_app(self.controller),
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
            session.refresh_verification_status()
            session.set_status(SessionStatus.RUNNING)
            session.set_status(
                SessionStatus.COMPLETED_UNVERIFIED,
                final_text="Updated the greeting.\n\nOutcome: internal diagnostics",
            )
            SessionStore.for_workspace(workspace).save(session)

            base = f"http://127.0.0.1:{self.port}"
            workspace_query = urllib.parse.urlencode({"workspace": directory})
            with urllib.request.urlopen(
                f"{base}/api/sessions?{workspace_query}", timeout=3
            ) as response:
                listing = json.load(response)
            with urllib.request.urlopen(
                f"{base}/api/sessions/{session.session_id}?{workspace_query}", timeout=3
            ) as response:
                detail = json.load(response)
            with urllib.request.urlopen(
                f"{base}/api/sessions/{session.session_id}/changes/{change.change_id}?{workspace_query}",
                timeout=3,
            ) as response:
                code = json.load(response)

            self.assertEqual(listing["sessions"][0]["title"], "Friendly greeting")
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


if __name__ == "__main__":
    unittest.main()
