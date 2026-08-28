from __future__ import annotations

from typing import Any


def tool_error_data(message: str, *, tool: str | None = None) -> dict[str, Any]:
    lowered = message.casefold()
    if "unknown tool" in lowered:
        code = "unknown_tool"
        suggestion = "Choose one of the advertised local tools; do not invent tool names."
    elif "unexpected argument" in lowered or "missing required argument" in lowered:
        code = "invalid_arguments"
        suggestion = "Retry once with only arguments declared in the tool schema."
    elif "must be of type" in lowered or "must be one of" in lowered:
        code = "invalid_argument_value"
        suggestion = "Correct the argument type or enum value using the tool schema."
    elif "path does not exist" in lowered:
        code = "path_not_found"
        suggestion = "Use the workspace overview or list_files to locate the path first."
    elif "escapes the workspace" in lowered or "access to sensitive" in lowered:
        code = "path_blocked"
        suggestion = "Stay within visible workspace paths; do not retry the blocked path."
    elif "expected" in lowered and "occurrence" in lowered:
        code = "edit_match_mismatch"
        suggestion = "Read the current line range, then retry with an exact unique text block."
    elif "binary files" in lowered or "decode" in lowered:
        code = "unsupported_text_file"
        suggestion = "Choose a UTF-8 source file; binary content is intentionally not exposed."
    else:
        code = "tool_error"
        suggestion = "Use the error as an observation; change the approach instead of repeating it."
    return {"error_code": code, "suggestion": suggestion, "tool": tool}
