from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from ..exceptions import SessionError
from .models import AgentSession, validate_session_id


class SessionStore:
    """Persist versioned agent sessions using same-directory atomic replacement."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).expanduser().resolve()

    @classmethod
    def for_workspace(cls, workspace: str | Path) -> "SessionStore":
        return cls(Path(workspace).expanduser().resolve() / ".mini-coder" / "sessions")

    def path_for(self, session_id: str) -> Path:
        return self.root / f"{validate_session_id(session_id)}.json"

    def save(self, session: AgentSession) -> Path:
        path = self.path_for(session.session_id)
        try:
            payload = json.dumps(
                session.to_dict(),
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            ) + "\n"
        except (TypeError, ValueError) as exc:
            raise SessionError(f"session is not JSON serializable: {exc}") from exc

        try:
            self.root.mkdir(parents=True, exist_ok=True)
            descriptor, temporary_name = tempfile.mkstemp(
                dir=self.root,
                prefix=f".{session.session_id}.",
                suffix=".tmp",
                text=True,
            )
        except OSError as exc:
            raise SessionError(f"cannot prepare session storage in {self.root}: {exc}") from exc

        temporary_path = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary_path, path)
        except OSError as exc:
            try:
                os.close(descriptor)
            except OSError:
                pass
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise SessionError(f"cannot save session to {path}: {exc}") from exc
        return path

    def load(self, identifier: str | Path) -> AgentSession:
        path = self.resolve(identifier)
        try:
            with path.open("r", encoding="utf-8-sig") as stream:
                data = json.load(stream)
        except FileNotFoundError as exc:
            raise SessionError(f"session does not exist: {path}") from exc
        except json.JSONDecodeError as exc:
            raise SessionError(f"session file is not valid JSON: {path}: {exc}") from exc
        except OSError as exc:
            raise SessionError(f"cannot read session from {path}: {exc}") from exc

        session = AgentSession.from_dict(data)
        expected_name = f"{session.session_id}.json"
        if path.name != expected_name:
            raise SessionError(
                f"session id {session.session_id!r} does not match file name {path.name!r}"
            )
        return session

    def list_sessions(self) -> list[AgentSession]:
        if not self.root.is_dir():
            return []
        sessions: list[AgentSession] = []
        for path in self.root.glob("*.json"):
            try:
                sessions.append(self.load(path))
            except SessionError:
                continue
        sessions.sort(key=lambda item: item.updated_at, reverse=True)
        return sessions

    def resolve(self, identifier: str | Path) -> Path:
        candidate = Path(identifier).expanduser()
        if candidate.is_absolute() or candidate.parent != Path(".") or candidate.suffix == ".json":
            return candidate.resolve()
        return self.path_for(str(identifier))
