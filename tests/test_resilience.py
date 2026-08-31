from __future__ import annotations

import tempfile
import time
import unittest
import warnings
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from mini_coder.agent import AgentRunner
from mini_coder.config import AgentConfig, ApprovalPolicy
from mini_coder.exceptions import ModelError, ModelErrorCategory, ModelProtocolError
from mini_coder.messages import ModelResponse, ToolCall
from mini_coder.model import ModelClient
from mini_coder.model.errors import classify_model_exception
from mini_coder.redaction import redact_sensitive_text, redact_sensitive_value
from mini_coder.session import SessionStatus, SessionStore
from mini_coder.tools import create_default_registry


class FaultModel(ModelClient):
    def __init__(self, items: list[ModelResponse | BaseException]) -> None:
        self.items = list(items)
        self.calls = 0
        self.requests = []

    def complete(self, messages, tools) -> ModelResponse:
        self.calls += 1
        self.requests.append(messages)
        item = self.items.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


class FakeHTTPError(Exception):
    def __init__(self, status_code: int, retry_after: str | None = None) -> None:
        super().__init__(f"provider returned {status_code} with Bearer sk-fake-secret-123")
        headers = {} if retry_after is None else {"Retry-After": retry_after}
        self.status_code = status_code
        self.response = SimpleNamespace(status_code=status_code, headers=headers)


def make_config(workspace: Path, **overrides) -> AgentConfig:
    values = {
        "workspace": workspace,
        "api_key": "local-unusual-secret",
        "base_url": None,
        "model": "fake",
        "approval_policy": ApprovalPolicy.AUTO,
        "max_steps": 10,
        "command_timeout_seconds": 5,
        "max_tool_output_chars": 4_000,
        "max_context_chars": 20_000,
        "repeated_call_limit": 3,
        "retry_base_seconds": 0.01,
        "retry_max_seconds": 1.0,
    }
    values.update(overrides)
    return AgentConfig(**values)


class ModelFailureTests(unittest.TestCase):
    def test_http_and_transport_failures_are_classified(self) -> None:
        expected = {
            400: (ModelErrorCategory.REQUEST, False),
            401: (ModelErrorCategory.AUTHENTICATION, False),
            403: (ModelErrorCategory.PERMISSION, False),
            429: (ModelErrorCategory.RATE_LIMIT, True),
            500: (ModelErrorCategory.SERVER, True),
            502: (ModelErrorCategory.SERVER, True),
            503: (ModelErrorCategory.SERVER, True),
            504: (ModelErrorCategory.SERVER, True),
        }
        for status, (category, retryable) in expected.items():
            with self.subTest(status=status):
                classified = classify_model_exception(FakeHTTPError(status, "2"))
                self.assertEqual(classified.category, category)
                self.assertEqual(classified.retryable, retryable)
                self.assertNotIn("sk-fake-secret", str(classified))
                if status == 429:
                    self.assertEqual(classified.retry_after_seconds, 2.0)

        timeout = classify_model_exception(TimeoutError("socket timed out"))
        self.assertEqual(timeout.category, ModelErrorCategory.TIMEOUT)
        self.assertTrue(timeout.retryable)
        network = classify_model_exception(ConnectionError("connection reset"))
        self.assertEqual(network.category, ModelErrorCategory.NETWORK)
        self.assertTrue(network.retryable)
        stream_read = classify_model_exception(RuntimeError("stream_read_error"))
        self.assertEqual(stream_read.category, ModelErrorCategory.NETWORK)
        self.assertTrue(stream_read.retryable)

        unusual_secret = "provider-secret-without-a-known-prefix"
        classified = classify_model_exception(
            RuntimeError(f"request headers contained {unusual_secret}"),
            secrets=(unusual_secret,),
        )
        self.assertNotIn(unusual_secret, str(classified))

    def test_retryable_error_retries_once_and_persists_attempts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            store = SessionStore.for_workspace(workspace)
            events = []
            model = FaultModel(
                [
                    ModelError(
                        "rate limited",
                        category=ModelErrorCategory.RATE_LIMIT,
                        retryable=True,
                        status_code=429,
                        retry_after_seconds=0,
                    ),
                    ModelResponse(
                        content="Recovered.",
                        usage={"input_tokens": 2, "output_tokens": 1, "total_tokens": 3},
                    ),
                ]
            )
            runner = AgentRunner(
                model=model,
                registry=create_default_registry(),
                config=make_config(workspace),
                event_callback=lambda name, payload: events.append((name, payload)),
                session_store=store,
            )

            with patch("mini_coder.agent.random.uniform", return_value=0), patch(
                "mini_coder.agent.time.sleep"
            ) as sleep:
                result = runner.run("Inspect the project")

            session = store.load(result.session_id or "")
            self.assertEqual(result.status, "completed")
            self.assertEqual(model.calls, 2)
            self.assertEqual(session.model_call_count, 2)
            self.assertEqual(session.retry_count, 1)
            self.assertEqual(session.usage_missing_count, 1)
            self.assertEqual(session.total_usage["total_tokens"], 3)
            sleep.assert_called_once()
            retries = [payload for name, payload in events if name == "retry_scheduled"]
            self.assertEqual(len(retries), 1)
            self.assertEqual(retries[0]["category"], "rate_limit")

    def test_all_recoverable_categories_retry_with_a_hard_limit(self) -> None:
        categories = (
            ModelErrorCategory.RATE_LIMIT,
            ModelErrorCategory.SERVER,
            ModelErrorCategory.TIMEOUT,
            ModelErrorCategory.NETWORK,
        )
        for category in categories:
            with self.subTest(category=category), tempfile.TemporaryDirectory() as directory:
                workspace = Path(directory)
                store = SessionStore.for_workspace(workspace)
                model = FaultModel(
                    [
                        ModelError(
                            "temporary",
                            category=category,
                            retryable=True,
                        ),
                        ModelError(
                            "temporary again",
                            category=category,
                            retryable=True,
                        ),
                        ModelError(
                            "hard stop",
                            category=category,
                            retryable=True,
                        ),
                    ]
                )
                runner = AgentRunner(
                    model=model,
                    registry=create_default_registry(),
                    config=make_config(workspace, max_model_retries=2),
                    session_store=store,
                )

                with patch("mini_coder.agent.random.uniform", return_value=0), patch(
                    "mini_coder.agent.time.sleep"
                ):
                    result = runner.run("Inspect")

                session = store.load(result.session_id or "")
                self.assertEqual(result.status, "model_error")
                expected_calls = 2 if category == ModelErrorCategory.TIMEOUT else 3
                self.assertEqual(model.calls, expected_calls)
                self.assertEqual(session.retry_count, expected_calls - 1)
                self.assertEqual(session.model_call_count, expected_calls)
                if category == ModelErrorCategory.TIMEOUT:
                    rendered = str(model.requests[-1])
                    self.assertIn("smallest durable action batch", rendered)
                    self.assertIn("at most two file write/edit calls", rendered)

    def test_400_401_and_403_stop_without_retry(self) -> None:
        failures = (
            (400, ModelErrorCategory.REQUEST),
            (401, ModelErrorCategory.AUTHENTICATION),
            (403, ModelErrorCategory.PERMISSION),
        )
        for status_code, category in failures:
            with self.subTest(status=status_code), tempfile.TemporaryDirectory() as directory:
                workspace = Path(directory)
                store = SessionStore.for_workspace(workspace)
                model = FaultModel(
                    [
                        ModelError(
                            "request rejected",
                            category=category,
                            retryable=False,
                            status_code=status_code,
                        )
                    ]
                )
                runner = AgentRunner(
                    model=model,
                    registry=create_default_registry(),
                    config=make_config(workspace, max_model_retries=5),
                    session_store=store,
                )

                result = runner.run("Inspect")

                session = store.load(result.session_id or "")
                self.assertEqual(result.status, "model_error")
                self.assertEqual(model.calls, 1)
                self.assertEqual(session.model_call_count, 1)
                self.assertEqual(session.retry_count, 0)

    def test_protocol_error_is_retried_at_most_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            store = SessionStore.for_workspace(workspace)
            model = FaultModel(
                [ModelProtocolError("bad payload"), ModelProtocolError("still bad")]
            )
            runner = AgentRunner(
                model=model,
                registry=create_default_registry(),
                config=make_config(workspace, max_model_retries=5),
                session_store=store,
            )

            with patch("mini_coder.agent.random.uniform", return_value=0), patch(
                "mini_coder.agent.time.sleep"
            ):
                result = runner.run("Inspect")

            session = store.load(result.session_id or "")
            self.assertEqual(result.status, "model_error")
            self.assertEqual(model.calls, 2)
            self.assertEqual(session.retry_count, 1)


class BudgetTests(unittest.TestCase):
    def test_model_call_budget_stops_with_resumable_session(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            store = SessionStore.for_workspace(workspace)
            model = FaultModel(
                [
                    ModelResponse(
                        tool_calls=[
                            ToolCall(
                                id="call-list",
                                name="list_files",
                                arguments={"path": "."},
                                raw_arguments='{"path":"."}',
                            )
                        ]
                    ),
                    ModelResponse(content="Should not be requested"),
                ]
            )
            runner = AgentRunner(
                model=model,
                registry=create_default_registry(),
                config=make_config(workspace, max_model_calls=1),
                session_store=store,
            )

            result = runner.run("Inspect")

            session = store.load(result.session_id or "")
            self.assertEqual(result.status, "budget_exceeded")
            self.assertEqual(session.status, SessionStatus.INTERRUPTED)
            self.assertEqual(session.stop_reason, "max_model_calls")
            self.assertEqual(model.calls, 1)
            self.assertEqual(session.model_call_count, 1)

    def test_tool_call_budget_stops_before_pending_calls_execute(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            store = SessionStore.for_workspace(workspace)
            calls = [
                ToolCall(
                    id=f"call-{index}",
                    name="list_files",
                    arguments={"path": "."},
                    raw_arguments='{"path":"."}',
                )
                for index in range(2)
            ]
            runner = AgentRunner(
                model=FaultModel([ModelResponse(tool_calls=calls)]),
                registry=create_default_registry(),
                config=make_config(workspace, max_tool_calls=1),
                session_store=store,
            )

            result = runner.run("Inspect")

            session = store.load(result.session_id or "")
            self.assertEqual(result.status, "budget_exceeded")
            self.assertEqual(session.stop_reason, "max_tool_calls")
            self.assertEqual(len(session.tool_executions), 2)
            self.assertTrue(all(item.status.value == "requested" for item in session.tool_executions))

    def test_cumulative_tool_output_budget_is_enforced(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            (workspace / "large.txt").write_text("x" * 2_000, encoding="utf-8")
            store = SessionStore.for_workspace(workspace)
            response = ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="call-read-large",
                        name="read_file",
                        arguments={"path": "large.txt"},
                        raw_arguments='{"path":"large.txt"}',
                    )
                ]
            )
            runner = AgentRunner(
                model=FaultModel([response]),
                registry=create_default_registry(),
                config=make_config(
                    workspace,
                    max_tool_output_chars=3_000,
                    max_total_tool_output_chars=200,
                ),
                session_store=store,
            )

            result = runner.run("Read the file")

            session = store.load(result.session_id or "")
            self.assertEqual(result.status, "budget_exceeded")
            self.assertEqual(session.stop_reason, "max_total_tool_output")
            self.assertGreaterEqual(session.tool_output_chars, 200)

    def test_resume_rechecks_budget_between_pending_tools(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            for name in ("first.txt", "second.txt"):
                (workspace / name).write_text("x" * 1_000, encoding="utf-8")
            store = SessionStore.for_workspace(workspace)
            calls = [
                ToolCall(
                    id=f"call-{name}",
                    name="read_file",
                    arguments={"path": name},
                    raw_arguments=f'{{"path":"{name}"}}',
                )
                for name in ("first.txt", "second.txt")
            ]
            initial = AgentRunner(
                model=FaultModel([ModelResponse(tool_calls=calls)]),
                registry=create_default_registry(),
                config=make_config(workspace, max_tool_calls=1),
                session_store=store,
            ).run("Read both files")

            resumed = AgentRunner(
                model=FaultModel([]),
                registry=create_default_registry(),
                config=make_config(
                    workspace,
                    max_tool_calls=3,
                    max_total_tool_output_chars=100,
                ),
                session_store=store,
            ).run("", session=store.load(initial.session_id or ""))

            restored = store.load(resumed.session_id or "")
            self.assertEqual(resumed.status, "budget_exceeded")
            self.assertEqual(restored.stop_reason, "max_total_tool_output")
            self.assertEqual(restored.tool_executions[0].status.value, "completed")
            self.assertEqual(restored.tool_executions[1].status.value, "requested")

    def test_total_time_and_reported_token_budgets_are_enforced(self) -> None:
        for budget_name in ("time", "tokens"):
            with self.subTest(budget=budget_name), tempfile.TemporaryDirectory() as directory:
                workspace = Path(directory)
                store = SessionStore.for_workspace(workspace)

                class SlowOrTokenModel(ModelClient):
                    def complete(self, messages, tools) -> ModelResponse:
                        if budget_name == "time":
                            time.sleep(1.05)
                        return ModelResponse(
                            tool_calls=[
                                ToolCall(
                                    id="call-list-budget",
                                    name="list_files",
                                    arguments={"path": "."},
                                    raw_arguments='{"path":"."}',
                                )
                            ],
                            usage={
                                "input_tokens": 60,
                                "output_tokens": 40,
                                "total_tokens": 100,
                            },
                        )

                runner = AgentRunner(
                    model=SlowOrTokenModel(),
                    registry=create_default_registry(),
                    config=make_config(
                        workspace,
                        max_seconds=1 if budget_name == "time" else 30,
                        max_total_tokens=50 if budget_name == "tokens" else 1_000,
                    ),
                    session_store=store,
                )

                result = runner.run("Inspect")

                session = store.load(result.session_id or "")
                self.assertEqual(result.status, "budget_exceeded")
                expected = "max_seconds" if budget_name == "time" else "max_total_tokens"
                self.assertEqual(session.stop_reason, expected)

    def test_missing_provider_usage_is_reported_as_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            store = SessionStore.for_workspace(workspace)
            runner = AgentRunner(
                model=FaultModel([ModelResponse(content="Done")]),
                registry=create_default_registry(),
                config=make_config(workspace),
                session_store=store,
            )

            result = runner.run("Analyze only")

            session = store.load(result.session_id or "")
            self.assertEqual(session.usage_missing_count, 1)
            self.assertIn("usage status: unknown", result.final_text)


class RedactionAndEventTests(unittest.TestCase):
    def test_text_and_structured_payloads_are_redacted(self) -> None:
        fake = "sk-test-secret-123456"
        text = (
            f"Authorization: Bearer {fake}; api_key={fake}; "
            f"https://example.test?a=1&token={fake}"
        )
        redacted = redact_sensitive_text(text)
        self.assertNotIn(fake, redacted)
        self.assertGreaterEqual(redacted.count("[REDACTED]"), 3)
        structured = redact_sensitive_value(
            {"Authorization": f"Bearer {fake}", "nested": {"api_key": fake}}
        )
        self.assertEqual(structured["Authorization"], "[REDACTED]")
        self.assertEqual(structured["nested"]["api_key"], "[REDACTED]")

    def test_event_callback_failure_does_not_break_the_run(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            runner = AgentRunner(
                model=FaultModel([ModelResponse(content="Done")]),
                registry=create_default_registry(),
                config=make_config(workspace),
                event_callback=lambda name, payload: (_ for _ in ()).throw(OSError("disk full")),
            )

            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always")
                result = runner.run("Analyze")

            self.assertEqual(result.status, "completed")
            self.assertTrue(any("Event callback failed" in str(item.message) for item in caught))

    def test_core_events_have_versioned_envelopes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            events = []
            model = FaultModel(
                [
                    ModelResponse(
                        tool_calls=[
                            ToolCall(
                                id="call-events",
                                name="list_files",
                                arguments={"path": "."},
                                raw_arguments='{"path":"."}',
                            )
                        ]
                    ),
                    ModelResponse(content="Done"),
                ]
            )
            runner = AgentRunner(
                model=model,
                registry=create_default_registry(),
                config=make_config(workspace),
                event_callback=lambda name, payload: events.append((name, payload)),
                session_store=SessionStore.for_workspace(workspace),
            )

            runner.run("Inspect")

            names = [name for name, _ in events]
            for required in (
                "run_started",
                "model_request_started",
                "model_response_received",
                "tool_call_requested",
                "tool_call_approved",
                "tool_call_completed",
                "run_completed",
            ):
                self.assertIn(required, names)
            for _, payload in events:
                self.assertEqual(payload["event_schema_version"], 1)
                self.assertTrue(payload["run_id"])
                self.assertIn("timestamp", payload)


class CommandRiskApprovalTests(unittest.TestCase):
    def test_unknown_command_is_not_auto_approved(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            store = SessionStore.for_workspace(workspace)
            marker = workspace / "should-not-exist.txt"
            command = (
                f'python -c "from pathlib import Path; '
                f"Path(r'{marker}').write_text('unsafe')\""
            )
            model = FaultModel(
                [
                    ModelResponse(
                        tool_calls=[
                            ToolCall(
                                id="call-unknown",
                                name="run_command",
                                arguments={"command": command, "purpose": "other"},
                                raw_arguments="{}",
                            )
                        ]
                    ),
                    ModelResponse(content="The command was denied."),
                ]
            )
            runner = AgentRunner(
                model=model,
                registry=create_default_registry(),
                config=make_config(workspace, approval_policy=ApprovalPolicy.AUTO),
                session_store=store,
            )

            result = runner.run("Run an unknown command")

            session = store.load(result.session_id or "")
            self.assertEqual(result.status, "denied")
            self.assertEqual(session.tool_executions[0].risk, "unknown")
            self.assertFalse(marker.exists())

    def test_known_test_command_can_run_in_auto_mode(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            (workspace / "test_smoke.py").write_text(
                "import unittest\n\n"
                "class SmokeTest(unittest.TestCase):\n"
                "    def test_ok(self):\n"
                "        self.assertTrue(True)\n",
                encoding="utf-8",
            )
            store = SessionStore.for_workspace(workspace)
            model = FaultModel(
                [
                    ModelResponse(
                        tool_calls=[
                            ToolCall(
                                id="call-unittest",
                                name="run_command",
                                arguments={
                                    "command": "python -m unittest",
                                    "purpose": "verify",
                                },
                                raw_arguments="{}",
                            )
                        ]
                    ),
                    ModelResponse(content="Tests passed."),
                ]
            )
            runner = AgentRunner(
                model=model,
                registry=create_default_registry(),
                config=make_config(workspace, approval_policy=ApprovalPolicy.AUTO),
                session_store=store,
            )

            result = runner.run("Run tests")

            session = store.load(result.session_id or "")
            self.assertEqual(result.status, "completed")
            self.assertEqual(session.status, SessionStatus.COMPLETED_VERIFIED)
            self.assertEqual(session.tool_executions[0].risk, "workspace_write")

    def test_disposable_auto_mode_can_opt_in_to_unknown_commands(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            store = SessionStore.for_workspace(workspace)
            marker = workspace / "dependency-marker.txt"
            command = (
                f'python -c "from pathlib import Path; '
                f"Path(r'{marker}').write_text('ok')\""
            )
            model = FaultModel(
                [
                    ModelResponse(
                        tool_calls=[
                            ToolCall(
                                id="call-dependency-unknown",
                                name="run_command",
                                arguments={"command": command, "purpose": "other"},
                                raw_arguments="{}",
                            )
                        ]
                    ),
                    ModelResponse(content="Done."),
                ]
            )
            runner = AgentRunner(
                model=model,
                registry=create_default_registry(),
                config=make_config(
                    workspace,
                    approval_policy=ApprovalPolicy.AUTO,
                    auto_approve_unknown_commands=True,
                ),
                session_store=store,
            )

            result = runner.run("Run a command in a disposable test container")

            self.assertEqual(result.status, "completed")
            self.assertEqual(marker.read_text(encoding="utf-8"), "ok")
            session = store.load(result.session_id or "")
            self.assertTrue(session.tool_executions[0].approval_granted)
            self.assertEqual(session.tool_executions[0].risk, "unknown")


if __name__ == "__main__":
    unittest.main()
