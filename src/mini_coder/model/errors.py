from __future__ import annotations

from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any

from ..exceptions import ModelError, ModelErrorCategory, ModelProtocolError
from ..redaction import redact_sensitive_text


_RETRYABLE_SERVER_CODES = {500, 502, 503, 504, 524}


def classify_model_exception(
    exc: BaseException,
    *,
    secrets: tuple[str | None, ...] = (),
) -> ModelError:
    if isinstance(exc, ModelProtocolError):
        return exc
    if isinstance(exc, ModelError):
        return exc

    status_code = _status_code(exc)
    name = type(exc).__name__.casefold()
    diagnostic = redact_sensitive_text(str(exc), secrets=secrets)
    diagnostic_folded = diagnostic.casefold()
    if status_code == 401:
        category = ModelErrorCategory.AUTHENTICATION
        retryable = False
    elif status_code == 403:
        category = ModelErrorCategory.PERMISSION
        retryable = False
    elif status_code == 429:
        category = ModelErrorCategory.RATE_LIMIT
        retryable = True
    elif status_code in _RETRYABLE_SERVER_CODES:
        category = ModelErrorCategory.SERVER
        retryable = True
    elif status_code is not None and 500 <= status_code <= 599:
        category = ModelErrorCategory.SERVER
        retryable = False
    elif status_code is not None and 400 <= status_code <= 499:
        category = ModelErrorCategory.REQUEST
        retryable = False
    elif "timeout" in name or isinstance(exc, TimeoutError):
        category = ModelErrorCategory.TIMEOUT
        retryable = True
    elif any(marker in name for marker in ("connection", "network", "transport")) or any(
        marker in diagnostic_folded
        for marker in (
            "stream_read_error",
            "stream read error",
            "connection reset",
            "connection closed",
        )
    ):
        category = ModelErrorCategory.NETWORK
        retryable = True
    else:
        category = ModelErrorCategory.UNKNOWN
        retryable = False

    status = f" HTTP {status_code}" if status_code is not None else ""
    return ModelError(
        f"Model request failed [{category.value}{status}]: {diagnostic}",
        category=category,
        retryable=retryable,
        status_code=status_code,
        retry_after_seconds=_retry_after_seconds(exc),
    )


def _status_code(exc: BaseException) -> int | None:
    for candidate in (
        getattr(exc, "status_code", None),
        getattr(getattr(exc, "response", None), "status_code", None),
    ):
        if isinstance(candidate, int) and not isinstance(candidate, bool):
            return candidate
    return None


def _retry_after_seconds(exc: BaseException) -> float | None:
    response = getattr(exc, "response", None)
    headers: Any = getattr(response, "headers", None)
    if headers is None:
        headers = getattr(exc, "headers", None)
    if headers is None:
        return None
    try:
        value = headers.get("retry-after") or headers.get("Retry-After")
    except (AttributeError, TypeError):
        return None
    if value is None:
        return None
    try:
        seconds = float(str(value).strip())
        return max(0.0, seconds)
    except ValueError:
        pass
    try:
        target = parsedate_to_datetime(str(value))
        if target.tzinfo is None:
            target = target.replace(tzinfo=timezone.utc)
        return max(0.0, (target - datetime.now(timezone.utc)).total_seconds())
    except (TypeError, ValueError, OverflowError):
        return None
