from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any


_REDACTED = "[REDACTED]"
_SENSITIVE_FIELD = re.compile(
    r"^(?:authorization|proxy-authorization|api[_-]?key|access[_-]?token|"
    r"refresh[_-]?token|client[_-]?secret|password)$",
    re.IGNORECASE,
)
_TEXT_PATTERNS = (
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{6,}", re.IGNORECASE),
    re.compile(r"\b(?:sk|rk|pk|sess)-[A-Za-z0-9_-]{8,}\b", re.IGNORECASE),
    re.compile(
        r"(?i)(\b(?:OPENAI_API_KEY|CODING_AGENT_API_KEY|api[_-]?key|"
        r"access[_-]?token|refresh[_-]?token|client[_-]?secret|authorization|"
        r"password)\b\s*[=:]\s*)([^\s,;\]\}\"']+|\"[^\"]*\"|'[^']*')"
    ),
    re.compile(
        r"(?i)([?&](?:api[_-]?key|access[_-]?token|token|key)=)[^&#\s]+"
    ),
)


def redact_sensitive_text(
    value: Any,
    *,
    secrets: Sequence[str | None] = (),
) -> str:
    """Remove common credentials without retaining an unredacted diagnostic copy."""
    text = str(value)
    for secret in secrets:
        if isinstance(secret, str) and len(secret) >= 4:
            text = text.replace(secret, _REDACTED)
    text = _TEXT_PATTERNS[0].sub(f"Bearer {_REDACTED}", text)
    text = _TEXT_PATTERNS[1].sub(_REDACTED, text)
    text = _TEXT_PATTERNS[2].sub(_redact_assignment, text)
    text = _TEXT_PATTERNS[3].sub(lambda match: match.group(1) + _REDACTED, text)
    return text


def _redact_assignment(match: re.Match[str]) -> str:
    value = match.group(2)
    if len(value) >= 2 and value[0] in {"\"", "'"} and value[-1] == value[0]:
        return match.group(1) + value[0] + _REDACTED + value[-1]
    return match.group(1) + _REDACTED


def redact_sensitive_value(
    value: Any,
    *,
    secrets: Sequence[str | None] = (),
) -> Any:
    """Recursively redact event/tool payloads while preserving JSON value types."""
    if isinstance(value, str):
        return redact_sensitive_text(value, secrets=secrets)
    if isinstance(value, Mapping):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            rendered_key = str(key)
            if _SENSITIVE_FIELD.fullmatch(rendered_key):
                redacted[rendered_key] = _REDACTED
            else:
                redacted[rendered_key] = redact_sensitive_value(item, secrets=secrets)
        return redacted
    if isinstance(value, (list, tuple)):
        return [redact_sensitive_value(item, secrets=secrets) for item in value]
    return value
