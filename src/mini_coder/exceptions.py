from __future__ import annotations

from enum import Enum


class MiniCoderError(Exception):
    """Base exception for expected agent failures."""


class ConfigurationError(MiniCoderError):
    """Raised when required runtime configuration is absent or invalid."""


class ModelErrorCategory(str, Enum):
    AUTHENTICATION = "authentication"
    PERMISSION = "permission"
    RATE_LIMIT = "rate_limit"
    TIMEOUT = "timeout"
    NETWORK = "network"
    SERVER = "server"
    REQUEST = "request"
    RESPONSE_PARSE = "response_parse"
    UNKNOWN = "unknown"


class ModelError(MiniCoderError):
    """Classified model failure consumed by the local retry policy."""

    def __init__(
        self,
        message: str,
        *,
        category: ModelErrorCategory = ModelErrorCategory.UNKNOWN,
        retryable: bool = False,
        status_code: int | None = None,
        retry_after_seconds: float | None = None,
    ) -> None:
        super().__init__(message)
        self.category = category
        self.retryable = retryable
        self.status_code = status_code
        self.retry_after_seconds = retry_after_seconds


class ModelProtocolError(ModelError):
    """Raised when the model response cannot be interpreted safely."""

    def __init__(self, message: str) -> None:
        super().__init__(
            message,
            category=ModelErrorCategory.RESPONSE_PARSE,
            retryable=True,
        )


class ToolError(MiniCoderError):
    """Raised for invalid or failed local tool operations."""


class PathSafetyError(ToolError):
    """Raised when a requested path violates the workspace policy."""


class SessionError(MiniCoderError):
    """Raised when a persisted agent session cannot be stored or restored safely."""


class ChangeError(MiniCoderError):
    """Raised when a tracked file change cannot be prepared or applied safely."""


class ChangeConflictError(ChangeError):
    """Raised when a file changed after a tracked operation was prepared."""
