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
    command_timeout_seconds: int = 60
    max_tool_output_chars: int = 12_000
    max_context_chars: int = 80_000
    repeated_call_limit: int = 3

    def __post_init__(self) -> None:
        positive_fields = {
            "max_steps": self.max_steps,
            "command_timeout_seconds": self.command_timeout_seconds,
            "max_tool_output_chars": self.max_tool_output_chars,
            "max_context_chars": self.max_context_chars,
        }
        for name, value in positive_fields.items():
            if value < 1:
                raise ConfigurationError(f"{name} must be positive")
        if self.max_context_chars < 2_000:
            raise ConfigurationError("max_context_chars must be at least 2000")
        if self.repeated_call_limit < 2:
            raise ConfigurationError("repeated_call_limit must be at least 2")
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
            command_timeout_seconds=_env_int("CODING_AGENT_COMMAND_TIMEOUT", 60),
            max_tool_output_chars=_env_int("CODING_AGENT_MAX_TOOL_OUTPUT", 12_000),
            max_context_chars=_env_int("CODING_AGENT_CONTEXT_CHARS", 80_000),
            repeated_call_limit=_env_int("CODING_AGENT_REPEATED_CALL_LIMIT", 3, minimum=2),
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
