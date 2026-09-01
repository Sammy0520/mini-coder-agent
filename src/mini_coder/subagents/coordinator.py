from __future__ import annotations

import copy
import threading
import time
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from ..changes import ChangeTracker
from ..changes.models import ChangeRecord, PreparedChange
from ..exceptions import ChangeError
from .models import (
    PatchBundle,
    SubagentError,
    SubagentResult,
    SubagentRole,
    SubagentSpec,
    SubagentStatus,
    WorkerOutcome,
)
from .workspace import IsolatedWorkspace


EventCallback = Callable[[str, dict[str, Any]], None]
CancellationCallback = Callable[[], bool]
Worker = Callable[[SubagentSpec, Path, EventCallback], WorkerOutcome]


class ParallelSubagentCoordinator:
    """Run bounded child agents concurrently and stage writable results as patches."""

    def __init__(
        self,
        *,
        workspace: str | Path,
        worker: Worker,
        event_callback: EventCallback | None = None,
        cancellation_callback: CancellationCallback | None = None,
        max_parallel: int = 2,
        max_batches: int = 2,
        max_workspace_files: int = 5_000,
        max_workspace_bytes: int = 50_000_000,
    ) -> None:
        if max_parallel < 1 or max_parallel > 2:
            raise ValueError("max_parallel must be between 1 and 2")
        self.workspace = Path(workspace).expanduser().resolve()
        self.worker = worker
        self.event_callback = event_callback
        self.cancellation_callback = cancellation_callback
        self.max_parallel = max_parallel
        self.max_batches = max_batches
        self.max_workspace_files = max_workspace_files
        self.max_workspace_bytes = max_workspace_bytes
        self._lock = threading.RLock()
        self._batch_count = 0
        self._bundles: dict[str, PatchBundle] = {}

    def delegate(self, raw_tasks: list[dict[str, Any]]) -> dict[str, Any]:
        specs = [SubagentSpec.from_dict(item) for item in raw_tasks]
        if not specs:
            raise SubagentError("delegate_subagents requires at least one task")
        if len(specs) > self.max_parallel:
            raise SubagentError(
                f"At most {self.max_parallel} Subagents may run concurrently"
            )
        if len({item.agent_id for item in specs}) != len(specs):
            raise SubagentError("Subagent agent_id values must be unique within a batch")
        with self._lock:
            if self._batch_count >= self.max_batches:
                raise SubagentError(
                    f"This run already used the maximum {self.max_batches} delegation batches"
                )
            self._batch_count += 1
            batch_id = uuid.uuid4().hex

        self._emit(
            "subagents_planned",
            {
                "batch_id": batch_id,
                "parallel": len(specs) > 1,
                "count": len(specs),
                "agents": [item.to_dict() for item in specs],
            },
        )
        results: list[SubagentResult] = []
        started = time.monotonic()
        with ThreadPoolExecutor(
            max_workers=min(self.max_parallel, len(specs)),
            thread_name_prefix="mini-coder-subagent",
        ) as executor:
            futures = {
                executor.submit(self._run_one, batch_id, spec): spec for spec in specs
            }
            for future in as_completed(futures):
                spec = futures[future]
                try:
                    results.append(future.result())
                except Exception as exc:  # defensive boundary around child threads
                    result = SubagentResult(
                        spec=spec,
                        status=SubagentStatus.FAILED,
                        error=str(exc),
                    )
                    results.append(result)
                    self._emit_result("subagent_failed", result)

        by_id = {item.spec.agent_id: item for item in results}
        ordered = [by_id[item.agent_id] for item in specs]
        total_usage: dict[str, int] = {}
        for result in ordered:
            for name, value in result.usage.items():
                total_usage[name] = total_usage.get(name, 0) + value
        return {
            "batch_id": batch_id,
            "parallel": len(specs) > 1,
            "duration_seconds": time.monotonic() - started,
            "results": [item.to_dict() for item in ordered],
            "subagent_usage": total_usage,
            "subagent_model_calls": sum(item.model_calls for item in ordered),
            "subagent_tool_calls": sum(item.tool_calls for item in ordered),
            "pending_bundle_ids": [
                item.bundle_id for item in ordered if item.bundle_id is not None
            ],
        }

    def _run_one(self, batch_id: str, spec: SubagentSpec) -> SubagentResult:
        if self._cancelled():
            result = SubagentResult(spec=spec, status=SubagentStatus.CANCELLED)
            self._emit_result("subagent_cancelled", result)
            return result
        self._emit(
            "subagent_started",
            {
                "batch_id": batch_id,
                **spec.to_dict(),
                "status": SubagentStatus.RUNNING.value,
            },
        )
        isolated: IsolatedWorkspace | None = None
        child_workspace = self.workspace
        if spec.role == SubagentRole.IMPLEMENTER:
            destination = (
                self.workspace
                / ".mini-coder"
                / "runtime"
                / "subagents"
                / batch_id
                / spec.agent_id
                / "workspace"
            )
            isolated = IsolatedWorkspace.create(
                self.workspace,
                destination,
                max_files=self.max_workspace_files,
                max_bytes=self.max_workspace_bytes,
            )
            child_workspace = isolated.destination

        def child_event(name: str, payload: dict[str, Any]) -> None:
            public_payload = self._public_child_payload(name, payload)
            self._emit(
                "subagent_progress",
                {
                    "batch_id": batch_id,
                    "agent_id": spec.agent_id,
                    "role": spec.role.value,
                    "child_event": name,
                    "child_payload": public_payload,
                },
            )

        try:
            outcome = self.worker(spec, child_workspace, child_event)
            if self._cancelled() or outcome.status == "cancelled":
                result = SubagentResult(
                    spec=spec,
                    status=SubagentStatus.CANCELLED,
                    final_text=outcome.final_text,
                    usage=outcome.usage,
                    model_calls=outcome.model_calls,
                    tool_calls=outcome.tool_calls,
                    context_metrics=copy.deepcopy(outcome.context_metrics),
                )
                self._emit_result("subagent_cancelled", result)
                return result
            completed_status = outcome.status in {
                "completed",
                "completed_verified",
                "completed_unverified",
            }
            recovered_files = (
                isolated.collect_patch(spec.allowed_paths)
                if isolated is not None
                else []
            )
            if not completed_status and not recovered_files:
                result = SubagentResult(
                    spec=spec,
                    status=SubagentStatus.FAILED,
                    final_text=outcome.final_text,
                    usage=outcome.usage,
                    model_calls=outcome.model_calls,
                    tool_calls=outcome.tool_calls,
                    error=outcome.final_text or f"Subagent ended with {outcome.status}",
                    context_metrics=copy.deepcopy(outcome.context_metrics),
                )
                self._emit_result("subagent_failed", result)
                return result

            bundle_id = None
            status = SubagentStatus.COMPLETED
            if isolated is not None:
                files = recovered_files
                if files:
                    bundle = PatchBundle(
                        bundle_id=uuid.uuid4().hex,
                        agent_id=spec.agent_id,
                        files=files,
                        verification=copy.deepcopy(outcome.verification),
                    )
                    with self._lock:
                        self._bundles[bundle.bundle_id] = bundle
                    bundle_id = bundle.bundle_id
                    status = SubagentStatus.PATCH_PENDING
            recovered_error = None
            if not completed_status and bundle_id is not None:
                recovered_error = (
                    f"Subagent stopped with status {outcome.status}; its bounded patch "
                    "was preserved for parent review and integrated verification."
                )
            result = SubagentResult(
                spec=spec,
                status=status,
                final_text=outcome.final_text,
                usage=outcome.usage,
                model_calls=outcome.model_calls,
                tool_calls=outcome.tool_calls,
                bundle_id=bundle_id,
                error=recovered_error,
                context_metrics=copy.deepcopy(outcome.context_metrics),
            )
            self._emit_result("subagent_completed", result)
            if bundle_id is not None:
                bundle = self._bundles[bundle_id]
                self._emit(
                    "subagent_patch_ready",
                    {
                        "batch_id": batch_id,
                        **spec.to_dict(),
                        "status": SubagentStatus.PATCH_PENDING.value,
                        "summary": outcome.final_text[:2_000],
                        "patch": bundle.to_dict(include_content=False),
                    },
                )
            return result
        except SubagentError as exc:
            status = (
                SubagentStatus.SCOPE_VIOLATION
                if "outside its authorization" in str(exc)
                else SubagentStatus.FAILED
            )
            result = SubagentResult(spec=spec, status=status, error=str(exc))
            self._emit_result("subagent_failed", result)
            return result
        finally:
            if isolated is not None:
                isolated.cleanup()

    def bundle_preview(self, bundle_ids: list[str]) -> dict[str, Any]:
        bundles = self._resolve_bundles(bundle_ids)
        files = [item for bundle in bundles for item in bundle.files]
        duplicates = sorted(
            path for path in {item.path for item in files} if sum(
                candidate.path == path for candidate in files
            ) > 1
        )
        if duplicates:
            raise SubagentError(
                "Subagent patches overlap and require integration: " + ", ".join(duplicates)
            )
        return {
            "bundle_ids": bundle_ids,
            "file_count": len(files),
            "additions": sum(item.additions for item in files),
            "deletions": sum(item.deletions for item in files),
            "files": [item.to_dict(include_content=False) for item in files],
        }

    def apply_bundles(self, bundle_ids: list[str]) -> dict[str, Any]:
        preview = self.bundle_preview(bundle_ids)
        bundles = self._resolve_bundles(bundle_ids)
        tracker = ChangeTracker(self.workspace)
        prepared: list[PreparedChange] = []
        for bundle in bundles:
            for patch in bundle.files:
                change = tracker.prepare(
                    "write_file",
                    {
                        "path": patch.path,
                        "content": patch.after_text,
                        "overwrite": patch.before_hash is not None,
                    },
                    uuid.uuid4().hex,
                )
                if change.before_hash != patch.before_hash:
                    bundle.status = SubagentStatus.CONFLICTED
                    self._emit(
                        "subagent_patch_conflict",
                        {
                            "agent_id": bundle.agent_id,
                            "bundle_id": bundle.bundle_id,
                            "path": patch.path,
                            "expected_hash": patch.before_hash,
                            "current_hash": change.before_hash,
                        },
                    )
                    raise SubagentError(
                        f"Workspace changed while Subagent was running: {patch.path}"
                    )
                prepared.append(change)
        try:
            changes = tracker.apply_many(prepared)
        except ChangeError as exc:
            raise SubagentError(f"Could not apply the Subagent patch batch: {exc}") from exc
        for bundle in bundles:
            bundle.status = SubagentStatus.PATCH_APPLIED
            self._emit(
                "subagent_patch_applied",
                {
                    "agent_id": bundle.agent_id,
                    "bundle_id": bundle.bundle_id,
                    "status": bundle.status.value,
                },
            )
        return {
            **preview,
            "tracked_changes": [item.to_dict() for item in changes],
            "applied": True,
        }

    def _resolve_bundles(self, bundle_ids: list[str]) -> list[PatchBundle]:
        if not bundle_ids:
            raise SubagentError("At least one bundle_id is required")
        if len(bundle_ids) != len(set(bundle_ids)):
            raise SubagentError("bundle_id values must be unique")
        with self._lock:
            missing = [item for item in bundle_ids if item not in self._bundles]
            if missing:
                raise SubagentError("Unknown Subagent patch bundle: " + ", ".join(missing))
            bundles = [self._bundles[item] for item in bundle_ids]
        for bundle in bundles:
            if bundle.status != SubagentStatus.PATCH_PENDING:
                raise SubagentError(
                    f"Patch bundle {bundle.bundle_id} is {bundle.status.value}, not pending"
                )
        return bundles

    def _cancelled(self) -> bool:
        return bool(self.cancellation_callback and self.cancellation_callback())

    @staticmethod
    def _public_child_payload(name: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Keep child progress useful without forwarding prompts, code, or raw output."""
        allowed_by_event = {
            "phase_changed": ("phase", "reason"),
            "model_request_started": ("attempt", "sent_messages", "estimated_tokens"),
            "tool_call_requested": ("tool",),
            "tool_call_completed": ("tool", "ok", "duration_seconds"),
            "verification_completed": ("passed", "exit_code", "duration_seconds"),
            "context_compacted": (
                "history_messages",
                "sent_messages",
                "estimated_tokens",
                "checkpoint_generation",
                "checkpoint_hash",
                "compaction_reason",
                "checkpoint_created",
                "estimated_total_tokens",
            ),
            "context_checkpoint_committed": (
                "history_messages",
                "sent_messages",
                "checkpoint_generation",
                "checkpoint_hash",
                "compaction_reason",
                "checkpoint_created",
                "estimated_message_tokens",
                "estimated_total_tokens",
            ),
            "completion_reserve_started": ("reason",),
        }
        fields = allowed_by_event.get(name, ())
        result: dict[str, Any] = {}
        for field_name in fields:
            if field_name not in payload:
                continue
            value = payload.get(field_name)
            if isinstance(value, (str, int, float, bool)) or value is None:
                result[field_name] = value
        if name == "model_request_started":
            stability = payload.get("cache_stability")
            if isinstance(stability, dict):
                result["cache_stability"] = {
                    field_name: stability.get(field_name)
                    for field_name in (
                        "checkpoint_generation",
                        "checkpoint_hash",
                        "compaction_reason",
                        "estimated_longest_common_prefix_tokens",
                        "common_prefix_message_items",
                    )
                    if isinstance(
                        stability.get(field_name),
                        (str, int, float, bool, type(None)),
                    )
                }
        return result

    def _emit_result(self, name: str, result: SubagentResult) -> None:
        self._emit(name, result.to_dict())

    def _emit(self, name: str, payload: dict[str, Any]) -> None:
        if self.event_callback is not None:
            self.event_callback(name, payload)
