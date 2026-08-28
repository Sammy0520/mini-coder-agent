from __future__ import annotations

import copy
import json
from collections.abc import Sequence
from typing import Any

from ..config import WireAPI
from ..exceptions import ConfigurationError, ModelError, ModelProtocolError
from ..messages import ModelResponse, ToolCall
from .errors import classify_model_exception
from .base import ModelClient


class OpenAICompatibleClient(ModelClient):
    """Adapter for OpenAI-compatible Chat Completions and Responses endpoints."""

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        base_url: str | None = None,
        wire_api: WireAPI | str = WireAPI.CHAT_COMPLETIONS,
        reasoning_effort: str | None = None,
        verbosity: str | None = None,
        timeout_seconds: float = 120.0,
    ) -> None:
        try:
            from openai import OpenAI
        except ImportError as exc:
            raise ConfigurationError(
                "The 'openai' package is required for live model calls. "
                "Install the project with: python -m pip install -e ."
            ) from exc

        try:
            self._wire_api = WireAPI(wire_api)
        except ValueError as exc:
            raise ConfigurationError(
                "wire_api must be 'responses' or 'chat_completions'"
            ) from exc
        kwargs: dict[str, Any] = {
            "api_key": api_key,
            "timeout": timeout_seconds,
            # Retry policy belongs to AgentRunner so attempts, budgets, events,
            # and Session state remain observable and deterministic.
            "max_retries": 0,
        }
        if base_url:
            kwargs["base_url"] = base_url
        self._client = OpenAI(**kwargs)
        self._redaction_secrets = (api_key,)
        self._model = model
        self._reasoning_effort = reasoning_effort
        self._verbosity = verbosity

    def complete(
        self,
        messages: Sequence[dict[str, Any]],
        tools: Sequence[dict[str, Any]],
    ) -> ModelResponse:
        try:
            if self._wire_api == WireAPI.RESPONSES:
                return self._complete_responses(messages, tools)
            return self._complete_chat_completions(messages, tools)
        except ModelProtocolError:
            raise
        except Exception as exc:  # SDK/provider exceptions vary across compatible services.
            raise classify_model_exception(
                exc,
                secrets=self._redaction_secrets,
            ) from exc

    def _complete_chat_completions(
        self,
        messages: Sequence[dict[str, Any]],
        tools: Sequence[dict[str, Any]],
    ) -> ModelResponse:
        kwargs: dict[str, Any] = {
            "model": self._model,
            "messages": [self._chat_message(message) for message in messages],
            "tools": list(tools),
            "tool_choice": "auto",
        }
        if self._reasoning_effort:
            kwargs["reasoning_effort"] = self._reasoning_effort
        completion = self._client.chat.completions.create(**kwargs)
        return self._parse_completion(completion)

    def _complete_responses(
        self,
        messages: Sequence[dict[str, Any]],
        tools: Sequence[dict[str, Any]],
    ) -> ModelResponse:
        kwargs: dict[str, Any] = {
            "model": self._model,
            "input": self._to_responses_input(messages),
            "tools": self._to_responses_tools(tools),
            "tool_choice": "auto",
        }
        if self._reasoning_effort:
            kwargs["reasoning"] = {"effort": self._reasoning_effort}
        if self._verbosity:
            kwargs["text"] = {"verbosity": self._verbosity}
        response = self._client.responses.create(**kwargs)
        return self._parse_response(response)

    @staticmethod
    def _chat_message(message: dict[str, Any]) -> dict[str, Any]:
        return {key: copy.deepcopy(value) for key, value in message.items() if not key.startswith("_")}

    @staticmethod
    def _to_responses_tools(tools: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
        converted: list[dict[str, Any]] = []
        for tool in tools:
            function = tool.get("function")
            if tool.get("type") != "function" or not isinstance(function, dict):
                raise ModelProtocolError("Only JSON-schema function tools are supported")
            item = {
                "type": "function",
                "name": function.get("name"),
                "description": function.get("description", ""),
                "parameters": function.get("parameters", {"type": "object", "properties": {}}),
            }
            if "strict" in function:
                item["strict"] = function["strict"]
            converted.append(item)
        return converted

    @staticmethod
    def _to_responses_input(messages: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
        converted: list[dict[str, Any]] = []
        for message in messages:
            role = message.get("role")
            if role == "tool":
                call_id = message.get("tool_call_id")
                if not call_id:
                    raise ModelProtocolError("A tool result was missing tool_call_id")
                converted.append(
                    {
                        "type": "function_call_output",
                        "call_id": call_id,
                        "output": str(message.get("content") or ""),
                    }
                )
                continue

            provider_items = message.get("_provider_items")
            if role == "assistant" and isinstance(provider_items, list) and provider_items:
                converted.extend(copy.deepcopy(provider_items))
                continue

            content = message.get("content")
            if role in {"system", "developer", "user", "assistant"} and content:
                converted.append({"role": role, "content": str(content)})

            if role == "assistant":
                for call in message.get("tool_calls", []) or []:
                    function = call.get("function", {})
                    call_id = call.get("id")
                    name = function.get("name")
                    if not call_id or not name:
                        raise ModelProtocolError("An assistant tool call was incomplete")
                    converted.append(
                        {
                            "type": "function_call",
                            "call_id": call_id,
                            "name": name,
                            "arguments": function.get("arguments") or "{}",
                        }
                    )
        return converted

    @staticmethod
    def _parse_completion(completion: Any) -> ModelResponse:
        try:
            choice = completion.choices[0]
            message = choice.message
        except (AttributeError, IndexError, TypeError) as exc:
            raise ModelProtocolError("Model response did not contain a usable choice") from exc

        calls: list[ToolCall] = []
        for raw_call in getattr(message, "tool_calls", None) or []:
            function = getattr(raw_call, "function", None)
            calls.append(
                _make_tool_call(
                    getattr(raw_call, "id", None),
                    getattr(function, "name", None),
                    getattr(function, "arguments", None) or "{}",
                )
            )

        usage_obj = getattr(completion, "usage", None)
        usage = _extract_usage(
            usage_obj,
            ("prompt_tokens", "completion_tokens", "total_tokens"),
        )
        return ModelResponse(
            content=_content_as_text(getattr(message, "content", None)),
            tool_calls=calls,
            finish_reason=getattr(choice, "finish_reason", None),
            usage=usage,
        )

    @staticmethod
    def _parse_response(response: Any) -> ModelResponse:
        output = getattr(response, "output", None)
        if not isinstance(output, (list, tuple)):
            raise ModelProtocolError("Responses result did not contain an output list")

        calls: list[ToolCall] = []
        for item in output:
            if getattr(item, "type", None) != "function_call":
                continue
            calls.append(
                _make_tool_call(
                    getattr(item, "call_id", None),
                    getattr(item, "name", None),
                    getattr(item, "arguments", None) or "{}",
                )
            )

        content = _content_as_text(getattr(response, "output_text", None))
        if not content:
            content = _response_output_text(output)
        usage = _extract_usage(
            getattr(response, "usage", None),
            ("input_tokens", "output_tokens", "total_tokens"),
        )
        return ModelResponse(
            content=content,
            tool_calls=calls,
            finish_reason=getattr(response, "status", None),
            usage=usage,
            provider_items=[_plain_value(item) for item in output],
        )


def _make_tool_call(call_id: Any, name: Any, raw_arguments: Any) -> ToolCall:
    if not call_id:
        raise ModelProtocolError("A tool call was missing its call id")
    if not name:
        raise ModelProtocolError("A tool call was missing its function name")
    raw = str(raw_arguments)
    try:
        arguments = json.loads(raw)
        if not isinstance(arguments, dict):
            raise ValueError("arguments must decode to a JSON object")
        parse_error = None
    except (json.JSONDecodeError, ValueError) as exc:
        arguments = None
        parse_error = str(exc)
    return ToolCall(
        id=str(call_id),
        name=str(name),
        arguments=arguments,
        raw_arguments=raw,
        parse_error=parse_error,
    )


def _extract_usage(usage_obj: Any, names: Sequence[str]) -> dict[str, int]:
    usage: dict[str, int] = {}
    for name in names:
        value = getattr(usage_obj, name, None)
        if isinstance(value, int):
            usage[name] = value
    return usage


def _content_as_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            text = item.get("text") if isinstance(item, dict) else getattr(item, "text", None)
            if text:
                parts.append(str(text))
        return "\n".join(parts)
    return str(content)


def _response_output_text(output: Sequence[Any]) -> str:
    parts: list[str] = []
    for item in output:
        if getattr(item, "type", None) != "message":
            continue
        for content in getattr(item, "content", None) or []:
            if getattr(content, "type", None) == "output_text":
                text = getattr(content, "text", None)
                if text:
                    parts.append(str(text))
    return "\n".join(parts)


def _plain_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(key): _plain_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain_value(item) for item in value]
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        return _plain_value(model_dump(exclude_none=True))
    values = getattr(value, "__dict__", None)
    if isinstance(values, dict):
        return {
            str(key): _plain_value(item)
            for key, item in values.items()
            if not str(key).startswith("_")
        }
    return str(value)
