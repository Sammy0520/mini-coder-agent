from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import tomllib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from ..redaction import redact_sensitive_text
from .models import (
    AgentKind,
    BenchmarkReport,
    BenchmarkResult,
    BenchmarkTask,
    ComparisonConfig,
)


BENCHMARK_REPORT_SCHEMA = 1
IGNORED_PARTS = {".git", ".mini-coder", "__pycache__", ".pytest_cache"}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class BenchmarkRunner:
    """Run Mini Coder and Codex against identical isolated task fixtures."""

    def __init__(
        self,
        *,
        manifest: Path,
        output_directory: Path,
        mini_config: Path,
        agents: tuple[AgentKind, ...],
        selected_tasks: tuple[str, ...] = (),
        codex_executable: str = "codex",
    ) -> None:
        self.manifest = manifest.expanduser().resolve()
        self.output_directory = output_directory.expanduser().resolve()
        self.mini_config = mini_config.expanduser().resolve()
        self.agents = agents
        self.selected_tasks = selected_tasks
        self.codex_executable = codex_executable

    def load(self) -> tuple[ComparisonConfig, tuple[BenchmarkTask, ...]]:
        data = json.loads(self.manifest.read_text(encoding="utf-8"))
        comparison_data = data.get("comparison") or {}
        comparison = ComparisonConfig(
            model_provider=str(comparison_data["model_provider"]),
            base_url=str(comparison_data["base_url"]),
            wire_api=str(comparison_data["wire_api"]),
            model=str(comparison_data["model"]),
            reasoning_effort=str(comparison_data["reasoning_effort"]),
            verbosity=str(comparison_data["verbosity"]),
        )
        root = self.manifest.parent
        tasks: list[BenchmarkTask] = []
        for raw in data.get("tasks") or []:
            task_id = str(raw["id"])
            if self.selected_tasks and task_id not in self.selected_tasks:
                continue
            fixture = (root / str(raw["fixture"])).resolve()
            hidden_value = raw.get("hidden")
            hidden = (root / str(hidden_value)).resolve() if hidden_value else None
            tasks.append(
                BenchmarkTask(
                    task_id=task_id,
                    title=str(raw["title"]),
                    category=str(raw["category"]),
                    fixture=fixture,
                    hidden=hidden,
                    turns=tuple(str(item) for item in raw["turns"]),
                    validation_command=tuple(str(item) for item in raw["validation_command"]),
                    expected_changed_paths=frozenset(
                        str(item).replace("\\", "/")
                        for item in raw.get("expected_changed_paths") or []
                    ),
                    timeout_seconds=int(raw.get("timeout_seconds", 900)),
                )
            )
        if self.selected_tasks:
            found = {item.task_id for item in tasks}
            missing = sorted(set(self.selected_tasks) - found)
            if missing:
                raise ValueError("unknown benchmark task(s): " + ", ".join(missing))
        if not tasks:
            raise ValueError("benchmark manifest contains no selected tasks")
        self._validate_mini_config(comparison)
        return comparison, tuple(tasks)

    def _validate_mini_config(self, comparison: ComparisonConfig) -> None:
        with self.mini_config.open("rb") as stream:
            data = tomllib.load(stream)
        provider_name = data.get("model_provider")
        providers = data.get("model_providers") or {}
        provider = providers.get(provider_name) if isinstance(providers, dict) else None
        checks = {
            "model_provider": (provider_name, comparison.model_provider),
            "model": (data.get("model"), comparison.model),
            "model_reasoning_effort": (
                data.get("model_reasoning_effort"),
                comparison.reasoning_effort,
            ),
            "model_verbosity": (data.get("model_verbosity"), comparison.verbosity),
            "base_url": (
                provider.get("base_url") if isinstance(provider, dict) else None,
                comparison.base_url,
            ),
            "wire_api": (
                provider.get("wire_api") if isinstance(provider, dict) else None,
                comparison.wire_api,
            ),
        }
        mismatches = [
            f"{name}: config={actual!r}, benchmark={expected!r}"
            for name, (actual, expected) in checks.items()
            if actual != expected
        ]
        if mismatches:
            raise ValueError(
                "Mini Coder config does not match the benchmark comparison: "
                + "; ".join(mismatches)
            )

    def run(self) -> BenchmarkReport:
        comparison, tasks = self.load()
        self.output_directory.mkdir(parents=True, exist_ok=True)
        report = BenchmarkReport(
            schema_version=BENCHMARK_REPORT_SCHEMA,
            started_at=_utc_now(),
            finished_at="",
            comparison=comparison,
        )
        for task in tasks:
            for agent in self.agents:
                result = self._run_one(task, agent, comparison)
                report.results.append(result)
                self._write_json(
                    self.output_directory / f"{task.task_id}-{agent}.json",
                    result.to_dict(),
                )
        report.finished_at = _utc_now()
        self._write_json(self.output_directory / "benchmark-report.json", report.to_dict())
        (self.output_directory / "summary.md").write_text(
            self.render_markdown(report),
            encoding="utf-8",
        )
        return report

    def _run_one(
        self,
        task: BenchmarkTask,
        agent: AgentKind,
        comparison: ComparisonConfig,
    ) -> BenchmarkResult:
        started_at = _utc_now()
        started = time.monotonic()
        artifacts = self.output_directory / task.task_id / agent
        workspace = artifacts / "workspace"
        if artifacts.exists():
            shutil.rmtree(artifacts)
        workspace.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(task.fixture, workspace)
        self._initialize_git(workspace)
        before = self._snapshot(workspace)
        error: str | None = None
        return_codes: list[int] = []
        raw_events: list[dict[str, Any]] = []
        identifier: str | None = None
        try:
            if agent == "mini":
                return_codes, identifier, raw_events = self._run_mini(
                    task, workspace, artifacts
                )
            else:
                return_codes, identifier, raw_events = self._run_codex(
                    task, workspace, artifacts, comparison
                )
        except (OSError, subprocess.SubprocessError, ValueError) as exc:
            error = redact_sensitive_text(str(exc))

        after = self._snapshot(workspace)
        changed = sorted(
            path for path in set(before) | set(after) if before.get(path) != after.get(path)
        )
        validation_passed = self._validate(task, workspace, artifacts)
        process_completed = bool(return_codes) and all(code == 0 for code in return_codes)
        changed_match = set(changed) == set(task.expected_changed_paths)
        (
            usage,
            turns,
            model_calls,
            tool_calls,
            provider_models,
            provider_response_ids,
        ) = self._metrics(agent, raw_events)
        cache = self._cache_metrics(usage)
        passed = error is None and process_completed and validation_passed and changed_match
        return BenchmarkResult(
            task_id=task.task_id,
            title=task.title,
            category=task.category,
            agent=agent,
            started_at=started_at,
            finished_at=_utc_now(),
            passed=passed,
            process_completed=process_completed,
            validation_passed=validation_passed,
            changed_paths_match=changed_match,
            return_codes=return_codes,
            duration_seconds=time.monotonic() - started,
            turns=turns,
            model_calls=model_calls,
            tool_calls=tool_calls,
            usage=usage,
            cache=cache,
            provider_models=provider_models,
            provider_response_ids=provider_response_ids,
            changed_paths=changed,
            expected_changed_paths=sorted(task.expected_changed_paths),
            thread_or_session_id=identifier,
            workspace=str(workspace),
            artifacts=str(artifacts),
            error=error,
        )

    def _run_mini(
        self,
        task: BenchmarkTask,
        workspace: Path,
        artifacts: Path,
    ) -> tuple[list[int], str | None, list[dict[str, Any]]]:
        return_codes: list[int] = []
        events: list[dict[str, Any]] = []
        session_path: Path | None = None
        for index, prompt in enumerate(task.turns, start=1):
            event_path = artifacts / f"mini-turn-{index}.jsonl"
            command = [
                sys.executable,
                "-m",
                "mini_coder",
                prompt,
                "--config",
                str(self.mini_config),
                "--auto",
                "--log",
                str(event_path),
                "--max-seconds",
                str(task.timeout_seconds),
            ]
            if session_path is None:
                command.extend(["--workspace", str(workspace)])
            else:
                command.extend(["--resume", str(session_path)])
            completed = self._run_process(
                command,
                cwd=workspace,
                stdout=artifacts / f"mini-turn-{index}.stdout.txt",
                stderr=artifacts / f"mini-turn-{index}.stderr.txt",
                timeout=task.timeout_seconds + 30,
            )
            return_codes.append(completed)
            events.extend(self._read_jsonl(event_path))
            sessions = sorted(
                (workspace / ".mini-coder" / "sessions").glob("*.json"),
                key=lambda item: item.stat().st_mtime_ns,
            )
            if sessions:
                session_path = sessions[-1]
            if completed != 0:
                break
        identifier = session_path.stem if session_path else None
        if session_path and session_path.is_file():
            session = json.loads(session_path.read_text(encoding="utf-8"))
            events.append({"type": "mini_session", "session": session})
        return return_codes, identifier, events

    def _run_codex(
        self,
        task: BenchmarkTask,
        workspace: Path,
        artifacts: Path,
        comparison: ComparisonConfig,
    ) -> tuple[list[int], str | None, list[dict[str, Any]]]:
        return_codes: list[int] = []
        events: list[dict[str, Any]] = []
        thread_id: str | None = None
        overrides = [
            "-c",
            f'model_provider="{comparison.model_provider}"',
            "-c",
            (
                f'model_providers.{comparison.model_provider}.name='
                f'"{comparison.model_provider}"'
            ),
            "-c",
            (
                f'model_providers.{comparison.model_provider}.base_url='
                f'"{comparison.base_url}"'
            ),
            "-c",
            (
                f'model_providers.{comparison.model_provider}.wire_api='
                f'"{comparison.wire_api}"'
            ),
            "-c",
            f'model_providers.{comparison.model_provider}.env_key="OPENAI_API_KEY"',
            "-c",
            f'model_providers.{comparison.model_provider}.requires_openai_auth=false',
            "-c",
            f'model_reasoning_effort="{comparison.reasoning_effort}"',
            "-c",
            f'model_verbosity="{comparison.verbosity}"',
        ]
        for index, prompt in enumerate(task.turns, start=1):
            event_path = artifacts / f"codex-turn-{index}.jsonl"
            if thread_id is None:
                command = [
                    self.codex_executable,
                    "exec",
                    "--json",
                    "--color",
                    "never",
                    "--sandbox",
                    "workspace-write",
                    "--cd",
                    str(workspace),
                    "--skip-git-repo-check",
                    "--model",
                    comparison.model,
                    *overrides,
                    prompt,
                ]
            else:
                command = [
                    self.codex_executable,
                    "exec",
                    "resume",
                    "--json",
                    "--model",
                    comparison.model,
                    *overrides,
                    thread_id,
                    prompt,
                ]
            completed = self._run_process(
                command,
                cwd=workspace,
                stdout=event_path,
                stderr=artifacts / f"codex-turn-{index}.stderr.txt",
                timeout=task.timeout_seconds + 30,
            )
            return_codes.append(completed)
            turn_events = self._read_jsonl(event_path)
            events.extend(turn_events)
            thread_id = thread_id or self._codex_thread_id(turn_events)
            if completed != 0:
                break
        return return_codes, thread_id, events

    def _run_process(
        self,
        command: list[str],
        *,
        cwd: Path,
        stdout: Path,
        stderr: Path,
        timeout: int,
    ) -> int:
        environment = self._child_environment()
        with stdout.open("w", encoding="utf-8") as out, stderr.open(
            "w", encoding="utf-8"
        ) as err:
            completed = subprocess.run(
                command,
                cwd=cwd,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=out,
                stderr=err,
                text=True,
                errors="replace",
                timeout=timeout,
                check=False,
            )
        return completed.returncode

    def _child_environment(self) -> dict[str, str]:
        """Give both agents the same local credential without exposing it in argv."""
        environment = os.environ.copy()
        source_root = Path(__file__).resolve().parents[2]
        existing_pythonpath = environment.get("PYTHONPATH")
        environment["PYTHONPATH"] = str(source_root) + (
            os.pathsep + existing_pythonpath if existing_pythonpath else ""
        )
        auth_path = self.mini_config.parent / "auth.json"
        if auth_path.is_file():
            data = json.loads(auth_path.read_text(encoding="utf-8-sig"))
            key = data.get("CODING_AGENT_API_KEY") or data.get("OPENAI_API_KEY")
            if isinstance(key, str) and key.strip():
                environment["CODING_AGENT_API_KEY"] = key.strip()
                environment["OPENAI_API_KEY"] = key.strip()
        return environment

    def _validate(self, task: BenchmarkTask, workspace: Path, artifacts: Path) -> bool:
        if task.hidden and task.hidden.is_dir():
            hidden_target = workspace / ".minicoder-bench-hidden"
            shutil.copytree(task.hidden, hidden_target)
        command = [sys.executable if item == "python" else item for item in task.validation_command]
        result = subprocess.run(
            command,
            cwd=workspace,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            errors="replace",
            timeout=120,
            check=False,
            env=self._validation_environment(),
        )
        (artifacts / "validation.txt").write_text(result.stdout, encoding="utf-8")
        return result.returncode == 0

    @staticmethod
    def _validation_environment() -> dict[str, str]:
        environment = os.environ.copy()
        environment.pop("OPENAI_API_KEY", None)
        environment.pop("CODING_AGENT_API_KEY", None)
        return environment

    @staticmethod
    def _metrics(
        agent: AgentKind,
        events: list[dict[str, Any]],
    ) -> tuple[
        dict[str, int],
        int,
        int | None,
        int | None,
        list[str],
        list[str],
    ]:
        if agent == "mini":
            session = next(
                (item.get("session") for item in reversed(events) if item.get("type") == "mini_session"),
                None,
            )
            if not isinstance(session, dict):
                return {}, 0, None, None, [], []
            usage = {
                str(key): int(value)
                for key, value in (session.get("total_usage") or {}).items()
                if isinstance(value, int) and not isinstance(value, bool)
            }
            provider_models = sorted(
                {
                    str(record.get("provider", {}).get("model"))
                    for record in session.get("model_call_records") or []
                    if isinstance(record, dict)
                    and isinstance(record.get("provider"), dict)
                    and record["provider"].get("model")
                }
            )
            provider_response_ids = [
                str(record.get("provider", {}).get("response_id"))
                for record in session.get("model_call_records") or []
                if isinstance(record, dict)
                and isinstance(record.get("provider"), dict)
                and record["provider"].get("response_id")
            ]
            return (
                usage,
                int(session.get("turn_count", 0)),
                int(session.get("model_call_count", 0)),
                len(session.get("tool_executions") or []),
                provider_models,
                provider_response_ids,
            )

        usage: dict[str, int] = {}
        turns = 0
        tool_calls = 0
        provider_models: set[str] = set()
        provider_response_ids: set[str] = set()
        tool_item_ids: set[str] = set()
        for event in events:
            event_type = str(event.get("type") or "")
            if event_type == "turn.completed":
                turns += 1
                for key, value in (event.get("usage") or {}).items():
                    if isinstance(value, int) and not isinstance(value, bool):
                        normalized = {
                            "cached_input_tokens": "cached_tokens",
                            "reasoning_output_tokens": "reasoning_tokens",
                        }.get(str(key), str(key))
                        usage[normalized] = usage.get(normalized, 0) + value
            item = event.get("item")
            if isinstance(item, dict):
                if item.get("type") in {"command_execution", "file_change", "mcp_tool_call"}:
                    item_id = str(item.get("id") or "")
                    if item_id:
                        tool_item_ids.add(item_id)
                    elif event_type.endswith(".completed"):
                        tool_calls += 1
                model = item.get("model")
                if model:
                    provider_models.add(str(model))
            for response_id in (
                event.get("response_id"),
                item.get("response_id") if isinstance(item, dict) else None,
            ):
                if response_id:
                    provider_response_ids.add(str(response_id))
        # Codex JSON reports aggregate usage per completed turn, not the number
        # of underlying provider requests. Keep model_calls unknown rather than
        # presenting the number of user turns as an API-call count.
        return (
            usage,
            turns,
            None,
            len(tool_item_ids) + tool_calls,
            sorted(provider_models),
            sorted(provider_response_ids),
        )

    @staticmethod
    def _cache_metrics(usage: dict[str, int]) -> dict[str, Any]:
        reported_input = usage.get("input_tokens")
        cached = usage.get("cached_tokens")
        if not isinstance(reported_input, int) or not isinstance(cached, int):
            return {
                "reported_input_tokens": reported_input,
                "cached_tokens": cached,
                "cache_reuse_ratio": None,
                "accounting": "unknown",
            }
        if cached <= reported_input:
            effective = reported_input
            uncached = reported_input - cached
            accounting = "cached_included_in_input"
        else:
            effective = reported_input + cached
            uncached = reported_input
            accounting = "cached_reported_separately"
        return {
            "reported_input_tokens": reported_input,
            "cached_tokens": cached,
            "effective_input_tokens": effective,
            "uncached_input_tokens": uncached,
            "cache_reuse_ratio": cached / effective if effective else 0.0,
            "accounting": accounting,
        }

    @staticmethod
    def _codex_thread_id(events: Iterable[dict[str, Any]]) -> str | None:
        for event in events:
            if event.get("type") == "thread.started" and event.get("thread_id"):
                return str(event["thread_id"])
        return None

    @staticmethod
    def _initialize_git(workspace: Path) -> None:
        """Stop Git-aware agents from discovering the benchmark's parent repo."""
        commands = (
            ["git", "init", "--quiet"],
            ["git", "add", "--all"],
            [
                "git",
                "-c",
                "user.name=MiniCoderBench",
                "-c",
                "user.email=benchmark@invalid.local",
                "commit",
                "--quiet",
                "--no-gpg-sign",
                "-m",
                "benchmark baseline",
            ],
        )
        for command in commands:
            completed = subprocess.run(
                command,
                cwd=workspace,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                errors="replace",
                timeout=30,
                check=False,
            )
            if completed.returncode != 0:
                raise OSError(
                    "cannot create isolated benchmark Git baseline: "
                    + (completed.stderr.strip() or "git command failed")
                )

    @staticmethod
    def _read_jsonl(path: Path) -> list[dict[str, Any]]:
        if not path.is_file():
            return []
        result: list[dict[str, Any]] = []
        for line in path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(item, dict):
                result.append(item)
        return result

    @staticmethod
    def _snapshot(workspace: Path) -> dict[str, str]:
        result: dict[str, str] = {}
        for path in workspace.rglob("*"):
            if not path.is_file() or any(part in IGNORED_PARTS for part in path.parts):
                continue
            relative = path.relative_to(workspace).as_posix()
            result[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
        return result

    @staticmethod
    def _write_json(path: Path, value: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(value, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    @staticmethod
    def render_markdown(report: BenchmarkReport) -> str:
        lines = [
            "# MiniCoderBench comparison",
            "",
            (
                f"Both agents: `{report.comparison.model_provider}` / "
                f"`{report.comparison.base_url}` / `{report.comparison.model}` / "
                f"`{report.comparison.reasoning_effort}`."
            ),
            "Actual cost must be copied from the aicode007 billing panel; local token fields are evidence, not an invoice.",
            "",
            "| Task | Agent | Pass | Time | Turns | Model calls | Tool calls | Input | Cached | Cache reuse | Actual cost |",
            "|---|---:|:---:|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
        for item in report.results:
            ratio = item.cache.get("cache_reuse_ratio")
            ratio_text = f"{ratio:.1%}" if isinstance(ratio, float) else "n/a"
            lines.append(
                f"| {item.task_id} | {item.agent} | {'yes' if item.passed else 'no'} | "
                f"{item.duration_seconds:.1f}s | {item.turns} | {item.model_calls or 'n/a'} | "
                f"{item.tool_calls if item.tool_calls is not None else 'n/a'} | "
                f"{item.usage.get('input_tokens', 'n/a')} | "
                f"{item.usage.get('cached_tokens', 'n/a')} | {ratio_text} | pending |"
            )
        lines.extend(
            [
                "",
                "## Billing reconciliation",
                "",
                "Fill `actual_cost` in the JSON report from aicode007 using each run's timestamps and provider response IDs. Prefer separate keys in the same billing group when available.",
                "",
            ]
        )
        return "\n".join(lines)
