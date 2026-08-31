from __future__ import annotations

import locale
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

from ..exceptions import ToolError
from ..redaction import redact_sensitive_text
from .base import RiskLevel, Tool, ToolContext, ToolResult, truncate_text
from .command_risk import CommandRisk, assess_command


class RunCommandTool(Tool):
    name = "run_command"
    description = (
        "Run tests, builds, formatters, or other project commands in the host's default shell. "
        "The agent Python environment is first on PATH. Use dedicated file/search tools instead "
        "of shell-based searching, patching, or source-file writes. For negative tests, declare "
        "the accepted process codes with expected_exit_codes."
    )
    risk = RiskLevel.EXECUTE
    parameters = {
        "type": "object",
        "properties": {
            "command": {"type": "string"},
            "cwd": {"type": "string", "description": "Workspace-relative directory, default '.'"},
            "timeout_seconds": {"type": "integer", "description": "May reduce but not exceed configured timeout"},
            "purpose": {
                "type": "string",
                "enum": ["inspect", "verify", "other"],
                "description": "Use 'verify' for tests, builds, linters, and other acceptance checks",
            },
            "expected_exit_codes": {
                "type": "array",
                "items": {"type": "integer"},
                "minItems": 1,
                "maxItems": 16,
                "uniqueItems": True,
                "description": (
                    "Process exit codes that mean this command behaved as expected; default [0]. "
                    "Use a non-zero value only for a deliberate negative-path acceptance check. "
                    "Never use it to reclassify a test failure, import error, missing dependency, "
                    "or unavailable environment as a passing verification."
                ),
            },
            "verification_paths": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Optional workspace files or directories covered by this check. "
                    "Use this to keep unrelated successful checks current."
                ),
            },
            "verification_domains": {
                "type": "array",
                "items": {
                    "type": "string",
                    "enum": ["python", "web", "docs", "native", "config", "all"],
                },
                "description": "Optional technology areas covered by this check.",
            },
        },
        "required": ["command"],
        "additionalProperties": False,
    }

    def execute(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        command = arguments["command"].strip()
        if not command:
            raise ToolError("command must not be empty")
        if len(command) > 4000:
            raise ToolError("command is too long")
        expected_exit_codes = _expected_exit_codes(arguments.get("expected_exit_codes"))
        cwd = context.policy.resolve(arguments.get("cwd", "."), must_exist=True)
        if not cwd.is_dir():
            raise ToolError("cwd must be a directory")
        timeout = min(
            max(arguments.get("timeout_seconds", context.command_timeout_seconds), 1),
            context.command_timeout_seconds,
        )
        encoding = locale.getpreferredencoding(False) or "utf-8"
        assessment = assess_command(command)
        started_at = time.monotonic()
        process: subprocess.Popen[str] | None = None
        collector: threading.Thread | None = None
        collected: dict[str, tuple[str | bytes | None, str | bytes | None]] = {}
        collection_error: list[BaseException] = []
        creationflags = (
            getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) if os.name == "nt" else 0
        )
        try:
            process = subprocess.Popen(
                command,
                cwd=cwd,
                env=_command_environment(
                    context.runtime_directory,
                    preserve_project_path=context.preserve_project_command_path,
                ),
                shell=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding=encoding,
                errors="replace",
                start_new_session=os.name != "nt",
                creationflags=creationflags,
            )
            def collect_output() -> None:
                try:
                    collected["streams"] = process.communicate()
                except BaseException as exc:  # propagated on the calling thread below
                    collection_error.append(exc)

            collector = threading.Thread(
                target=collect_output,
                name=f"mini-coder-command-{process.pid}",
                daemon=True,
            )
            collector.start()
            deadline = started_at + timeout
            while collector.is_alive():
                if context.is_cancelled():
                    duration_seconds = time.monotonic() - started_at
                    terminated = _terminate_process_tree(process)
                    _invalidate_after_command(context, assessment.level)
                    return _stopped_result(
                        process,
                        "",
                        "",
                        encoding=encoding,
                        context=context,
                        assessment=assessment,
                        duration_seconds=duration_seconds,
                        terminated=terminated,
                    )
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(command, timeout)
                collector.join(timeout=min(0.1, remaining))
            if collection_error:
                raise collection_error[0]
            stdout, stderr = collected.get("streams", ("", ""))
        except subprocess.TimeoutExpired:
            duration_seconds = time.monotonic() - started_at
            if process is None:
                raise
            terminated = _terminate_process_tree(process)
            _close_process_streams(process)
            if collector is not None:
                collector.join(timeout=0.5)
            stdout, stderr = collected.get("streams", ("", ""))
            stdout_text, stdout_truncated = _truncate_stream(
                redact_sensitive_text(_as_text(stdout, encoding)),
                context.max_output_chars // 2,
            )
            stderr_text, stderr_truncated = _truncate_stream(
                redact_sensitive_text(_as_text(stderr, encoding)),
                context.max_output_chars // 2,
            )
            _invalidate_after_command(context, assessment.level)
            return ToolResult(
                False,
                f"Command timed out after {timeout} second(s)",
                {
                    "timed_out": True,
                    "exit_code": process.returncode,
                    "duration_seconds": duration_seconds,
                    "stdout": stdout_text,
                    "stderr": stderr_text,
                    "stdout_truncated": stdout_truncated,
                    "stderr_truncated": stderr_truncated,
                    "output_truncated": stdout_truncated or stderr_truncated,
                    "process_tree_terminated": terminated,
                    "command_risk": assessment.level.value,
                    "expected_side_effects": assessment.expected_side_effects,
                    "expected_exit_codes": expected_exit_codes,
                    "expectation_met": False,
                },
            )
        except KeyboardInterrupt:
            if process is not None:
                _terminate_process_tree(process)
                _close_process_streams(process)
            raise

        per_stream = max(500, context.max_output_chars // 2)
        if process is None:
            raise ToolError("command process did not start")
        duration_seconds = time.monotonic() - started_at
        stdout_text, stdout_truncated = _truncate_stream(
            redact_sensitive_text(stdout), per_stream
        )
        stderr_text, stderr_truncated = _truncate_stream(
            redact_sensitive_text(stderr), per_stream
        )
        expectation_met = process.returncode in expected_exit_codes
        _invalidate_after_command(context, assessment.level)
        return ToolResult(
            expectation_met,
            (
                f"Command met the expected exit code {process.returncode}"
                if expectation_met
                else (
                    f"Command exited with code {process.returncode}; expected one of "
                    f"{expected_exit_codes}"
                )
            ),
            {
                "exit_code": process.returncode,
                "stdout": stdout_text,
                "stderr": stderr_text,
                "timed_out": False,
                "duration_seconds": duration_seconds,
                "stdout_truncated": stdout_truncated,
                "stderr_truncated": stderr_truncated,
                "output_truncated": stdout_truncated or stderr_truncated,
                "process_tree_terminated": False,
                "command_risk": assessment.level.value,
                "expected_side_effects": assessment.expected_side_effects,
                "expected_exit_codes": expected_exit_codes,
                "expectation_met": expectation_met,
            },
        )


def _invalidate_after_command(context: ToolContext, risk: CommandRisk) -> None:
    if risk != CommandRisk.READ_ONLY:
        context.invalidate_observations()


def _command_environment(
    agent_runtime_directory: Path | None = None,
    *,
    preserve_project_path: bool = False,
) -> dict[str, str]:
    """Build a predictable child environment without exposing model credentials."""
    environment = os.environ.copy()
    if not preserve_project_path:
        python_runtime_directory = str(Path(sys.executable).absolute().parent)
        existing_path = environment.get("PATH", "")
        path_entries = [entry for entry in existing_path.split(os.pathsep) if entry]
        runtime_key = os.path.normcase(os.path.abspath(python_runtime_directory))
        path_entries = [
            entry
            for entry in path_entries
            if os.path.normcase(os.path.abspath(entry)) != runtime_key
        ]
        environment["PATH"] = os.pathsep.join([python_runtime_directory, *path_entries])

        if os.path.normcase(os.path.abspath(sys.prefix)) != os.path.normcase(
            os.path.abspath(sys.base_prefix)
        ):
            environment["VIRTUAL_ENV"] = str(Path(sys.prefix).absolute())

    # Project commands should not automatically inherit the credential used to
    # operate the coding model. Tests that genuinely need a key can receive a
    # separate, deliberately scoped variable from the user.
    environment.pop("OPENAI_API_KEY", None)
    environment.pop("CODING_AGENT_API_KEY", None)
    environment.pop("MINI_CODER_RUNTIME_DIR", None)
    if agent_runtime_directory is not None:
        agent_runtime_directory.mkdir(parents=True, exist_ok=True)
        environment["MINI_CODER_RUNTIME_DIR"] = str(
            agent_runtime_directory.resolve()
        )
    return environment


def _expected_exit_codes(value: Any) -> list[int]:
    if value is None:
        return [0]
    if not isinstance(value, list) or not value or len(value) > 16:
        raise ToolError("expected_exit_codes must contain between 1 and 16 integers")
    result: list[int] = []
    for item in value:
        if not isinstance(item, int) or isinstance(item, bool):
            raise ToolError("expected_exit_codes must contain only integers")
        if item not in result:
            result.append(item)
    if len(result) != len(value):
        raise ToolError("expected_exit_codes must not contain duplicates")
    return result


def _as_text(value: str | bytes | None, encoding: str) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode(encoding, errors="replace")
    return value


def _stopped_result(
    process: subprocess.Popen[str],
    stdout: str | bytes | None,
    stderr: str | bytes | None,
    *,
    encoding: str,
    context: ToolContext,
    assessment: Any,
    duration_seconds: float,
    terminated: bool,
) -> ToolResult:
    per_stream = max(500, context.max_output_chars // 2)
    stdout_text, stdout_truncated = _truncate_stream(
        redact_sensitive_text(_as_text(stdout, encoding)),
        per_stream,
    )
    stderr_text, stderr_truncated = _truncate_stream(
        redact_sensitive_text(_as_text(stderr, encoding)),
        per_stream,
    )
    return ToolResult(
        False,
        "Command stopped by user",
        {
            "cancelled": True,
            "exit_code": process.returncode,
            "stdout": stdout_text,
            "stderr": stderr_text,
            "timed_out": False,
            "duration_seconds": duration_seconds,
            "stdout_truncated": stdout_truncated,
            "stderr_truncated": stderr_truncated,
            "output_truncated": stdout_truncated or stderr_truncated,
            "process_tree_terminated": terminated,
            "command_risk": assessment.level.value,
            "expected_side_effects": assessment.expected_side_effects,
        },
    )


def _truncate_stream(text: str, limit: int) -> tuple[str, bool]:
    return truncate_text(text, limit), len(text) > limit


def _terminate_process_tree(process: subprocess.Popen[str]) -> bool:
    if process.poll() is not None:
        return True
    try:
        if os.name == "nt":
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=2,
                creationflags=flags,
            )
        else:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
        if process.poll() is None:
            process.kill()
        return True
    except (OSError, subprocess.SubprocessError):
        try:
            process.kill()
            return True
        except OSError:
            return False


def _close_process_streams(process: subprocess.Popen[str]) -> None:
    for stream in (process.stdout, process.stderr):
        if stream is not None:
            try:
                stream.close()
            except OSError:
                pass
