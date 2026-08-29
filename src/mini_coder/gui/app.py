from __future__ import annotations

import asyncio
import difflib
import hashlib
import json
import os
import string
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from ..exceptions import SessionError
from ..session import SessionStore
from .controller import RunController, RunRequest


class StartRunBody(BaseModel):
    task: str = Field(min_length=1, max_length=20_000)
    workspace: str = Field(min_length=1, max_length=2_000)
    title: str = Field(default="", max_length=120)
    config_path: str | None = Field(default=None, max_length=2_000)
    auto: bool = False


class ApprovalBody(BaseModel):
    approved: bool


def _available_roots() -> list[str]:
    if os.name != "nt":
        return [str(Path("/").resolve())]
    return [
        f"{letter}:\\"
        for letter in string.ascii_uppercase
        if Path(f"{letter}:\\").is_dir()
    ]


def _session_summary(session) -> dict:
    return {
        "session_id": session.session_id,
        "title": session.title,
        "task": session.task,
        "workspace": session.workspace,
        "status": session.status.value,
        "verification_status": session.verification_status.value,
        "changed_files": len({item.path for item in session.changes if item.undo_status == "active"}),
        "created_at": session.created_at,
        "updated_at": session.updated_at,
    }


def _friendly_final_text(text: str) -> str:
    result = text.strip()
    for marker in ("\n\nOutcome:", "\n\nLocal change summary:"):
        if marker in result:
            result = result.split(marker, 1)[0].rstrip()
    return result


def _resolve_session(workspace: str, session_id: str):
    root = Path(workspace).expanduser().resolve()
    if not root.is_dir():
        raise HTTPException(status_code=400, detail="项目文件夹不存在")
    try:
        session = SessionStore.for_workspace(root).load(session_id)
    except SessionError as exc:
        raise HTTPException(status_code=404, detail="找不到这个会话") from exc
    if Path(session.workspace).resolve() != root:
        raise HTTPException(status_code=404, detail="找不到这个会话")
    return session


def create_app(controller: RunController | None = None) -> FastAPI:
    active_controller = controller or RunController()
    static_dir = Path(__file__).with_name("static")
    app = FastAPI(
        title="Mini Coder Agent GUI",
        version="0.1",
        docs_url=None,
        redoc_url=None,
    )
    app.state.run_controller = active_controller
    app.mount("/assets", StaticFiles(directory=static_dir), name="assets")

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(static_dir / "index.html")

    @app.get("/api/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/bootstrap")
    def bootstrap() -> dict[str, str]:
        workspace = Path.cwd().resolve()
        return {
            "default_workspace": str(workspace),
            "default_config_path": str(workspace / "agent.toml"),
        }

    @app.get("/api/directories")
    def list_directories(path: str | None = None) -> dict:
        if path and len(path) > 2_000:
            raise HTTPException(status_code=400, detail="文件夹路径过长")
        try:
            current = Path(path).expanduser().resolve(strict=True) if path else Path.cwd().resolve()
        except (OSError, RuntimeError) as exc:
            raise HTTPException(status_code=400, detail="文件夹不存在或无法访问") from exc
        if not current.is_dir():
            raise HTTPException(status_code=400, detail="选择的路径不是文件夹")
        try:
            directories = []
            for child in current.iterdir():
                try:
                    if child.is_dir():
                        directories.append({"name": child.name, "path": str(child.resolve())})
                except OSError:
                    continue
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail="没有权限查看这个文件夹") from exc
        except OSError as exc:
            raise HTTPException(status_code=400, detail="无法读取这个文件夹") from exc
        directories.sort(key=lambda item: item["name"].casefold())
        parent = None if current.parent == current else str(current.parent)
        return {
            "current": str(current),
            "parent": parent,
            "roots": _available_roots(),
            "directories": directories,
        }

    @app.get("/api/sessions")
    def list_sessions(workspace: str) -> dict:
        root = Path(workspace).expanduser().resolve()
        if not root.is_dir():
            raise HTTPException(status_code=400, detail="项目文件夹不存在")
        sessions = [
            item
            for item in SessionStore.for_workspace(root).list_sessions()
            if Path(item.workspace).resolve() == root
        ]
        return {"workspace": str(root), "sessions": [_session_summary(item) for item in sessions]}

    @app.get("/api/sessions/{session_id}")
    def get_session(session_id: str, workspace: str) -> dict:
        session = _resolve_session(workspace, session_id)
        conversation = [{"role": "user", "content": session.task}]
        if session.final_text:
            conversation.append(
                {"role": "assistant", "content": _friendly_final_text(session.final_text)}
            )
        return {
            **_session_summary(session),
            "conversation": conversation,
            "final_text": session.final_text,
            "usage": session.total_usage,
            "model_calls": session.model_call_count,
            "tool_calls": len(session.tool_executions),
            "run_duration_seconds": session.run_duration_seconds,
            "changes": [
                {
                    "change_id": change.change_id,
                    "path": change.path,
                    "additions": change.additions,
                    "deletions": change.deletions,
                    "diff": change.unified_diff,
                    "diff_truncated": change.diff_truncated,
                    "undo_status": change.undo_status,
                }
                for change in session.changes
            ],
            "verifications": [item.to_dict() for item in session.verification_records],
        }

    @app.get("/api/sessions/{session_id}/changes/{change_id}")
    def get_change(session_id: str, change_id: str, workspace: str) -> dict:
        session = _resolve_session(workspace, session_id)
        change = next((item for item in session.changes if item.change_id == change_id), None)
        if change is None:
            raise HTTPException(status_code=404, detail="找不到这项代码变更")
        workspace_root = Path(session.workspace).resolve()
        target = (workspace_root / change.path).resolve()
        try:
            target.relative_to(workspace_root)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="代码文件不在项目文件夹中") from exc
        try:
            raw = target.read_bytes()
        except OSError as exc:
            raise HTTPException(status_code=404, detail="修改后的文件已经不存在") from exc
        if len(raw) > 1_000_000:
            raise HTTPException(status_code=413, detail="文件超过 1 MB，无法在页面中完整显示")
        try:
            after = raw.decode("utf-8-sig" if raw.startswith(b"\xef\xbb\xbf") else "utf-8")
        except UnicodeDecodeError as exc:
            raise HTTPException(status_code=415, detail="这个文件不是 UTF-8 文本文件") from exc
        before = change.before_snapshot or ""
        full_diff = "".join(
            difflib.unified_diff(
                before.splitlines(keepends=True),
                after.splitlines(keepends=True),
                fromfile="/dev/null" if change.before_snapshot is None else f"a/{change.path}",
                tofile=f"b/{change.path}",
                lineterm="\n",
            )
        )
        return {
            "change_id": change.change_id,
            "path": change.path,
            "before": before,
            "after": after,
            "diff": full_diff,
            "additions": change.additions,
            "deletions": change.deletions,
            "matches_agent_version": hashlib.sha256(raw).hexdigest() == change.after_hash,
        }

    @app.post("/api/runs", status_code=202)
    def start_run(body: StartRunBody) -> dict:
        try:
            return active_controller.start(
                RunRequest(
                    task=body.task,
                    workspace=body.workspace,
                    title=body.title,
                    config_path=body.config_path or None,
                    auto=body.auto,
                )
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/runs/{run_id}")
    def get_run(run_id: str) -> dict:
        try:
            return active_controller.snapshot(run_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/api/runs/{run_id}/approvals/{approval_id}")
    def decide_approval(run_id: str, approval_id: str, body: ApprovalBody) -> dict:
        try:
            return active_controller.decide_approval(run_id, approval_id, body.approved)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get("/api/runs/{run_id}/events")
    async def stream_events(run_id: str, request: Request, after: int = 0):
        try:
            active_controller.snapshot(run_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

        header_sequence = request.headers.get("last-event-id")
        if header_sequence and header_sequence.isdigit():
            after = max(after, int(header_sequence))

        async def generate():
            sequence = max(0, after)
            while not await request.is_disconnected():
                events, terminal = await asyncio.to_thread(
                    active_controller.wait_for_events,
                    run_id,
                    sequence,
                    15.0,
                )
                if not events:
                    yield ": keep-alive\n\n"
                for event in events:
                    sequence = event["sequence"]
                    data = json.dumps(event, ensure_ascii=False, default=str)
                    yield f"id: {sequence}\nevent: run-event\ndata: {data}\n\n"
                if terminal and not events:
                    break

        return StreamingResponse(
            generate(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    return app
