from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Callable

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .controller import RunController, RunRequest


class StartRunBody(BaseModel):
    task: str = Field(min_length=1, max_length=20_000)
    workspace: str = Field(min_length=1, max_length=2_000)
    config_path: str | None = Field(default=None, max_length=2_000)
    auto: bool = False


class ApprovalBody(BaseModel):
    approved: bool


class SelectDirectoryBody(BaseModel):
    initial_directory: str | None = Field(default=None, max_length=2_000)


def _select_directory(initial_directory: str | None = None) -> str | None:
    import tkinter as tk
    from tkinter import filedialog

    initial = Path(initial_directory).expanduser() if initial_directory else Path.cwd()
    if not initial.is_dir():
        initial = Path.cwd()
    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    try:
        selected = filedialog.askdirectory(
            parent=root,
            initialdir=str(initial.resolve()),
            title="选择 Agent 要处理的项目文件夹",
            mustexist=True,
        )
    finally:
        root.destroy()
    return str(Path(selected).resolve()) if selected else None


def create_app(
    controller: RunController | None = None,
    directory_picker: Callable[[str | None], str | None] | None = None,
) -> FastAPI:
    active_controller = controller or RunController()
    active_directory_picker = directory_picker or _select_directory
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

    @app.post("/api/select-workspace")
    def select_workspace(body: SelectDirectoryBody) -> dict[str, str | bool | None]:
        try:
            selected = active_directory_picker(body.initial_directory)
        except Exception as exc:
            raise HTTPException(
                status_code=500,
                detail=f"无法打开文件夹选择窗口：{exc}",
            ) from exc
        if selected and not Path(selected).is_dir():
            raise HTTPException(status_code=400, detail="选择的路径不是有效文件夹")
        return {"selected": selected is not None, "workspace": selected}

    @app.post("/api/runs", status_code=202)
    def start_run(body: StartRunBody) -> dict:
        try:
            return active_controller.start(
                RunRequest(
                    task=body.task,
                    workspace=body.workspace,
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
