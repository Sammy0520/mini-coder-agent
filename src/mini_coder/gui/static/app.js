const state = {
  runId: null,
  source: null,
  eventCount: 0,
  changes: new Set(),
  pendingApproval: null,
  folderPath: null,
  folderParent: null,
};

const els = Object.fromEntries([
  "workspace", "selectWorkspaceButton", "configPath", "task", "runButton", "connectionDot", "connectionLabel",
  "runStatus", "sessionId", "verificationStatus", "changeCount", "eventCount",
  "timeline", "approvalPanel", "approvalRisk", "approvalTitle", "approvalArguments",
  "approveButton", "denyButton", "diffStats", "diffMeta", "diffView",
  "verificationBadge", "verificationMessage", "summaryPanel", "summaryText", "toast",
  "folderModal", "folderCloseButton", "folderCancelButton", "folderChooseButton",
  "folderRoots", "folderUpButton", "folderPath", "folderGoButton", "folderList",
  "folderCurrentHint",
].map((id) => [id, document.getElementById(id)]));

const terminalEvents = new Set(["controller_run_finished", "controller_run_failed"]);

els.runButton.addEventListener("click", startRun);
els.selectWorkspaceButton.addEventListener("click", openFolderBrowser);
els.folderCloseButton.addEventListener("click", closeFolderBrowser);
els.folderCancelButton.addEventListener("click", closeFolderBrowser);
els.folderChooseButton.addEventListener("click", chooseCurrentFolder);
els.folderUpButton.addEventListener("click", () => loadDirectory(state.folderParent));
els.folderGoButton.addEventListener("click", () => loadDirectory(els.folderPath.value.trim()));
els.folderPath.addEventListener("keydown", (event) => {
  if (event.key === "Enter") loadDirectory(els.folderPath.value.trim());
});
els.folderModal.addEventListener("click", (event) => {
  if (event.target === els.folderModal) closeFolderBrowser();
});
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && !els.folderModal.classList.contains("hidden")) closeFolderBrowser();
});
els.approveButton.addEventListener("click", () => decideApproval(true));
els.denyButton.addEventListener("click", () => decideApproval(false));
loadDefaults();

async function loadDefaults() {
  try {
    const response = await fetch("/api/bootstrap");
    const data = await readJson(response);
    if (!response.ok) return;
    if (!els.workspace.value) els.workspace.value = data.default_workspace || "";
    if (!els.configPath.value) els.configPath.value = data.default_config_path || "agent.toml";
  } catch (_) {
    // Fields remain editable if startup information is temporarily unavailable.
  }
}

function openFolderBrowser() {
  els.folderModal.classList.remove("hidden");
  document.body.classList.add("modal-open");
  loadDirectory(els.workspace.value.trim() || null);
}

function closeFolderBrowser() {
  els.folderModal.classList.add("hidden");
  document.body.classList.remove("modal-open");
}

function chooseCurrentFolder() {
  if (!state.folderPath) return;
  els.workspace.value = state.folderPath;
  closeFolderBrowser();
  toast("已选择项目文件夹");
}

async function loadDirectory(path) {
  els.folderList.classList.add("folder-loading");
  els.folderList.innerHTML = '<div class="folder-empty">正在读取文件夹…</div>';
  els.folderChooseButton.disabled = true;
  try {
    const query = path ? `?path=${encodeURIComponent(path)}` : "";
    const response = await fetch(`/api/directories${query}`);
    const data = await readJson(response);
    if (!response.ok) throw new Error(data.detail || "无法读取这个文件夹。");
    state.folderPath = data.current;
    state.folderParent = data.parent;
    els.folderPath.value = data.current;
    els.folderCurrentHint.textContent = `当前：${data.current}`;
    els.folderUpButton.disabled = !data.parent;
    renderRoots(data.roots || [], data.current);
    renderDirectories(data.directories || []);
    els.folderChooseButton.disabled = false;
  } catch (error) {
    els.folderList.innerHTML = `<div class="folder-empty">${escapeHtml(error.message)}</div>`;
    toast(error.message, true);
  } finally {
    els.folderList.classList.remove("folder-loading");
  }
}

function renderRoots(roots, current) {
  els.folderRoots.replaceChildren();
  roots.forEach((root) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = `root-button${current.toLowerCase().startsWith(root.toLowerCase()) ? " active" : ""}`;
    button.textContent = root;
    button.addEventListener("click", () => loadDirectory(root));
    els.folderRoots.appendChild(button);
  });
}

function renderDirectories(directories) {
  els.folderList.replaceChildren();
  if (!directories.length) {
    els.folderList.innerHTML = '<div class="folder-empty">这个文件夹中没有子文件夹，可以直接选择当前文件夹。</div>';
    return;
  }
  directories.forEach((directory) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "folder-item";
    const icon = document.createElement("span");
    icon.className = "folder-icon";
    icon.textContent = "▰";
    const label = document.createElement("span");
    label.textContent = directory.name;
    button.append(icon, label);
    button.addEventListener("click", () => loadDirectory(directory.path));
    els.folderList.appendChild(button);
  });
}

async function startRun() {
  const task = els.task.value.trim();
  const workspace = els.workspace.value.trim();
  const configPath = els.configPath.value.trim();
  if (!task || !workspace) {
    toast("请先选择项目文件夹，并填写要完成的任务。", true);
    return;
  }
  resetRunView();
  setBusy(true, "正在启动 Agent…");
  try {
    const response = await fetch("/api/runs", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({task, workspace, config_path: configPath || null, auto: false}),
    });
    const data = await readJson(response);
    if (!response.ok) throw new Error(data.detail || "无法开始执行任务。");
    state.runId = data.run_id;
    els.runStatus.textContent = labelStatus(data.status);
    connectEvents(data.run_id);
  } catch (error) {
    setBusy(false, "发生错误");
    els.runStatus.textContent = "启动失败";
    toast(error.message, true);
  }
}

function connectEvents(runId) {
  if (state.source) state.source.close();
  const source = new EventSource(`/api/runs/${runId}/events`);
  state.source = source;
  source.addEventListener("open", () => setBusy(true, "正在执行"));
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
    els.connectionLabel.textContent = "正在重新连接…";
    els.connectionDot.className = "connection-dot busy";
  };
}

function renderEvent(envelope) {
  state.eventCount += 1;
  els.eventCount.textContent = `${state.eventCount} 条记录`;
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
    controller_run_created: ["01", "任务已创建", `项目：${payload.workspace || ""}`, ""],
    session_created: ["S", "会话已保存", shortId(payload.session_id), "success"],
    workspace_overview_generated: ["⌁", "已理解项目结构", overviewDetail(payload), ""],
    run_started: ["▶", "Agent 已启动", payload.resumed ? "继续已有会话" : "开始新的会话", ""],
    model_request_started: ["AI", "正在请求模型", `第 ${payload.step || ""} 步`, ""],
    model_response_received: ["AI", "模型已响应", modelDetail(payload), ""],
    tool_call_requested: ["↳", `调用工具：${tool}`, toolDetail(payload), ""],
    approval_required: ["!", "需要确认操作", `${tool} · ${payload.risk || "write"}`, "warning"],
    approval_resolved: [payload.approved ? "✓" : "×", payload.approved ? "已允许" : "已拒绝", tool, payload.approved ? "success" : "error"],
    change_preview: ["Δ", `准备修改：${payload.path || "文件"}`, `新增 ${payload.additions || 0} 行 / 删除 ${payload.deletions || 0} 行`, "warning"],
    change_applied: ["✓", `已修改：${payload.path || "文件"}`, `新增 ${payload.additions || 0} 行 / 删除 ${payload.deletions || 0} 行`, "success"],
    phase_changed: ["→", `进入阶段：${payload.phase || "working"}`, `${payload.previous || "start"} → ${payload.phase || "working"}`, ""],
    verification_started: ["V", "开始本地验证", payload.command || "正在运行项目检查", ""],
    verification_completed: [payload.passed ? "✓" : "×", payload.passed ? "验证通过" : "验证失败", verificationDetail(payload), payload.passed ? "success" : "error"],
    run_completed: ["✓", "Agent 已完成任务", `状态：${payload.verification_status || payload.result_status || "completed"}`, "success"],
    run_failed: ["×", "Agent 执行失败", payload.stop_reason || payload.result_status || "执行失败", "error"],
    controller_run_finished: ["✓", "本次执行已结束", `共 ${payload.steps || 0} 个模型步骤`, payload.result_status === "completed" ? "success" : "error"],
    controller_run_failed: ["×", "运行控制器出错", payload.error || "发生未知错误", "error"],
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
  if (name === "verification_started") setVerification("running", "验证中", payload.command || "正在运行项目检查…");
  if (name === "verification_completed") {
    setVerification(payload.passed ? "passed" : "failed", payload.passed ? "已通过" : "未通过", verificationDetail(payload));
  }
  if (name === "controller_run_finished") {
    const completed = payload.result_status === "completed";
    els.runStatus.textContent = completed ? "已完成" : labelStatus(payload.result_status);
    setBusy(false, completed ? "执行完成" : "已停止");
    showSummary(payload.final_text || "本次执行已结束。");
  }
  if (name === "controller_run_failed") {
    els.runStatus.textContent = "执行失败";
    setBusy(false, "发生错误", true);
    showSummary(payload.error || "本次执行失败。");
  }
}

function showApproval(payload) {
  state.pendingApproval = payload;
  els.approvalRisk.textContent = payload.risk || "write";
  els.approvalTitle.textContent = `允许 Agent 执行 ${payload.tool || "这项操作"} 吗？`;
  els.approvalArguments.textContent = JSON.stringify(payload.arguments || {}, null, 2);
  els.approvalPanel.classList.remove("hidden");
  els.runStatus.textContent = "等待你的确认";
}

function hideApproval() {
  state.pendingApproval = null;
  els.approvalPanel.classList.add("hidden");
  els.runStatus.textContent = "正在执行";
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
    if (!response.ok) throw new Error(data.detail || "无法提交确认结果。");
  } catch (error) {
    toast(error.message, true);
  } finally {
    els.approveButton.disabled = false;
    els.denyButton.disabled = false;
  }
}

function showDiff(payload) {
  els.diffStats.textContent = `+${payload.additions || 0} / -${payload.deletions || 0}`;
  els.diffMeta.textContent = `${payload.path || "文件"}${payload.diff_truncated ? " · 预览已截断" : ""}`;
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
  els.eventCount.textContent = "0 条记录";
  els.changeCount.textContent = "0";
  els.sessionId.textContent = "—";
  els.runStatus.textContent = "正在启动";
  els.timeline.innerHTML = "";
  els.approvalPanel.classList.add("hidden");
  els.summaryPanel.classList.add("hidden");
  els.diffStats.textContent = "暂无修改";
  els.diffMeta.textContent = "Agent 准备写入文件时，会先在这里展示修改内容。";
  els.diffView.innerHTML = '<code><span class="diff-placeholder">等待 Agent 准备修改…</span></code>';
  setVerification("neutral", "未运行", "Agent 完成修改后，会运行项目内的检查命令，用真实结果判断任务是否成功。");
}

function setBusy(busy, label, error = false) {
  els.runButton.disabled = busy;
  els.connectionLabel.textContent = label;
  els.connectionDot.className = `connection-dot${error ? " error" : busy ? " busy" : ""}`;
}

function overviewDetail(payload) {
  const files = [...(payload.manifests || []), ...(payload.entry_points || [])];
  return files.length ? files.slice(0, 5).join(" · ") : "已生成项目概览";
}

function modelDetail(payload) {
  const tools = payload.tool_calls || [];
  const content = String(payload.content || "").trim();
  if (tools.length) return `准备调用：${tools.join(", ")}`;
  return content.slice(0, 220) || "模型已返回结果";
}

function toolDetail(payload) {
  if (payload.tool === "run_command" && payload.arguments?.command) return payload.arguments.command;
  if (payload.arguments?.path) return payload.arguments.path;
  return compactPayload(payload.arguments || {});
}

function verificationDetail(payload) {
  const exitCode = payload.exit_code ?? "?";
  const duration = Number(payload.duration_seconds || 0).toFixed(2);
  return `退出码 ${exitCode} · ${duration} 秒${payload.command ? ` · ${payload.command}` : ""}`;
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
  const value = String(status || "unknown");
  const labels = {
    created: "已创建",
    running: "正在执行",
    waiting_for_approval: "等待确认",
    completed: "已完成",
    failed: "执行失败",
    denied: "已拒绝",
    interrupted: "已中断",
    unknown: "未知状态",
  };
  return labels[value] || value.replaceAll("_", " ");
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
