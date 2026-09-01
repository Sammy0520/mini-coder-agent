from __future__ import annotations

import tempfile
import threading
import time
import unittest
from dataclasses import dataclass, field
from pathlib import Path

from mini_coder.agent import AgentRunner
from mini_coder.config import AgentConfig, ApprovalPolicy
from mini_coder.messages import ModelResponse, ToolCall
from mini_coder.model import ModelClient
from mini_coder.session import SessionStore
from mini_coder.tools.base import RiskLevel, Tool, ToolContext, ToolResult
from mini_coder.tools.registry import ToolRegistry


class FakeModel(ModelClient):
    def __init__(self, responses: list[ModelResponse]) -> None:
        self.responses = list(responses)
        self.requests: list[list[dict]] = []

    def complete(self, messages, tools) -> ModelResponse:
        self.requests.append(list(messages))
        return self.responses.pop(0)


@dataclass
class SharedProbe:
    barrier: threading.Barrier
    lock: threading.Lock = field(default_factory=threading.Lock)
    active: int = 0
    peak: int = 0
    timeline: list[str] = field(default_factory=list)


class ProbeReadTool(Tool):
    description = "Test-only parallel observation"
    parameters = {"type": "object", "properties": {}, "additionalProperties": False}
    risk = RiskLevel.READ
    parallel_safe = True

    def __init__(self, name: str, probe: SharedProbe, delay: float = 0.04, *, fail: bool = False):
        self.name = name
        self.probe = probe
        self.delay = delay
        self.fail = fail

    def execute(self, arguments: dict, context: ToolContext) -> ToolResult:
        with self.probe.lock:
            self.probe.active += 1
            self.probe.peak = max(self.probe.peak, self.probe.active)
            self.probe.timeline.append(f"{self.name}:start")
        try:
            self.probe.barrier.wait(timeout=1)
            time.sleep(self.delay)
            return ToolResult(not self.fail, self.name)
        finally:
            with self.probe.lock:
                self.probe.timeline.append(f"{self.name}:end")
                self.probe.active -= 1


class SerialTool(Tool):
    name = "serial_barrier"
    description = "Test-only serial barrier"
    parameters = {"type": "object", "properties": {}, "additionalProperties": False}
    risk = RiskLevel.WRITE

    def __init__(self, timeline: list[str]):
        self.timeline = timeline

    def execute(self, arguments: dict, context: ToolContext) -> ToolResult:
        self.timeline.append("serial_barrier")
        return ToolResult(True, "serial")


def config(workspace: Path) -> AgentConfig:
    return AgentConfig(
        workspace=workspace,
        api_key="test",
        base_url=None,
        model="fake",
        approval_policy=ApprovalPolicy.AUTO,
        max_steps=4,
    )


def call(call_id: str, name: str) -> ToolCall:
    return ToolCall(id=call_id, name=name, arguments={}, raw_arguments="{}")


class ParallelToolTests(unittest.TestCase):
    def test_parallel_reads_overlap_but_results_commit_in_request_order(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            probe = SharedProbe(threading.Barrier(2))
            registry = ToolRegistry(
                [
                    ProbeReadTool("slow_read", probe, 0.08),
                    ProbeReadTool("fast_read", probe, 0.04),
                ]
            )
            model = FakeModel(
                [
                    ModelResponse(tool_calls=[call("slow", "slow_read"), call("fast", "fast_read")]),
                    ModelResponse(content="done"),
                ]
            )
            store = SessionStore.for_workspace(workspace)
            events: list[tuple[str, dict]] = []
            result = AgentRunner(
                model=model,
                registry=registry,
                config=config(workspace),
                session_store=store,
                event_callback=lambda name, payload: events.append((name, payload)),
            ).run("inspect two independent sources")

            session = store.load(result.session_id or "")
            tool_messages = [item for item in model.requests[1] if item.get("role") == "tool"]
            self.assertEqual(probe.peak, 2)
            self.assertEqual([item["tool_call_id"] for item in tool_messages[-2:]], ["slow", "fast"])
            self.assertEqual(session.parallel_tool_batches, 1)
            self.assertEqual(session.parallel_tool_calls, 2)
            self.assertEqual(session.parallel_tool_peak_concurrency, 2)
            self.assertGreater(session.parallel_tool_overlap_seconds, 0.02)
            self.assertEqual(
                [name for name, _ in events if name.startswith("parallel_tool_batch_")],
                ["parallel_tool_batch_started", "parallel_tool_batch_completed"],
            )

    def test_serial_tool_is_a_barrier_between_parallel_read_segments(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            timeline: list[str] = []
            first = SharedProbe(threading.Barrier(2), timeline=timeline)
            second = SharedProbe(threading.Barrier(2), timeline=timeline)
            registry = ToolRegistry(
                [
                    ProbeReadTool("read_a", first),
                    ProbeReadTool("read_b", first),
                    SerialTool(timeline),
                    ProbeReadTool("read_c", second),
                    ProbeReadTool("read_d", second),
                ]
            )
            model = FakeModel(
                [
                    ModelResponse(
                        tool_calls=[
                            call("a", "read_a"),
                            call("b", "read_b"),
                            call("barrier", "serial_barrier"),
                            call("c", "read_c"),
                            call("d", "read_d"),
                        ]
                    ),
                    ModelResponse(content="done"),
                ]
            )
            store = SessionStore.for_workspace(workspace)
            result = AgentRunner(
                model=model,
                registry=registry,
                config=config(workspace),
                session_store=store,
            ).run("inspect, update, then inspect")

            session = store.load(result.session_id or "")
            barrier_index = timeline.index("serial_barrier")
            self.assertTrue(all(timeline.index(f"read_{name}:end") < barrier_index for name in ("a", "b")))
            self.assertTrue(all(timeline.index(f"read_{name}:start") > barrier_index for name in ("c", "d")))
            self.assertEqual(session.parallel_tool_batches, 2)
            self.assertEqual(session.parallel_tool_calls, 4)

    def test_one_parallel_read_failure_does_not_cancel_its_peer(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            probe = SharedProbe(threading.Barrier(2))
            registry = ToolRegistry(
                [
                    ProbeReadTool("bad_read", probe, fail=True),
                    ProbeReadTool("good_read", probe),
                ]
            )
            model = FakeModel(
                [
                    ModelResponse(tool_calls=[call("bad", "bad_read"), call("good", "good_read")]),
                    ModelResponse(content="done"),
                ]
            )
            store = SessionStore.for_workspace(workspace)
            result = AgentRunner(
                model=model,
                registry=registry,
                config=config(workspace),
                session_store=store,
            ).run("inspect both")

            session = store.load(result.session_id or "")
            by_name = {record.name: record for record in session.tool_executions}
            self.assertFalse(by_name["bad_read"].ok)
            self.assertTrue(by_name["good_read"].ok)
            self.assertEqual(session.failed_tool_call_count, 1)


if __name__ == "__main__":
    unittest.main()
