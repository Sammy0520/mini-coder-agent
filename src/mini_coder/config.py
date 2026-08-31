from __future__ import annotations

import json
import os
import tomllib
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from .exceptions import ConfigurationError


class ApprovalPolicy(str, Enum):
    SAFE = "safe"
    AUTO = "auto"


class WireAPI(str, Enum):
    CHAT_COMPLETIONS = "chat_completions"
    RESPONSES = "responses"


def _env_int(name: str, default: int, *, minimum: int = 1) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be an integer") from exc
    if value < minimum:
        raise ConfigurationError(f"{name} must be at least {minimum}")
    return value


def _env_float(name: str, default: float, *, minimum: float = 0.0) -> float:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be a number") from exc
    if value < minimum:
        raise ConfigurationError(f"{name} must be at least {minimum}")
    return value


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    normalized = raw.strip().casefold()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ConfigurationError(f"{name} must be true or false")


@dataclass(frozen=True, slots=True)
class AgentConfig:
    workspace: Path
    api_key: str | None
    base_url: str | None
    model: str | None
    model_provider: str | None = None
    wire_api: WireAPI = WireAPI.CHAT_COMPLETIONS
    model_reasoning_effort: str | None = None
    model_verbosity: str | None = None
    requires_openai_auth: bool = True
    approval_policy: ApprovalPolicy = ApprovalPolicy.SAFE
    max_steps: int = 20
    max_seconds: int = 900
    max_model_calls: int = 40
    max_tool_calls: int = 100
    command_timeout_seconds: int = 60
    max_tool_output_chars: int = 12_000
    max_total_tool_output_chars: int = 200_000
    max_total_tokens: int = 500_000
    max_context_chars: int = 80_000
    max_context_tokens: int = 20_000
    repeated_call_limit: int = 3
    max_model_retries: int = 2
    retry_base_seconds: float = 0.5
    retry_max_seconds: float = 8.0
    model_timeout_seconds: float = 120.0
    model_streaming: bool = True
    prompt_cache_enabled: bool = True
    prompt_cache_key: str = "mini-coder-agent-v1"
    max_response_tool_calls: int = 8
    max_response_write_calls: int = 3
    max_response_write_chars: int = 18_000
    preserve_project_command_path: bool = False
    auto_approve_unknown_commands: bool = False
    external_evaluation: bool = False

    def __post_init__(self) -> None:
        positive_fields = {
            "max_steps": self.max_steps,
            "max_seconds": self.max_seconds,
            "max_model_calls": self.max_model_calls,
            "max_tool_calls": self.max_tool_calls,
            "command_timeout_seconds": self.command_timeout_seconds,
            "max_tool_output_chars": self.max_tool_output_chars,
            "max_total_tool_output_chars": self.max_total_tool_output_chars,
            "max_total_tokens": self.max_total_tokens,
            "max_context_chars": self.max_context_chars,
            "max_context_tokens": self.max_context_tokens,
            "max_response_tool_calls": self.max_response_tool_calls,
            "max_response_write_calls": self.max_response_write_calls,
            "max_response_write_chars": self.max_response_write_chars,
        }
        for name, value in positive_fields.items():
            if value < 1:
                raise ConfigurationError(f"{name} must be positive")
        if self.max_context_chars < 2_000:
            raise ConfigurationError("max_context_chars must be at least 2000")
        if self.repeated_call_limit < 2:
            raise ConfigurationError("repeated_call_limit must be at least 2")
        if self.max_model_retries < 0:
            raise ConfigurationError("max_model_retries must not be negative")
        if self.retry_base_seconds < 0 or self.retry_max_seconds < 0:
            raise ConfigurationError("retry delays must not be negative")
        if self.retry_max_seconds < self.retry_base_seconds:
            raise ConfigurationError("retry_max_seconds must be >= retry_base_seconds")
        if self.model_timeout_seconds <= 0:
            raise ConfigurationError("model_timeout_seconds must be positive")
        if not self.prompt_cache_key.strip():
            raise ConfigurationError("prompt_cache_key must not be empty")
        if self.model_reasoning_effort not in {None, "none", "low", "medium", "high", "xhigh", "max"}:
            raise ConfigurationError(
                "model_reasoning_effort must be one of: none, low, medium, high, xhigh, max"
            )
        if self.model_verbosity not in {None, "low", "medium", "high"}:
            raise ConfigurationError("model_verbosity must be one of: low, medium, high")
        if self.base_url and (self.base_url.startswith("[") or "](" in self.base_url):
            raise ConfigurationError(
                "base_url must be a plain URL, not Markdown link syntax"
            )

    @classmethod
    def from_env(
        cls,
        workspace: str | Path = ".",
        *,
        approval_policy: ApprovalPolicy = ApprovalPolicy.SAFE,
        model: str | None = None,
        base_url: str | None = None,
        config_path: str | Path | None = None,
        wire_api: str | WireAPI | None = None,
        reasoning_effort: str | None = None,
        verbosity: str | None = None,
        max_steps: int | None = None,
        max_seconds: int | None = None,
        max_model_calls: int | None = None,
        max_tool_calls: int | None = None,
        max_tool_output_chars: int | None = None,
        max_total_tool_output_chars: int | None = None,
        max_total_tokens: int | None = None,
        max_model_retries: int | None = None,
        preserve_project_command_path: bool = False,
        auto_approve_unknown_commands: bool = False,
        external_evaluation: bool = False,
    ) -> "AgentConfig":
        file_data = _load_toml(config_path)
        provider_name = file_data.get("model_provider")
        provider = _provider_table(file_data, provider_name)
        selected_wire_api = (
            wire_api
            or os.getenv("CODING_AGENT_WIRE_API")
            or provider.get("wire_api")
            or WireAPI.CHAT_COMPLETIONS.value
        )
        try:
            wire_api_value = WireAPI(selected_wire_api)
        except ValueError as exc:
            raise ConfigurationError(
                "wire_api must be 'responses' or 'chat_completions'"
            ) from exc
        return cls(
            workspace=Path(workspace).expanduser().resolve(),
            api_key=_load_api_key(config_path),
            base_url=(
                base_url
                or os.getenv("CODING_AGENT_BASE_URL")
                or _optional_string(provider.get("base_url"), "base_url")
            ),
            model=(
                model
                or os.getenv("CODING_AGENT_MODEL")
                or _optional_string(file_data.get("model"), "model")
            ),
            model_provider=_optional_string(provider_name, "model_provider"),
            wire_api=wire_api_value,
            model_reasoning_effort=(
                reasoning_effort
                or os.getenv("CODING_AGENT_REASONING_EFFORT")
                or _optional_string(file_data.get("model_reasoning_effort"), "model_reasoning_effort")
            ),
            model_verbosity=(
                verbosity
                or os.getenv("CODING_AGENT_VERBOSITY")
                or _optional_string(file_data.get("model_verbosity"), "model_verbosity")
            ),
            requires_openai_auth=_optional_bool(
                provider.get("requires_openai_auth"),
                "requires_openai_auth",
                default=True,
            ),
            approval_policy=approval_policy,
            max_steps=(
                max_steps
                if max_steps is not None
                else _env_int("CODING_AGENT_MAX_STEPS", 20)
            ),
            max_seconds=(
                max_seconds
                if max_seconds is not None
                else _env_int("CODING_AGENT_MAX_SECONDS", 900)
            ),
            max_model_calls=(
                max_model_calls
                if max_model_calls is not None
                else _env_int("CODING_AGENT_MAX_MODEL_CALLS", 40)
            ),
            max_tool_calls=(
                max_tool_calls
                if max_tool_calls is not None
                else _env_int("CODING_AGENT_MAX_TOOL_CALLS", 100)
            ),
            command_timeout_seconds=_env_int("CODING_AGENT_COMMAND_TIMEOUT", 60),
            max_tool_output_chars=(
                max_tool_output_chars
                if max_tool_output_chars is not None
                else _env_int("CODING_AGENT_MAX_TOOL_OUTPUT", 12_000)
            ),
            max_total_tool_output_chars=(
                max_total_tool_output_chars
                if max_total_tool_output_chars is not None
                else _env_int("CODING_AGENT_MAX_TOTAL_TOOL_OUTPUT", 200_000)
            ),
            max_total_tokens=(
                max_total_tokens
                if max_total_tokens is not None
                else _env_int("CODING_AGENT_MAX_TOTAL_TOKENS", 500_000)
            ),
            preserve_project_command_path=preserve_project_command_path,
            auto_approve_unknown_commands=auto_approve_unknown_commands,
            external_evaluation=external_evaluation,
            max_context_chars=_env_int("CODING_AGENT_CONTEXT_CHARS", 80_000),
            max_context_tokens=_env_int("CODING_AGENT_CONTEXT_TOKENS", 20_000),
            repeated_call_limit=_env_int("CODING_AGENT_REPEATED_CALL_LIMIT", 3, minimum=2),
            max_model_retries=(
                max_model_retries
                if max_model_retries is not None
                else _env_int("CODING_AGENT_MAX_RETRIES", 2, minimum=0)
            ),
            retry_base_seconds=_env_float("CODING_AGENT_RETRY_BASE_SECONDS", 0.5),
            retry_max_seconds=_env_float("CODING_AGENT_RETRY_MAX_SECONDS", 8.0),
            model_timeout_seconds=_env_float(
                "CODING_AGENT_MODEL_TIMEOUT_SECONDS",
                120.0,
                minimum=0.001,
            ),
            model_streaming=_env_bool(
                "CODING_AGENT_STREAMING",
                _optional_bool(file_data.get("model_streaming"), "model_streaming", default=True),
            ),
            prompt_cache_enabled=_env_bool(
                "CODING_AGENT_PROMPT_CACHE",
                _optional_bool(
                    file_data.get("prompt_cache_enabled"),
                    "prompt_cache_enabled",
                    default=True,
                ),
            ),
            prompt_cache_key=(
                os.getenv("CODING_AGENT_PROMPT_CACHE_KEY")
                or _optional_string(file_data.get("prompt_cache_key"), "prompt_cache_key")
                or "mini-coder-agent-v1"
            ),
            max_response_tool_calls=_env_int(
                "CODING_AGENT_MAX_RESPONSE_TOOL_CALLS", 8
            ),
            max_response_write_calls=_env_int(
                "CODING_AGENT_MAX_RESPONSE_WRITE_CALLS", 3
            ),
            max_response_write_chars=_env_int(
                "CODING_AGENT_MAX_RESPONSE_WRITE_CHARS", 18_000
            ),
        )

    def validate_for_model(self) -> None:
        if not self.workspace.is_dir():
            raise ConfigurationError(f"Workspace is not a directory: {self.workspace}")
        if self.requires_openai_auth and not self.api_key:
            raise ConfigurationError(
                "Missing CODING_AGENT_API_KEY (or OPENAI_API_KEY). "
                "For a local compatible server, set it to any value the server accepts."
            )
        if not self.model:
            raise ConfigurationError("Missing CODING_AGENT_MODEL")


def _load_toml(config_path: str | Path | None) -> dict[str, Any]:
    if config_path is None:
        return {}
    path = Path(config_path).expanduser().resolve()
    if not path.is_file():
        raise ConfigurationError(f"Config file does not exist: {path}")
    try:
        with path.open("rb") as stream:
            data = tomllib.load(stream)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigurationError(f"Invalid TOML in {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigurationError("Config root must be a TOML table")
    return data


def _load_api_key(config_path: str | Path | None) -> str | None:
    """Load credentials without putting them in the shared provider TOML.

    Environment variables intentionally win so a developer or CI job can
    override the local credential file for a single process.
    """
    environment_key = os.getenv("CODING_AGENT_API_KEY") or os.getenv("OPENAI_API_KEY")
    if environment_key:
        return environment_key

    if config_path is None:
        auth_path = Path.cwd() / "auth.json"
    else:
        auth_path = Path(config_path).expanduser().resolve().parent / "auth.json"
    if not auth_path.is_file():
        return None

    try:
        with auth_path.open("r", encoding="utf-8-sig") as stream:
            data = json.load(stream)
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigurationError(f"Cannot read local credentials from {auth_path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigurationError(f"Local credentials must be a JSON object: {auth_path}")

    auth_mode = data.get("auth_mode", "apikey")
    if auth_mode != "apikey":
        raise ConfigurationError(
            f"Unsupported auth_mode in {auth_path}; expected 'apikey'"
        )
    key = data.get("CODING_AGENT_API_KEY") or data.get("OPENAI_API_KEY")
    if not isinstance(key, str) or not key.strip():
        raise ConfigurationError(
            f"Local credentials in {auth_path} must contain OPENAI_API_KEY"
        )
    return key.strip()


def _provider_table(data: dict[str, Any], provider_name: Any) -> dict[str, Any]:
    if provider_name is None:
        return {}
    if not isinstance(provider_name, str) or not provider_name:
        raise ConfigurationError("model_provider must be a non-empty string")
    providers = data.get("model_providers")
    if not isinstance(providers, dict):
        raise ConfigurationError("model_providers table is required when model_provider is set")
    provider = providers.get(provider_name)
    if not isinstance(provider, dict):
        raise ConfigurationError(f"Missing [model_providers.{provider_name}] table")
    return provider


def _optional_string(value: Any, name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise ConfigurationError(f"{name} must be a non-empty string")
    return value


def _optional_bool(value: Any, name: str, *, default: bool) -> bool:
    if value is None:
        return default
    if not isinstance(value, bool):
        raise ConfigurationError(f"{name} must be true or false")
    return value
