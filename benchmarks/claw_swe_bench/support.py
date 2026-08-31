from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Iterable


BENCHMARK_INTEGRITY_NOTICE = """Benchmark integrity rules:
- Solve the task only from the problem statement and files already present in /testbed.
- Do not access the public internet or search for the issue, pull request, commit, patch, or solution.
- Do not use curl, wget, network-capable package installers, GitHub APIs, git fetch/pull/clone,
  or another method that retrieves external source code. Such an attempt invalidates the run.
- Make a concrete candidate edit once the relevant implementation and tests are understood;
  do not spend the whole run on broad repository exploration.
"""


_INFRASTRUCTURE_PATTERNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "billing",
        (
            "insufficient balance",
            "billing_error",
            "余额已用尽",
            "quota exceeded",
            "insufficient_quota",
        ),
    ),
    (
        "authentication",
        (
            "authentication_error",
            "invalid_api_key",
            "incorrect api key",
            "invalid api key",
            "401 unauthorized",
            "status 401",
            "http 401",
        ),
    ),
    (
        "provider_access",
        (
            "unexpected status 403 forbidden",
            "permission http 403",
            "status 403 forbidden",
        ),
    ),
    (
        "provider_network",
        (
            "failed to connect to api",
            "connection error while calling the model",
            "model request failed [network",
            "name or service not known",
            "temporary failure in name resolution",
        ),
    ),
)


_NETWORK_COMMAND = re.compile(
    r"(?:^|[;&|\s])(?:curl|wget|aria2c|gh\s+api|git\s+(?:fetch|pull|clone)|"
    r"pip\s+install|python\s+-m\s+pip\s+install|npm\s+(?:install|view)|"
    r"Invoke-WebRequest|Invoke-RestMethod)(?:$|\s)",
    flags=re.IGNORECASE,
)
_PUBLIC_URL = re.compile(r"https?://", flags=re.IGNORECASE)


class ProviderPreflightError(RuntimeError):
    def __init__(self, category: str, message: str):
        super().__init__(message)
        self.category = category


def classify_infrastructure_failure(*texts: str) -> str | None:
    """Return a stable category only for failures outside agent capability."""

    combined = "\n".join(texts).casefold()
    for category, patterns in _INFRASTRUCTURE_PATTERNS:
        if any(pattern.casefold() in combined for pattern in patterns):
            return category
    return None


def provider_preflight(
    *, api_key: str, base_url: str, model: str, timeout: float = 45.0
) -> None:
    """Make one minimal model request so billing/auth failures stop before a run."""

    endpoint = f"{base_url.rstrip('/')}/responses"
    payload = json.dumps(
        {
            "model": model,
            "input": "Reply with exactly OK.",
            "max_output_tokens": 16,
            "reasoning": {"effort": "low"},
            "text": {"verbosity": "low"},
            "store": False,
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        endpoint,
        data=payload,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            response.read(4096)
    except urllib.error.HTTPError as exc:
        detail = exc.read(4096).decode("utf-8", errors="replace")
        category = classify_infrastructure_failure(
            f"HTTP {exc.code} {exc.reason}", detail
        ) or "provider_http"
        raise ProviderPreflightError(
            category,
            f"provider preflight failed ({category}, HTTP {exc.code})",
        ) from exc
    except (OSError, TimeoutError) as exc:
        raise ProviderPreflightError(
            "provider_network", "provider preflight could not reach the model API"
        ) from exc


def benchmark_integrity_violations(
    events: Iterable[dict[str, Any]],
) -> list[str]:
    """Find external-network commands in structured Mini/Codex event logs."""

    violations: list[str] = []
    for event in events:
        candidates: list[str] = []
        item = event.get("item")
        if isinstance(item, dict) and item.get("type") in {
            "command_execution",
            "mcp_tool_call",
        }:
            candidates.extend(
                str(item[key]) for key in ("command", "arguments") if item.get(key)
            )
        data = event.get("data")
        if isinstance(data, dict):
            tool = str(data.get("tool") or data.get("name") or "")
            if tool in {"run_command", "command", "shell"}:
                for key in ("command", "arguments", "input"):
                    if data.get(key):
                        candidates.append(str(data[key]))
        tool = str(event.get("tool") or "")
        if event.get("event") == "tool_call_requested" and tool in {
            "run_command",
            "command",
            "shell",
        }:
            for key in ("arguments", "raw_arguments"):
                if event.get(key):
                    candidates.append(str(event[key]))
        for candidate in candidates:
            if _NETWORK_COMMAND.search(candidate) or _PUBLIC_URL.search(candidate):
                normalized = " ".join(candidate.split())
                if len(normalized) > 240:
                    normalized = normalized[:237] + "..."
                if normalized not in violations:
                    violations.append(normalized)
    return violations


def read_jsonl(path: Path) -> list[dict[str, Any]]:
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


def codex_metrics(events: Iterable[dict[str, Any]]) -> dict[str, Any]:
    usage: dict[str, int] = {}
    turns = 0
    tool_calls = 0
    tool_ids: set[str] = set()
    thread_id: str | None = None
    for event in events:
        event_type = str(event.get("type") or "")
        if event_type == "thread.started" and event.get("thread_id"):
            thread_id = thread_id or str(event["thread_id"])
        if event_type == "turn.completed":
            turns += 1
            for key, value in (event.get("usage") or {}).items():
                if not isinstance(value, int) or isinstance(value, bool):
                    continue
                normalized = {
                    "cached_input_tokens": "cached_tokens",
                    "reasoning_output_tokens": "reasoning_tokens",
                }.get(str(key), str(key))
                usage[normalized] = usage.get(normalized, 0) + value
        item = event.get("item")
        if not isinstance(item, dict):
            continue
        if item.get("type") in {"command_execution", "file_change", "mcp_tool_call"}:
            item_id = item.get("id")
            if item_id:
                tool_ids.add(str(item_id))
            elif event_type.endswith(".completed"):
                tool_calls += 1
    return {
        **usage,
        "turns": turns,
        "model_calls": None,
        "tool_calls": len(tool_ids) + tool_calls,
        "thread_id": thread_id,
    }


def mini_session_metrics(session: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(session, dict):
        return {}
    usage = {
        str(key): value
        for key, value in (session.get("total_usage") or {}).items()
        if isinstance(value, int) and not isinstance(value, bool)
    }
    return {
        **usage,
        "turns": int(session.get("turn_count", 0)),
        "model_calls": int(session.get("model_call_count", 0)),
        "tool_calls": len(session.get("tool_executions") or []),
        "session_id": session.get("session_id"),
    }


def newest_session(directory: Path) -> dict[str, Any] | None:
    if not directory.is_dir():
        return None
    candidates = sorted(directory.glob("*.json"), key=lambda path: path.stat().st_mtime_ns)
    if not candidates:
        return None
    try:
        data = json.loads(candidates[-1].read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None
