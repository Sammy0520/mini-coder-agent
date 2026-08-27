from __future__ import annotations

import locale
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from ..exceptions import ToolError
from .base import RiskLevel, Tool, ToolContext, ToolResult, truncate_text


class RunCommandTool(Tool):
    name = "run_command"
    description = (
        "Run tests, builds, formatters, or other project commands in the host's default shell. "
        "The agent Python environment is first on PATH. Use dedicated file/search tools instead "
        "of shell-based searching, patching, or file writes."
    )
    risk = RiskLevel.EXECUTE
    parameters = {
        "type": "object",
        "properties": {
            "command": {"type": "string"},
            "cwd": {"type": "string", "description": "Workspace-relative directory, default '.'"},
            "timeout_seconds": {"type": "integer", "description": "May reduce but not exceed configured timeout"},
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
        cwd = context.policy.resolve(arguments.get("cwd", "."), must_exist=True)
        if not cwd.is_dir():
            raise ToolError("cwd must be a directory")
        timeout = min(
            max(arguments.get("timeout_seconds", context.command_timeout_seconds), 1),
            context.command_timeout_seconds,
        )
        encoding = locale.getpreferredencoding(False) or "utf-8"
        try:
            completed = subprocess.run(
                command,
                cwd=cwd,
                env=_command_environment(),
                shell=True,
                capture_output=True,
                text=True,
                encoding=encoding,
                errors="replace",
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            stdout = _as_text(exc.stdout, encoding)
            stderr = _as_text(exc.stderr, encoding)
            return ToolResult(
                False,
                f"Command timed out after {timeout} second(s)",
                {
                    "timed_out": True,
                    "stdout": truncate_text(stdout, context.max_output_chars // 2),
                    "stderr": truncate_text(stderr, context.max_output_chars // 2),
                },
            )

        per_stream = max(500, context.max_output_chars // 2)
        return ToolResult(
            completed.returncode == 0,
            f"Command exited with code {completed.returncode}",
            {
                "exit_code": completed.returncode,
                "stdout": truncate_text(completed.stdout, per_stream),
                "stderr": truncate_text(completed.stderr, per_stream),
                "timed_out": False,
            },
        )


def _command_environment() -> dict[str, str]:
    """Build a predictable child environment without exposing model credentials."""
    environment = os.environ.copy()
    runtime_directory = str(Path(sys.executable).absolute().parent)
    existing_path = environment.get("PATH", "")
    path_entries = [entry for entry in existing_path.split(os.pathsep) if entry]
    runtime_key = os.path.normcase(os.path.abspath(runtime_directory))
    path_entries = [
        entry
        for entry in path_entries
        if os.path.normcase(os.path.abspath(entry)) != runtime_key
    ]
    environment["PATH"] = os.pathsep.join([runtime_directory, *path_entries])

    if os.path.normcase(os.path.abspath(sys.prefix)) != os.path.normcase(
        os.path.abspath(sys.base_prefix)
    ):
        environment["VIRTUAL_ENV"] = str(Path(sys.prefix).absolute())

    # Project commands should not automatically inherit the credential used to
    # operate the coding model. Tests that genuinely need a key can receive a
    # separate, deliberately scoped variable from the user.
    environment.pop("OPENAI_API_KEY", None)
    environment.pop("CODING_AGENT_API_KEY", None)
    return environment


def _as_text(value: str | bytes | None, encoding: str) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode(encoding, errors="replace")
    return value
