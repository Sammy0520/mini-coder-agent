from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path
from claw_swebench.claws.base import BaseClawAdapter
from claw_swebench.types import AgentResult

from benchmarks.claw_swe_bench.support import (
    BENCHMARK_INTEGRITY_NOTICE,
    benchmark_integrity_violations,
    classify_infrastructure_failure,
    codex_metrics,
    mini_session_metrics,
    newest_session,
    read_jsonl,
)


PROVIDER_NAME = "aicode007"
DEFAULT_BASE_URL = "https://api.aicode007.com"
DEFAULT_REASONING_EFFORT = "xhigh"
DEFAULT_VERBOSITY = "high"


def _require_path(value: str, label: str, *, directory: bool = True) -> str:
    path = Path(value).expanduser()
    valid = path.is_dir() if directory else path.is_file()
    if not valid:
        kind = "directory" if directory else "file"
        raise FileNotFoundError(f"{label} {kind} does not exist: {path}")
    return str(path.resolve())


def _proxy_args() -> list[str]:
    proxy = (
        os.environ.get("CLAW_RESTRICTED_PROXY_URL")
        or os.environ.get("CLAW_AGENT_PROXY", "")
    ).strip()
    if not proxy:
        return []
    return [
        "-e", f"HTTP_PROXY={proxy}",
        "-e", f"HTTPS_PROXY={proxy}",
        "-e", "NO_PROXY=localhost,127.0.0.1",
    ]


def _network_args() -> list[str]:
    network = os.environ.get("CLAW_AGENT_NETWORK", "").strip()
    return ["--network", network] if network else []


class MiniCoderAdapter(BaseClawAdapter):
    name = "mini-coder"

    def __init__(
        self,
        model: str,
        timeout: int,
        max_turns: int | None = None,
        *,
        base_url: str = DEFAULT_BASE_URL,
        reasoning_effort: str = DEFAULT_REASONING_EFFORT,
        verbosity: str = DEFAULT_VERBOSITY,
    ):
        super().__init__(model, timeout, max_turns)
        self.base_url = base_url
        self.reasoning_effort = reasoning_effort
        self.verbosity = verbosity
        self.python_home = _require_path(
            os.environ.get(
                "MINI_CODER_PYTHON_HOME",
                "/home/sammy/minicoder-eval/python/cpython-3.12.13-linux-x86_64-gnu",
            ),
            "MINI_CODER_PYTHON_HOME",
        )
        self.environment = _require_path(
            os.environ.get("MINI_CODER_ENV_PATH", "/home/sammy/minicoder-eval/agent-env"),
            "MINI_CODER_ENV_PATH",
        )
        self.python = f"{self.environment}/bin/python"
        self.source_root = _require_path(
            str(Path(__file__).resolve().parents[2] / "src"),
            "Mini Coder source root",
        )
        self.container_source_root = "/opt/mini-coder-src"
        self.last_infrastructure_error: str | None = None
        self.last_integrity_violations: list[str] = []

    def container_run_args(self, instance_id: str) -> list[str]:
        return [
            *_network_args(),
            "-v", f"{self.python_home}:{self.python_home}:ro",
            "-v", f"{self.environment}:{self.environment}:ro",
            "-v", f"{self.source_root}:{self.container_source_root}:ro",
            *_proxy_args(),
        ]

    def post_container_start(self, workspace) -> None:
        workspace.run_in_container(
            "cd /testbed && mkdir -p .git/info && "
            "grep -qxF '.mini-coder/' .git/info/exclude 2>/dev/null || "
            "printf '\\n.mini-coder/\\n' >> .git/info/exclude"
        )

    def send_task(
        self,
        prompt: str,
        agent_id: str,
        container_name: str,
        artifact_dir: Path | None = None,
        instance_id: str | None = None,
    ) -> AgentResult:
        if not os.environ.get("OPENAI_API_KEY"):
            raise RuntimeError("OPENAI_API_KEY is required for Mini Coder benchmark runs")
        artifact_dir = artifact_dir or Path.cwd()
        artifact_dir.mkdir(parents=True, exist_ok=True)
        stdout_path = artifact_dir / "mini-coder.stdout.log"
        stderr_path = artifact_dir / "mini-coder.stderr.log"
        event_path = artifact_dir / "mini-coder.events.jsonl"
        session_dir = artifact_dir / "mini-coder-sessions"
        container_event_path = f"/tmp/{agent_id}-mini-coder-events.jsonl"
        max_model_calls = int(os.environ.get("CLAW_MINI_MAX_MODEL_CALLS", "12"))
        max_tool_calls = int(os.environ.get("CLAW_MINI_MAX_TOOL_CALLS", "60"))
        max_total_tokens = int(os.environ.get("CLAW_MINI_MAX_TOTAL_TOKENS", "120000"))
        command = [
            "docker", "exec", "-w", "/testbed",
            "-e", "OPENAI_API_KEY",
            "-e", "HOME=/tmp/mini-coder-home",
            "-e", "CODING_AGENT_PROMPT_CACHE_KEY=swe-bench-verified-easy-v1",
            "-e", f"PYTHONPATH={self.container_source_root}",
            container_name,
            self.python, "-m", "mini_coder",
            "--workspace", "/testbed",
            "--model", self.model,
            "--base-url", self.base_url,
            "--wire-api", "responses",
            "--reasoning-effort", self.reasoning_effort,
            "--verbosity", self.verbosity,
            "--max-steps", str(min(self.max_turns or 300, max_model_calls)),
            "--max-seconds", str(self.timeout),
            "--max-model-calls", str(min(self.max_turns or 300, max_model_calls)),
            "--max-tool-calls", str(max_tool_calls),
            "--max-total-tokens", str(max_total_tokens),
            "--max-retries", "1",
            "--auto",
            "--preserve-project-command-path",
            "--auto-approve-unknown-commands",
            "--external-evaluation",
            "--log", container_event_path,
            f"{BENCHMARK_INTEGRITY_NOTICE}\n{prompt}",
        ]
        start = time.monotonic()
        timed_out = False
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                errors="replace",
                timeout=self.timeout + 30,
                check=False,
            )
            exit_code = completed.returncode
            stdout = completed.stdout or ""
            stderr = completed.stderr or ""
        except subprocess.TimeoutExpired as exc:
            timed_out = True
            exit_code = -1
            stdout = (exc.stdout or "") if isinstance(exc.stdout, str) else ""
            stderr = (exc.stderr or "") if isinstance(exc.stderr, str) else ""
        duration = time.monotonic() - start
        stdout_path.write_text(stdout, encoding="utf-8")
        stderr_path.write_text(stderr, encoding="utf-8")
        subprocess.run(
            ["docker", "cp", f"{container_name}:{container_event_path}", str(event_path)],
            capture_output=True,
            check=False,
        )
        if session_dir.exists():
            shutil.rmtree(session_dir)
        subprocess.run(
            [
                "docker", "cp",
                f"{container_name}:/testbed/.mini-coder/sessions",
                str(session_dir),
            ],
            capture_output=True,
            check=False,
        )
        session = newest_session(session_dir)
        usage = mini_session_metrics(session)
        session_id = str(usage.get("session_id")) if usage.get("session_id") else None
        events = read_jsonl(event_path)
        self.last_infrastructure_error = classify_infrastructure_failure(stdout, stderr)
        self.last_integrity_violations = benchmark_integrity_violations(events)
        if self.last_infrastructure_error:
            finish_reason = "infrastructure_error"
        elif self.last_integrity_violations:
            finish_reason = "integrity_violation"
        else:
            finish_reason = "timeout" if timed_out else ("stop" if exit_code == 0 else "error")
        return AgentResult(
            success=(
                exit_code == 0
                and not timed_out
                and not self.last_infrastructure_error
                and not self.last_integrity_violations
            ),
            timeout=timed_out,
            exit_code=exit_code,
            finish_reason=finish_reason,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            session_id=session_id,
            duration_seconds=duration,
            usage=usage,
        )


class CodexAdapter(BaseClawAdapter):
    name = "codex"

    def __init__(
        self,
        model: str,
        timeout: int,
        max_turns: int | None = None,
        *,
        base_url: str = DEFAULT_BASE_URL,
        reasoning_effort: str = DEFAULT_REASONING_EFFORT,
        verbosity: str = DEFAULT_VERBOSITY,
    ):
        super().__init__(model, timeout, max_turns)
        self.base_url = base_url
        self.reasoning_effort = reasoning_effort
        self.verbosity = verbosity
        self.executable = _require_path(
            os.environ.get(
                "CODEX_BIN", "/home/sammy/minicoder-eval/codex-cli/codex"
            ),
            "CODEX_BIN",
            directory=False,
        )
        self.code_mode_host = _require_path(
            os.environ.get(
                "CODEX_CODE_MODE_HOST",
                str(Path(self.executable).with_name("codex-code-mode-host")),
            ),
            "CODEX_CODE_MODE_HOST",
            directory=False,
        )
        self.last_infrastructure_error: str | None = None
        self.last_integrity_violations: list[str] = []

    def container_run_args(self, instance_id: str) -> list[str]:
        return [
            *_network_args(),
            "-v", f"{self.executable}:{self.executable}:ro",
            "-v", f"{self.code_mode_host}:{self.code_mode_host}:ro",
            *_proxy_args(),
        ]

    def post_container_start(self, workspace) -> None:
        workspace.run_in_container(
            "mkdir -p /root/.codex && chmod 700 /root/.codex"
        )

    def send_task(
        self,
        prompt: str,
        agent_id: str,
        container_name: str,
        artifact_dir: Path | None = None,
        instance_id: str | None = None,
    ) -> AgentResult:
        if not os.environ.get("OPENAI_API_KEY"):
            raise RuntimeError("OPENAI_API_KEY is required for Codex benchmark runs")
        artifact_dir = artifact_dir or Path.cwd()
        artifact_dir.mkdir(parents=True, exist_ok=True)
        stdout_path = artifact_dir / "codex.events.jsonl"
        stderr_path = artifact_dir / "codex.stderr.log"
        overrides = [
            "-c", f'model_provider="{PROVIDER_NAME}"',
            "-c", f'model_providers.{PROVIDER_NAME}.name="{PROVIDER_NAME}"',
            "-c", f'model_providers.{PROVIDER_NAME}.base_url="{self.base_url}"',
            "-c", f'model_providers.{PROVIDER_NAME}.wire_api="responses"',
            "-c", f'model_providers.{PROVIDER_NAME}.env_key="OPENAI_API_KEY"',
            "-c", f'model_providers.{PROVIDER_NAME}.requires_openai_auth=false',
            "-c", f'model_reasoning_effort="{self.reasoning_effort}"',
            "-c", f'model_verbosity="{self.verbosity}"',
        ]
        command = [
            "docker", "exec", "-i", "-w", "/testbed",
            "-e", "OPENAI_API_KEY",
            "-e", "HOME=/root",
            "-e", "CODEX_HOME=/root/.codex",
            "-e", "NO_COLOR=1",
            container_name,
            self.executable,
            "exec",
            "--json",
            "--color", "never",
            # The SWE-bench Docker container is the external sandbox. This
            # avoids nested Linux sandbox incompatibilities while preserving
            # host isolation and matches Mini Coder's --auto container run.
            "--dangerously-bypass-approvals-and-sandbox",
            "--cd", "/testbed",
            "--skip-git-repo-check",
            "--ephemeral",
            "--ignore-user-config",
            "--ignore-rules",
            "--model", self.model,
            *overrides,
            "-",
        ]
        start = time.monotonic()
        timed_out = False
        try:
            completed = subprocess.run(
                command,
                input=f"{BENCHMARK_INTEGRITY_NOTICE}\n{prompt}",
                capture_output=True,
                text=True,
                errors="replace",
                timeout=self.timeout + 30,
                check=False,
            )
            exit_code = completed.returncode
            stdout = completed.stdout or ""
            stderr = completed.stderr or ""
        except subprocess.TimeoutExpired as exc:
            timed_out = True
            exit_code = -1
            stdout = (exc.stdout or "") if isinstance(exc.stdout, str) else ""
            stderr = (exc.stderr or "") if isinstance(exc.stderr, str) else ""
        duration = time.monotonic() - start
        stdout_path.write_text(stdout, encoding="utf-8")
        stderr_path.write_text(stderr, encoding="utf-8")
        events = read_jsonl(stdout_path)
        usage = codex_metrics(events)
        thread_id = str(usage.get("thread_id")) if usage.get("thread_id") else None
        self.last_infrastructure_error = classify_infrastructure_failure(stdout, stderr)
        self.last_integrity_violations = benchmark_integrity_violations(events)
        if self.last_infrastructure_error:
            finish_reason = "infrastructure_error"
        elif self.last_integrity_violations:
            finish_reason = "integrity_violation"
        else:
            finish_reason = "timeout" if timed_out else ("stop" if exit_code == 0 else "error")
        return AgentResult(
            success=(
                exit_code == 0
                and not timed_out
                and not self.last_infrastructure_error
                and not self.last_integrity_violations
            ),
            timeout=timed_out,
            exit_code=exit_code,
            finish_reason=finish_reason,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            session_id=thread_id,
            duration_seconds=duration,
            usage=usage,
        )


def register_adapters() -> None:
    from claw_swebench.claws import CLAWS
    from claw_swebench.config import CLAW_DEFAULTS

    CLAWS["mini-coder"] = MiniCoderAdapter
    CLAWS["codex"] = CodexAdapter
    defaults = {"model": "gpt-5.6-sol", "timeout": 1800, "max_turns": 300}
    CLAW_DEFAULTS["mini-coder"] = dict(defaults)
    CLAW_DEFAULTS["codex"] = dict(defaults)
