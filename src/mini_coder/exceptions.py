class MiniCoderError(Exception):
    """Base exception for expected agent failures."""


class ConfigurationError(MiniCoderError):
    """Raised when required runtime configuration is absent or invalid."""


class ModelError(MiniCoderError):
    """Raised when the model request fails."""


class ModelProtocolError(ModelError):
    """Raised when the model response cannot be interpreted safely."""


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
