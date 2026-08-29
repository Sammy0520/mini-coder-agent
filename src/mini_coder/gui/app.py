from __future__ import annotations

import asyncio
import difflib
import hashlib
import json
import os
import string
import threading
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
    session_id: str | None = Field(default=None, max_length=128)
    config_path: str | None = Field(default=None, max_length=2_000)
    auto: bool = False


class ApprovalBody(BaseModel):
    approved: bool


class WorkspaceCatalog:
    """Remember GUI workspaces so sessions can be listed independently of projects."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser().resolve()
        self._lock = threading.Lock()

    def register(self, workspace: str | Path) -> Path:
        root = Path(workspace).expanduser().resolve()
        if not root.is_dir():
            raise ValueError("项目文件夹不存在")
        with self._lock:
            values = self._read_locked()
            rendered = str(root)
            if rendered not in values:
                values.append(rendered)
                self._write_locked(values)
        return root

    def workspaces(self) -> list[Path]:
        with self._lock:
            values = self._read_locked()
        roots: list[Path] = []
        for value in values:
            try:
                root = Path(value).expanduser().resolve()
            except (OSError, RuntimeError):
                continue
            if root.is_dir():
                roots.append(root)
        return roots

    def _read_locked(self) -> list[str]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8-sig"))
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            return []
        values = data.get("workspaces") if isinstance(data, dict) else None
        if not isinstance(values, list):
            return []
        return [item for item in values if isinstance(item, str) and item]

    def _write_locked(self, values: list[str]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(
            json.dumps({"workspaces": values}, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, self.path)


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
        "turn_count": session.turn_count,
    }


def _friendly_final_text(text: str) -> str:
    result = text.strip()
    for marker in ("\n\nOutcome:", "\n\nLocal change summary:"):
        if marker in result:
            result = result.split(marker, 1)[0].rstrip()
    return result


def _natural_session_reply(session) -> str:
    active_changes = [item for item in session.changes if item.undo_status == "active"]
    paths = list(dict.fromkeys(item.path for item in active_changes))
    if session.status.value.startswith("completed"):
        paragraphs = ["已经完成了。"]
        if paths:
            created = all(item.before_hash is None for item in active_changes)
            verb = "创建了" if created else "创建或修改了"
            paragraphs.append(f"我{verb} {_join_chinese(paths)}。")
        if session.verification_status.value == "passed":
            paragraphs[-1] = paragraphs[-1][:-1] + "，也运行了项目测试，结果全部通过。"
        elif session.verification_status.value in {"failed", "stale"}:
            paragraphs.append("项目检查还没有完全通过，具体情况可以在右侧查看。")
        elif paths:
            paragraphs.append("这次修改还没有经过完整的项目检查。")
        if paths:
            paragraphs.append("想看具体内容的话，可以点击右侧的文件名查看完整改动。")
        return "\n\n".join(paragraphs)
    friendly = _friendly_final_text(session.final_text)
    if friendly:
        first = friendly.split("\n", 1)[0].strip().strip("`#*- ")
        if first:
            return f"这次任务还没有完全完成。\n\n{first}"
    return "这次任务还没有完全完成，可以查看右侧过程了解停在哪里。"


def _join_chinese(values: list[str]) -> str:
    if not values:
        return ""
    if len(values) == 1:
        return values[0]
    return "、".join(values[:-1]) + " 和 " + values[-1]


def _friendly_tool_action(name: str) -> str:
    return {
        "read_file": "阅读文件",
        "list_files": "查看文件列表",
        "search_text": "搜索项目内容",
        "write_file": "写入文件",
        "edit_file": "修改文件",
        "run_command": "运行本地命令",
    }.get(name, "完成一项项目操作")


def _session_execution_history(session) -> list[dict]:
    history: list[dict] = []
    if session.workspace_baseline:
        history.append(
            {
                "title": "先了解了一下项目结构",
                "details": ["看过项目入口、测试和主要文件后再开始处理任务。"],
                "time": session.created_at,
                "icon": "⌁",
            }
        )
    changes = {item.tool_execution_id: item for item in session.changes}
    verifications = {
        item.tool_execution_id: item for item in session.verification_records
    }
    read_group: dict | None = None
    for execution in session.tool_executions:
        arguments = execution.arguments or {}
        if execution.name in {"read_file", "list_files", "search_text"}:
            target = arguments.get("path") or arguments.get("query") or "项目内容"
            detail = f"{_friendly_tool_action(execution.name)}：{target}"
            if read_group is None:
                read_group = {
                    "title": "查看和阅读了项目文件",
                    "details": [detail],
                    "time": execution.created_at,
                    "icon": "⌕",
                    "count": 1,
                }
                history.append(read_group)
            else:
                read_group["details"].append(detail)
                read_group["count"] += 1
                read_group["title"] = (
                    f"查看和阅读了项目文件（{read_group['count']} 项）"
                )
            continue
        read_group = None
        if execution.name in {"write_file", "edit_file"}:
            change = changes.get(execution.execution_id)
            path = change.path if change is not None else arguments.get("path", "项目文件")
            if execution.approval_granted is False:
                title = f"没有改动 {path}"
                details = ["这次修改没有得到允许，所以文件保持原样。"]
                icon = "×"
            elif change is not None:
                verb = "创建了" if change.before_hash is None else "修改了"
                title = f"{verb} {path}"
                details = [
                    f"这个文件新增了 {change.additions} 行，删除了 {change.deletions} 行。"
                ]
                icon = "✓"
            else:
                title = f"尝试处理 {path}"
                details = ["这次文件操作没有成功完成。"]
                icon = "!"
        elif execution.name == "run_command":
            verification = verifications.get(execution.execution_id)
            if verification is not None:
                title = (
                    "运行测试，确认功能可以正常使用"
                    if verification.passed
                    else "运行测试后发现还有问题"
                )
                details = [
                    "测试已经通过。" if verification.passed else "测试没有通过，需要继续处理。",
                    f"运行内容：{verification.command}",
                ]
                icon = "✓" if verification.passed else "!"
            else:
                title = "运行了一项本地检查"
                details = [f"运行内容：{arguments.get('command', '本地命令')}"]
                icon = "✓" if execution.ok else "!"
        else:
            title = _friendly_tool_action(execution.name)
            details = []
            if arguments.get("path"):
                details.append(f"处理位置：{arguments['path']}")
            icon = "✓" if execution.ok is not False else "!"
        history.append(
            {
                "title": title,
                "details": details,
                "time": execution.updated_at,
                "icon": icon,
            }
        )
    return history


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


def _all_sessions(catalog: WorkspaceCatalog) -> list:
    sessions = []
    for root in catalog.workspaces():
        for session in SessionStore.for_workspace(root).list_sessions():
            if Path(session.workspace).resolve() == root:
                sessions.append(session)
    sessions.sort(key=lambda item: item.updated_at, reverse=True)
    return sessions


def _resolve_catalog_session(catalog: WorkspaceCatalog, session_id: str):
    for root in catalog.workspaces():
        try:
            return _resolve_session(str(root), session_id)
        except HTTPException as exc:
            if exc.status_code != 404:
                raise
    raise HTTPException(status_code=404, detail="找不到这个会话")


def create_app(
    controller: RunController | None = None,
    *,
    catalog_path: str | Path | None = None,
) -> FastAPI:
    active_controller = controller or RunController()
    catalog = WorkspaceCatalog(
        catalog_path or (Path.cwd() / ".mini-coder" / "gui-workspaces.json")
    )
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
    def list_sessions(workspace: str | None = None) -> dict:
        if workspace:
            try:
                catalog.register(workspace)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
        sessions = _all_sessions(catalog)
        return {"sessions": [_session_summary(item) for item in sessions]}

    @app.get("/api/sessions/{session_id}")
    def get_session(session_id: str) -> dict:
        session = _resolve_catalog_session(catalog, session_id)
        conversation = []
        for item in session.conversation:
            content = str(item.get("content") or "")
            if item.get("role") == "assistant":
                content = _friendly_final_text(content)
            if content:
                conversation.append({"role": item.get("role"), "content": content})
        if session.final_text and (
            not conversation or conversation[-1].get("role") != "assistant"
        ):
            conversation.append(
                {"role": "assistant", "content": _natural_session_reply(session)}
            )
        return {
            **_session_summary(session),
            "conversation": conversation,
            "final_text": session.final_text,
            "usage": session.total_usage,
            "model_calls": session.model_call_count,
            "tool_calls": len(session.tool_executions),
            "run_duration_seconds": session.run_duration_seconds,
            "model_call_records": session.model_call_records,
            "working_memory": session.working_memory,
            "execution": _session_execution_history(session),
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
    def get_change(session_id: str, change_id: str) -> dict:
        session = _resolve_catalog_session(catalog, session_id)
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
            catalog.register(body.workspace)
            return active_controller.start(
                RunRequest(
                    task=body.task,
                    workspace=body.workspace,
                    title=body.title,
                    session_id=body.session_id or None,
                    config_path=body.config_path or None,
                    auto=body.auto,
                )
            )
        except (ValueError, SessionError) as exc:
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
