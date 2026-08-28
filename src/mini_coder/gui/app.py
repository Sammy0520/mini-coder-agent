from __future__ import annotations

import asyncio
import json
from pathlib import Path

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
