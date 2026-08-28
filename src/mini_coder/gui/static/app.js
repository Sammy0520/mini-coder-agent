const state = {
  runId: null,
  source: null,
  eventCount: 0,
  changes: new Set(),
  pendingApproval: null,
};

const els = Object.fromEntries([
  "workspace", "configPath", "task", "runButton", "connectionDot", "connectionLabel",
  "runStatus", "sessionId", "verificationStatus", "changeCount", "eventCount",
  "timeline", "approvalPanel", "approvalRisk", "approvalTitle", "approvalArguments",
  "approveButton", "denyButton", "diffStats", "diffMeta", "diffView",
  "verificationBadge", "verificationMessage", "summaryPanel", "summaryText", "toast",
].map((id) => [id, document.getElementById(id)]));

const terminalEvents = new Set(["controller_run_finished", "controller_run_failed"]);

els.runButton.addEventListener("click", startRun);
els.approveButton.addEventListener("click", () => decideApproval(true));
els.denyButton.addEventListener("click", () => decideApproval(false));

async function startRun() {
  const task = els.task.value.trim();
  const workspace = els.workspace.value.trim();
  const configPath = els.configPath.value.trim();
  if (!task || !workspace) {
    toast("Workspace and task are required.", true);
    return;
  }
  resetRunView();
  setBusy(true, "Starting agent…");
  try {
    const response = await fetch("/api/runs", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({task, workspace, config_path: configPath || null, auto: false}),
    });
    const data = await readJson(response);
    if (!response.ok) throw new Error(data.detail || "Could not start the run.");
    state.runId = data.run_id;
    els.runStatus.textContent = labelStatus(data.status);
    connectEvents(data.run_id);
  } catch (error) {
    setBusy(false, "Error");
    els.runStatus.textContent = "Failed to start";
    toast(error.message, true);
  }
}

function connectEvents(runId) {
  if (state.source) state.source.close();
  const source = new EventSource(`/api/runs/${runId}/events`);
  state.source = source;
  source.addEventListener("open", () => setBusy(true, "Live"));
  source.addEventListener("run-event", (message) => {
    const envelope = JSON.parse(message.data);
    renderEvent(envelope);
    if (terminalEvents.has(envelope.event)) {
      source.close();
      state.source = null;
      refreshSnapshot(runId);
    }
  });
  source.onerror = () => {
    if (!state.source) return;
    els.connectionLabel.textContent = "Reconnecting…";
    els.connectionDot.className = "connection-dot busy";
  };
}

function renderEvent(envelope) {
  state.eventCount += 1;
  els.eventCount.textContent = `${state.eventCount} event${state.eventCount === 1 ? "" : "s"}`;
  if (state.eventCount === 1) els.timeline.innerHTML = "";
  const payload = envelope.payload || {};
  const presentation = describeEvent(envelope.event, payload);
  const item = document.createElement("div");
  item.className = "timeline-event";
  item.innerHTML = `
    <div class="event-icon ${presentation.tone}">${presentation.icon}</div>
    <div class="event-body">
      <div class="event-head">
        <p class="event-title">${escapeHtml(presentation.title)}</p>
        <span class="event-time">${formatTime(envelope.timestamp)}</span>
      </div>
      <p class="event-detail">${escapeHtml(presentation.detail)}</p>
    </div>`;
  els.timeline.appendChild(item);
  els.timeline.scrollTop = els.timeline.scrollHeight;
  updatePanels(envelope.event, payload);
}

function describeEvent(name, payload) {
  const tool = payload.tool || "tool";
  const map = {
    controller_run_created: ["01", "Run created", `Workspace: ${payload.workspace || ""}`, ""],
    session_created: ["S", "Session saved", shortId(payload.session_id), "success"],
    workspace_overview_generated: ["⌁", "Project understood", overviewDetail(payload), ""],
    run_started: ["▶", "Agent started", payload.resumed ? "Resuming an existing session" : "Starting a new session", ""],
    model_request_started: ["AI", "Asking model", `Step ${payload.step || ""}`, ""],
    model_response_received: ["AI", "Model response", modelDetail(payload), ""],
    tool_call_requested: ["↳", `Tool: ${tool}`, toolDetail(payload), ""],
    approval_required: ["!", "Approval required", `${tool} · ${payload.risk || "write"}`, "warning"],
    approval_resolved: [payload.approved ? "✓" : "×", payload.approved ? "Approved" : "Rejected", tool, payload.approved ? "success" : "error"],
    change_preview: ["Δ", `Change preview: ${payload.path || "file"}`, `+${payload.additions || 0} / -${payload.deletions || 0}`, "warning"],
    change_applied: ["✓", `Changed: ${payload.path || "file"}`, `+${payload.additions || 0} / -${payload.deletions || 0}`, "success"],
    phase_changed: ["→", `Phase: ${payload.phase || "working"}`, `${payload.previous || "start"} → ${payload.phase || "working"}`, ""],
    verification_started: ["V", "Verification started", payload.command || "Running local verification", ""],
    verification_completed: [payload.passed ? "✓" : "×", payload.passed ? "Verification passed" : "Verification failed", verificationDetail(payload), payload.passed ? "success" : "error"],
    run_completed: ["✓", "Agent completed", `Status: ${payload.verification_status || payload.result_status || "completed"}`, "success"],
    run_failed: ["×", "Agent run failed", payload.stop_reason || payload.result_status || "Run failed", "error"],
    controller_run_finished: ["✓", "Run controller finished", `${payload.steps || 0} model step(s)`, payload.result_status === "completed" ? "success" : "error"],
    controller_run_failed: ["×", "Run controller failed", payload.error || "Unexpected error", "error"],
  };
  const value = map[name] || ["·", humanize(name), compactPayload(payload), ""];
  return {icon: value[0], title: value[1], detail: value[2] || "", tone: value[3] || ""};
}

function updatePanels(name, payload) {
  if (payload.session_id) els.sessionId.textContent = shortId(payload.session_id);
  if (name === "approval_required") showApproval(payload);
  if (name === "approval_resolved" || name === "approval_expired") hideApproval();
  if (name === "change_preview") showDiff(payload);
  if (name === "change_applied") {
    if (payload.path) state.changes.add(payload.path);
    els.changeCount.textContent = state.changes.size;
  }
  if (name === "verification_started") setVerification("running", "Running", payload.command || "Running local verification…");
  if (name === "verification_completed") {
    setVerification(payload.passed ? "passed" : "failed", payload.passed ? "Passed" : "Failed", verificationDetail(payload));
  }
  if (name === "controller_run_finished") {
    const completed = payload.result_status === "completed";
    els.runStatus.textContent = completed ? "Completed" : labelStatus(payload.result_status);
    setBusy(false, completed ? "Complete" : "Stopped");
    showSummary(payload.final_text || "Run finished.");
  }
  if (name === "controller_run_failed") {
    els.runStatus.textContent = "Failed";
    setBusy(false, "Error", true);
    showSummary(payload.error || "The run failed.");
  }
}

function showApproval(payload) {
  state.pendingApproval = payload;
  els.approvalRisk.textContent = payload.risk || "write";
  els.approvalTitle.textContent = `Allow ${payload.tool || "this operation"}?`;
  els.approvalArguments.textContent = JSON.stringify(payload.arguments || {}, null, 2);
  els.approvalPanel.classList.remove("hidden");
  els.runStatus.textContent = "Waiting for approval";
}

function hideApproval() {
  state.pendingApproval = null;
  els.approvalPanel.classList.add("hidden");
  els.runStatus.textContent = "Running";
}

async function decideApproval(approved) {
  const pending = state.pendingApproval;
  if (!pending || !state.runId) return;
  els.approveButton.disabled = true;
  els.denyButton.disabled = true;
  try {
    const response = await fetch(`/api/runs/${state.runId}/approvals/${pending.approval_id}`, {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({approved}),
    });
    const data = await readJson(response);
    if (!response.ok) throw new Error(data.detail || "Approval could not be submitted.");
  } catch (error) {
    toast(error.message, true);
  } finally {
    els.approveButton.disabled = false;
    els.denyButton.disabled = false;
  }
}

function showDiff(payload) {
  els.diffStats.textContent = `+${payload.additions || 0} / -${payload.deletions || 0}`;
  els.diffMeta.textContent = `${payload.path || "file"}${payload.diff_truncated ? " · preview truncated" : ""}`;
  const code = document.createElement("code");
  String(payload.diff || "").split("\n").forEach((line) => {
    const span = document.createElement("span");
    span.className = `diff-line ${diffClass(line)}`;
    span.textContent = line || " ";
    code.appendChild(span);
  });
  els.diffView.replaceChildren(code);
}

function setVerification(tone, label, message) {
  els.verificationBadge.className = `verification-badge ${tone}`;
  els.verificationBadge.textContent = label;
  els.verificationStatus.textContent = label;
  els.verificationMessage.textContent = message;
}

function showSummary(text) {
  els.summaryText.textContent = text;
  els.summaryPanel.classList.remove("hidden");
}

async function refreshSnapshot(runId) {
  try {
    const response = await fetch(`/api/runs/${runId}`);
    const data = await readJson(response);
    if (!response.ok) return;
    els.runStatus.textContent = labelStatus(data.status);
    if (data.result?.session_id) els.sessionId.textContent = shortId(data.result.session_id);
    if (data.result?.final_text) showSummary(data.result.final_text);
    if (data.error) showSummary(data.error);
  } catch (_) {
    // The event stream already contains the useful result; snapshot refresh is best effort.
  }
}

function resetRunView() {
  if (state.source) state.source.close();
  state.runId = null;
  state.source = null;
  state.eventCount = 0;
  state.changes = new Set();
  state.pendingApproval = null;
  els.eventCount.textContent = "0 events";
  els.changeCount.textContent = "0";
  els.sessionId.textContent = "—";
  els.runStatus.textContent = "Starting";
  els.timeline.innerHTML = "";
  els.approvalPanel.classList.add("hidden");
  els.summaryPanel.classList.add("hidden");
  els.diffStats.textContent = "No changes";
  els.diffMeta.textContent = "A write preview will appear before approval.";
  els.diffView.innerHTML = '<code><span class="diff-placeholder">Waiting for the agent to prepare a change…</span></code>';
  setVerification("neutral", "Not run", "The final status is decided by local command results, not by model claims.");
}

function setBusy(busy, label, error = false) {
  els.runButton.disabled = busy;
  els.connectionLabel.textContent = label;
  els.connectionDot.className = `connection-dot${error ? " error" : busy ? " busy" : ""}`;
}

function overviewDetail(payload) {
  const files = [...(payload.manifests || []), ...(payload.entry_points || [])];
  return files.length ? files.slice(0, 5).join(" · ") : "Workspace overview generated";
}

function modelDetail(payload) {
  const tools = payload.tool_calls || [];
  const content = String(payload.content || "").trim();
  if (tools.length) return `Requested: ${tools.join(", ")}`;
  return content.slice(0, 220) || "Model returned a response";
}

function toolDetail(payload) {
  if (payload.tool === "run_command" && payload.arguments?.command) return payload.arguments.command;
  if (payload.arguments?.path) return payload.arguments.path;
  return compactPayload(payload.arguments || {});
}

function verificationDetail(payload) {
  const exitCode = payload.exit_code ?? "?";
  const duration = Number(payload.duration_seconds || 0).toFixed(2);
  return `Exit ${exitCode} · ${duration}s${payload.command ? ` · ${payload.command}` : ""}`;
}

function compactPayload(value) {
  const text = JSON.stringify(value || {});
  return text.length > 220 ? `${text.slice(0, 217)}…` : text;
}

function diffClass(line) {
  if (line.startsWith("+++") || line.startsWith("---")) return "header";
  if (line.startsWith("+")) return "add";
  if (line.startsWith("-")) return "remove";
  if (line.startsWith("@@")) return "hunk";
  return "";
}

function labelStatus(status) {
  return String(status || "unknown").replaceAll("_", " ").replace(/\b\w/g, (c) => c.toUpperCase());
}

function humanize(name) { return labelStatus(name); }
function shortId(value) { return value ? `${String(value).slice(0, 8)}…` : "—"; }
function formatTime(value) { try { return new Date(value).toLocaleTimeString([], {hour: "2-digit", minute: "2-digit", second: "2-digit"}); } catch (_) { return ""; } }
function escapeHtml(value) { const node = document.createElement("span"); node.textContent = String(value ?? ""); return node.innerHTML; }
async function readJson(response) { try { return await response.json(); } catch (_) { return {}; } }

function toast(message, error = false) {
  els.toast.textContent = message;
  els.toast.className = `toast show${error ? " error" : ""}`;
  window.setTimeout(() => { els.toast.className = "toast"; }, 3400);
}
