from __future__ import annotations

from pathlib import Path

from ..exceptions import PathSafetyError


class WorkspacePolicy:
    """Confines file operations to one workspace and hides common secret locations."""

    _DENIED_PARTS = {".git", ".ssh", ".gnupg", "node_modules", ".venv", "venv"}
    _DENIED_NAMES = {
        ".env",
        ".env.local",
        ".env.production",
        ".env.development",
        "credentials",
        "credentials.json",
        "id_rsa",
        "id_ed25519",
    }
    _DENIED_SUFFIXES = {".pem", ".key", ".p12", ".pfx"}

    def __init__(self, root: Path) -> None:
        self.root = root.expanduser().resolve()
        if not self.root.is_dir():
            raise PathSafetyError(f"Workspace is not a directory: {self.root}")

    def resolve(
        self,
        requested: str | Path,
        *,
        must_exist: bool | None = None,
    ) -> Path:
        raw = Path(requested).expanduser()
        candidate = raw.resolve() if raw.is_absolute() else (self.root / raw).resolve()
        if not candidate.is_relative_to(self.root):
            raise PathSafetyError(f"Path escapes the workspace: {requested}")
        if self.is_denied(candidate):
            raise PathSafetyError(f"Access to sensitive or internal path is blocked: {requested}")
        if must_exist is True and not candidate.exists():
            raise PathSafetyError(f"Path does not exist: {requested}")
        if must_exist is False and candidate.exists():
            raise PathSafetyError(f"Path already exists: {requested}")
        return candidate

    def is_denied(self, path: Path) -> bool:
        try:
            relative = path.resolve().relative_to(self.root)
        except ValueError:
            return True
        lowered_parts = [part.casefold() for part in relative.parts]
        if any(part in self._DENIED_PARTS for part in lowered_parts):
            return True
        if not lowered_parts:
            return False
        name = lowered_parts[-1]
        if name in self._DENIED_NAMES:
            return True
        return Path(name).suffix in self._DENIED_SUFFIXES

    def display(self, path: Path) -> str:
        relative = path.resolve().relative_to(self.root)
        return relative.as_posix() or "."

