from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from mini_coder.agent import AgentRunResult
from mini_coder.gui.controller import RunController, RunRequest
from mini_coder.session import AgentSession, SessionStatus, SessionStore
from mini_coder.tools.base import RiskLevel, Tool, ToolContext, ToolResult


class DummyWriteTool(Tool):
    name = "write_file"
    description = "Write a file"
    parameters = {"type": "object", "properties": {}}
    risk = RiskLevel.WRITE

    def execute(self, arguments: dict, context: ToolContext) -> ToolResult:
        return ToolResult(True, "unused")


class DummyCommandTool(Tool):
    name = "run_command"
    description = "Run a command"
    parameters = {"type": "object", "properties": {}}
    risk = RiskLevel.EXECUTE

    def execute(self, arguments: dict, context: ToolContext) -> ToolResult:
        return ToolResult(True, "unused")


class FakeRunner:
    def __init__(self, event_callback, approval_callback, *, request_approval: bool) -> None:
        self.event_callback = event_callback
        self.approval_callback = approval_callback
        self.request_approval = request_approval

    def run(self, task: str) -> AgentRunResult:
        self.event_callback("run_started", {"task_preview": task[:30]})
        if self.request_approval:
            approved = self.approval_callback(
                DummyWriteTool(),
                {"path": "answer.txt", "content": "done\n"},
            )
            self.event_callback(
                "tool_call_completed",
                {"tool": "write_file", "ok": approved, "content": "decision recorded"},
            )
            if not approved:
                return AgentRunResult("denied", "The write was denied.", 1)
        self.event_callback("run_completed", {"verification_status": "passed"})
        return AgentRunResult(
            "completed",
            "The requested change is complete.",
            2,
            total_usage={"total_tokens": 12},
            session_id="session-test",
        )


class CancellationAwareRunner:
    def __init__(self, approval_callback, cancellation_callback) -> None:
        self.approval_callback = approval_callback
        self.cancellation_callback = cancellation_callback

    def run(self, task: str) -> AgentRunResult:
        self.approval_callback(
            DummyWriteTool(),
            {"path": "answer.txt", "content": "done\n"},
        )
        if self.cancellation_callback():
            return AgentRunResult("cancelled", "Stopped and saved.", 1)
        return AgentRunResult("completed", "done", 1)


class BatchApprovalRunner:
    def __init__(self, batch_approval_callback, *, commands: bool = False) -> None:
        self.batch_approval_callback = batch_approval_callback
        self.commands = commands

    def run(self, task: str) -> AgentRunResult:
        items = (
            [
                (DummyCommandTool(), {"command": "python -m unittest", "purpose": "verify"}),
                (DummyCommandTool(), {"command": "python -m compileall src", "purpose": "verify"}),
            ]
            if self.commands
            else [
                (DummyWriteTool(), {"path": "one.txt", "content": "one\n"}),
                (DummyWriteTool(), {"path": "two.txt", "content": "two\n"}),
            ]
        )
        approved = self.batch_approval_callback(items)
        return AgentRunResult(
            "completed" if approved else "denied",
            "done" if approved else "denied",
            1,
            session_id="batch-session",
        )


def wait_until(predicate, timeout: float = 2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.01)
    raise AssertionError("condition was not reached before timeout")


class RunControllerTests(unittest.TestCase):
    def test_batch_approval_is_exposed_as_one_active_run_request(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            def factory(request, event, approval, batch_approval, cancelled):
                return BatchApprovalRunner(batch_approval)

            controller = RunController(
                runner_factory=factory,
                approval_timeout_seconds=2,
            )
            started = controller.start(
                RunRequest(task="Create two files", workspace=directory)
            )
            waiting = wait_until(
                lambda: (
                    snapshot
                    if (snapshot := controller.snapshot(started["run_id"]))["status"]
                    == "waiting_for_approval"
                    else None
                )
            )

            self.assertEqual(len(controller.active_runs()), 1)
            pending = waiting["pending_approval"]
            self.assertEqual(pending["tool"], "batch")
            self.assertEqual(len(pending["items"]), 2)
            self.assertEqual(pending["summary"], "修改 2 个文件")

            controller.decide_approval(
                started["run_id"], pending["approval_id"], True
            )
            wait_until(
                lambda: controller.snapshot(started["run_id"])["status"] == "completed"
            )
            self.assertEqual(controller.active_runs(), [])

    def test_batch_command_approval_explains_the_requested_checks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            def factory(request, event, approval, batch_approval, cancelled):
                return BatchApprovalRunner(batch_approval, commands=True)

            controller = RunController(
                runner_factory=factory,
                approval_timeout_seconds=2,
            )
            started = controller.start(
                RunRequest(task="Run project checks", workspace=directory)
            )
            waiting = wait_until(
                lambda: controller.snapshot(started["run_id"])["pending_approval"]
            )
            pending = waiting

            self.assertEqual(pending["summary"], "运行 2 条本地命令")
            self.assertIn("运行项目检查", pending["items"][0]["description"])
            self.assertIn("python -m unittest", pending["items"][0]["description"])
            controller.decide_approval(
                started["run_id"], pending["approval_id"], False
            )
            wait_until(
                lambda: controller.snapshot(started["run_id"])["status"] == "failed"
            )

    def test_follow_up_request_reuses_saved_session_title_and_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            session = AgentSession.create(
                task="First task",
                title="Shared conversation",
                workspace=directory,
                model={"model": "fake"},
            )
            session.set_status(SessionStatus.RUNNING)
            session.set_status(SessionStatus.COMPLETED_UNVERIFIED, final_text="done")
            SessionStore.for_workspace(directory).save(session)
            captured = []

            def factory(request, event, approval):
                captured.append(request)
                return FakeRunner(event, approval, request_approval=False)

            controller = RunController(runner_factory=factory)
            started = controller.start(
                RunRequest(
                    task="Add another feature",
                    workspace=directory,
                    title="ignored title",
                    session_id=session.session_id,
                )
            )
            wait_until(
                lambda: controller.snapshot(started["run_id"])["status"] == "completed"
            )

            self.assertEqual(captured[0].session_id, session.session_id)
            self.assertEqual(captured[0].title, "Shared conversation")
            self.assertEqual(Path(captured[0].workspace), Path(directory).resolve())

    def test_completed_run_exposes_ordered_events_and_result(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller = RunController(
                runner_factory=lambda request, event, approval: FakeRunner(
                    event,
                    approval,
                    request_approval=False,
                )
            )

            started = controller.start(
                RunRequest(task="Inspect the project", workspace=directory)
            )
            finished = wait_until(
                lambda: (
                    snapshot
                    if (snapshot := controller.snapshot(started["run_id"]))["status"]
                    == "completed"
                    else None
                )
            )
            events = controller.events_after(started["run_id"])

            self.assertEqual(finished["result"]["session_id"], "session-test")
            self.assertEqual(finished["result"]["total_usage"]["total_tokens"], 12)
            self.assertEqual(
                [item["sequence"] for item in events],
                list(range(1, len(events) + 1)),
            )
            self.assertEqual(events[0]["event"], "controller_run_created")
            self.assertEqual(events[-1]["event"], "controller_run_finished")

    def test_safe_run_waits_for_browser_approval(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller = RunController(
                runner_factory=lambda request, event, approval: FakeRunner(
                    event,
                    approval,
                    request_approval=True,
                ),
                approval_timeout_seconds=2,
            )
            started = controller.start(
                RunRequest(task="Create answer.txt", workspace=directory)
            )
            waiting = wait_until(
                lambda: (
                    snapshot
                    if (snapshot := controller.snapshot(started["run_id"]))["status"]
                    == "waiting_for_approval"
                    else None
                )
            )
            pending = waiting["pending_approval"]

            self.assertEqual(pending["tool"], "write_file")
            self.assertEqual(pending["risk"], "write")
            self.assertEqual(pending["arguments"]["path"], "answer.txt")

            controller.decide_approval(
                started["run_id"],
                pending["approval_id"],
                True,
            )
            finished = wait_until(
                lambda: (
                    snapshot
                    if (snapshot := controller.snapshot(started["run_id"]))["status"]
                    == "completed"
                    else None
                )
            )

            self.assertIsNone(finished["pending_approval"])
            names = [item["event"] for item in controller.events_after(started["run_id"])]
            self.assertIn("approval_required", names)
            self.assertIn("approval_resolved", names)

    def test_rejected_approval_finishes_as_failed_without_hanging(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller = RunController(
                runner_factory=lambda request, event, approval: FakeRunner(
                    event,
                    approval,
                    request_approval=True,
                ),
                approval_timeout_seconds=2,
            )
            started = controller.start(
                RunRequest(task="Create answer.txt", workspace=directory)
            )
            waiting = wait_until(
                lambda: (
                    snapshot
                    if (snapshot := controller.snapshot(started["run_id"]))[
                        "pending_approval"
                    ]
                    else None
                )
            )
            controller.decide_approval(
                started["run_id"],
                waiting["pending_approval"]["approval_id"],
                False,
            )
            finished = wait_until(
                lambda: (
                    snapshot
                    if (snapshot := controller.snapshot(started["run_id"]))["status"]
                    == "failed"
                    else None
                )
            )

            self.assertEqual(finished["result"]["status"], "denied")

    def test_cancel_wakes_pending_approval_and_finishes_as_cancelled(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller = RunController(
                runner_factory=lambda request, event, approval, cancelled: (
                    CancellationAwareRunner(approval, cancelled)
                ),
                approval_timeout_seconds=5,
            )
            started = controller.start(
                RunRequest(task="Create answer.txt", workspace=directory)
            )
            wait_until(
                lambda: controller.snapshot(started["run_id"])["pending_approval"]
            )

            cancelling = controller.cancel(started["run_id"])
            self.assertEqual(cancelling["status"], "cancelling")
            finished = wait_until(
                lambda: (
                    snapshot
                    if (snapshot := controller.snapshot(started["run_id"]))["status"]
                    == "cancelled"
                    else None
                )
            )

            self.assertEqual(finished["result"]["status"], "cancelled")
            self.assertIsNone(finished["pending_approval"])
            names = [item["event"] for item in controller.events_after(started["run_id"])]
            self.assertIn("controller_cancel_requested", names)
            with self.assertRaisesRegex(ValueError, "already finished"):
                controller.cancel(started["run_id"])

    def test_start_rejects_empty_task_and_missing_workspace(self) -> None:
        controller = RunController(
            runner_factory=lambda request, event, approval: FakeRunner(
                event,
                approval,
                request_approval=False,
            )
        )
        with self.assertRaisesRegex(ValueError, "task must not be empty"):
            controller.start(RunRequest(task=" ", workspace="."))
        with self.assertRaisesRegex(ValueError, "workspace is not a directory"):
            controller.start(
                RunRequest(task="Do work", workspace="definitely-not-a-real-directory")
            )


if __name__ == "__main__":
    unittest.main()
