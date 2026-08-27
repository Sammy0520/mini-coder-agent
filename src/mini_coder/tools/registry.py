from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from ..exceptions import ToolError
from .base import Tool, ToolContext, ToolResult


class ToolRegistry:
    def __init__(self, tools: Iterable[Tool] | None = None) -> None:
        self._tools: dict[str, Tool] = {}
        for tool in tools or []:
            self.register(tool)

    def register(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"Duplicate tool name: {tool.name}")
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def definitions(self) -> list[dict[str, Any]]:
        return [tool.definition() for tool in self._tools.values()]

    def execute(self, name: str, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        tool = self.get(name)
        if tool is None:
            return ToolResult(False, f"Unknown tool: {name}")
        try:
            self.validate_arguments(name, arguments)
            return tool.execute(arguments, context)
        except (ToolError, OSError, UnicodeError, ValueError) as exc:
            return ToolResult(False, str(exc))

    def validate_arguments(self, name: str, arguments: dict[str, Any]) -> None:
        tool = self.get(name)
        if tool is None:
            raise ToolError(f"Unknown tool: {name}")
        _validate_arguments(arguments, tool.parameters)


def _validate_arguments(arguments: dict[str, Any], schema: dict[str, Any]) -> None:
    if not isinstance(arguments, dict):
        raise ToolError("Tool arguments must be a JSON object")
    properties = schema.get("properties", {})
    required = schema.get("required", [])
    missing = [name for name in required if name not in arguments]
    if missing:
        raise ToolError(f"Missing required argument(s): {', '.join(missing)}")
    if schema.get("additionalProperties") is False:
        extras = sorted(set(arguments) - set(properties))
        if extras:
            raise ToolError(f"Unexpected argument(s): {', '.join(extras)}")

    type_map = {
        "string": str,
        "integer": int,
        "number": (int, float),
        "boolean": bool,
        "object": dict,
        "array": list,
    }
    for name, value in arguments.items():
        property_schema = properties.get(name, {})
        expected_name = property_schema.get("type")
        expected = type_map.get(expected_name)
        if expected is None:
            continue
        if expected_name in {"integer", "number"} and isinstance(value, bool):
            valid = False
        else:
            valid = isinstance(value, expected)
        if not valid:
            raise ToolError(f"Argument '{name}' must be of type {expected_name}")
        allowed = property_schema.get("enum")
        if allowed is not None and value not in allowed:
            rendered = ", ".join(repr(item) for item in allowed)
            raise ToolError(f"Argument '{name}' must be one of: {rendered}")
