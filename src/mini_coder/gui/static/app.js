const state = {
  runId: null,
  source: null,
  workspace: "",
  configPath: "",
  sessionTitle: "",
  sessionId: null,
  draft: true,
  runActive: false,
  cancellationRequested: false,
  currentTurn: 1,
  turnModelCalls: 0,
  turnToolCalls: 0,
  usedSessionMemory: false,
  eventCount: 0,
  executionGroups: [],
  readGroup: null,
  changes: new Map(),
  pendingApproval: null,
  folderPath: null,
  folderParent: null,
  startAfterSetup: false,
  codeDetail: null,
  codeTab: "diff",
  currentSequence: 0,
  activeRuns: new Map(),
  changesExpanded: false,
  sessionPoller: null,
  taskBrief: null,
  phase: "discover",
};

const elementIds = [
  "newSessionButton", "sessionCount", "sessionList",
  "connectionDot", "connectionLabel", "conversationEyebrow", "conversationTitle", "conversationProject",
  "detailsToggleButton", "stopRunButton", "runStatus", "sessionName", "verificationStatus", "changeCount", "conversation",
  "welcomeState", "messageList", "task", "composerHint", "runButton", "inspector", "closeInspectorButton",
  "approvalPanel", "approvalHeading", "approvalRisk", "approvalTitle", "approvalArguments", "approveButton",
  "denyButton", "undoButton", "changesToggleButton", "diffStats", "changeList", "verificationBadge", "verificationMessage", "eventCount", "turnSummary",
  "executionList", "sessionModal", "sessionModalClose", "sessionModalCancel", "sessionModalConfirm",
  "sessionDialogEyebrow", "sessionDialogTitle", "sessionTitleField", "sessionTitleInput", "workspace",
  "selectWorkspaceButton", "configPath", "folderModal", "folderCloseButton", "folderCancelButton",
  "folderChooseButton", "folderRoots", "folderUpButton", "folderPath", "folderGoButton", "folderList",
  "folderCurrentHint", "codeModal", "codeModalClose", "codeModalDone", "codeDialogTitle",
  "codeVersionWarning", "diffTabButton", "afterTabButton", "beforeTabButton", "fullCodeView",
  "fullCodeStats", "toast",
];
const els = Object.fromEntries(elementIds.map((id) => [id, document.getElementById(id)]));
const terminalEvents = new Set(["controller_run_finished", "controller_run_failed"]);
const readTools = new Set(["read_file", "list_files", "search_text"]);

bindEvents();
bootstrap();

function bindEvents() {
  els.newSessionButton.addEventListener("click", () => openSessionModal());
  els.sessionModalClose.addEventListener("click", closeSessionModal);
  els.sessionModalCancel.addEventListener("click", closeSessionModal);
  els.sessionModalConfirm.addEventListener("click", confirmSessionSetup);
  els.sessionModal.addEventListener("click", (event) => {
    if (event.target === els.sessionModal) closeSessionModal();
  });
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
  els.runButton.addEventListener("click", startRun);
  els.stopRunButton.addEventListener("click", stopRun);
  els.undoButton.addEventListener("click", undoLastChange);
  els.changesToggleButton.addEventListener("click", () => {
    state.changesExpanded = !state.changesExpanded;
    renderChanges();
  });
  els.task.addEventListener("keydown", (event) => {
    if ((event.ctrlKey || event.metaKey) && event.key === "Enter") startRun();
  });
  document.querySelectorAll("[data-prompt]").forEach((button) => {
    button.addEventListener("click", () => {
      els.task.value = button.dataset.prompt || "";
      els.task.focus();
    });
  });
  els.detailsToggleButton.addEventListener("click", () => els.inspector.classList.add("open"));
  els.closeInspectorButton.addEventListener("click", () => els.inspector.classList.remove("open"));
  els.approveButton.addEventListener("click", () => decideApproval(true));
  els.denyButton.addEventListener("click", () => decideApproval(false));
  els.codeModalClose.addEventListener("click", closeCodeModal);
  els.codeModalDone.addEventListener("click", closeCodeModal);
  els.codeModal.addEventListener("click", (event) => {
    if (event.target === els.codeModal) closeCodeModal();
  });
  els.diffTabButton.addEventListener("click", () => selectCodeTab("diff"));
  els.afterTabButton.addEventListener("click", () => selectCodeTab("after"));
  els.beforeTabButton.addEventListener("click", () => selectCodeTab("before"));
  document.addEventListener("keydown", (event) => {
    if (event.key !== "Escape") return;
    if (!els.codeModal.classList.contains("hidden")) closeCodeModal();
    else if (!els.folderModal.classList.contains("hidden")) closeFolderBrowser();
    else if (!els.sessionModal.classList.contains("hidden")) closeSessionModal();
  });
}

async function bootstrap() {
  try {
    const response = await fetch("/api/bootstrap");
    const data = await readJson(response);
    if (!response.ok) throw new Error(data.detail || "无法读取启动信息");
    let saved = {};
    try { saved = JSON.parse(localStorage.getItem("mini-coder-project") || "{}"); } catch (_) { saved = {}; }
    state.workspace = saved.workspace || data.default_workspace || "";
    const legacyProjectConfig = saved.workspace ? joinPath(saved.workspace, "agent.toml") : "";
    state.configPath = saved.configPath && !samePath(saved.configPath, legacyProjectConfig)
      ? saved.configPath
      : (data.default_config_path || "agent.toml");
    updateProjectDisplay();
    resetDraft();
    await syncActiveRuns();
    await loadSessions();
    state.sessionPoller = window.setInterval(refreshBackgroundRuns, 3000);
  } catch (error) {
    setConnection("启动信息读取失败", "error");
    toast(error.message, true);
  }
}

function openSessionModal(startAfterSetup = false) {
  state.startAfterSetup = startAfterSetup;
  els.sessionDialogEyebrow.textContent = "新建会话";
  els.sessionDialogTitle.textContent = "设置会话与工作文件夹";
  els.sessionTitleField.classList.remove("hidden");
  els.sessionTitleInput.value = state.sessionTitle || suggestedTitle(els.task.value);
  els.workspace.value = state.workspace;
  els.configPath.value = state.configPath;
  els.sessionModalConfirm.textContent = "进入会话";
  els.sessionModal.classList.remove("hidden");
  document.body.classList.add("modal-open");
  window.setTimeout(() => els.sessionTitleInput.focus(), 0);
}

function closeSessionModal() {
  els.sessionModal.classList.add("hidden");
  if (els.folderModal.classList.contains("hidden") && els.codeModal.classList.contains("hidden")) {
    document.body.classList.remove("modal-open");
  }
  state.startAfterSetup = false;
}

async function confirmSessionSetup() {
  const workspace = els.workspace.value.trim();
  const configPath = els.configPath.value.trim();
  const title = els.sessionTitleInput.value.trim();
  if (!workspace) {
    toast("请先选择工作文件夹。", true);
    return;
  }
  if (!title) {
    toast("请为这次会话起一个容易识别的名称。", true);
    els.sessionTitleInput.focus();
    return;
  }
  const shouldStart = state.startAfterSetup;
  state.workspace = workspace;
  state.configPath = configPath || "agent.toml";
  state.sessionTitle = title;
  saveProject();
  updateProjectDisplay();
  closeSessionModal();
  resetDraft({keepTitle: true});
  await loadSessions();
  if (shouldStart) startRun();
}

function saveProject() {
  try {
    localStorage.setItem("mini-coder-project", JSON.stringify({
      workspace: state.workspace,
      configPath: state.configPath,
    }));
  } catch (_) {
    // The page remains usable when browser storage is unavailable.
  }
}

function updateProjectDisplay() {
  els.conversationProject.textContent = state.workspace || "尚未选择工作文件夹";
  els.composerHint.textContent = state.workspace
    ? `工作文件夹：${state.workspace}`
    : "新建会话时选择工作文件夹";
}

async function loadSessions(options = {}) {
  if (!options.silent) {
    els.sessionList.innerHTML = '<div class="sidebar-empty">正在读取会话…</div>';
  }
  try {
    const response = await fetch("/api/sessions");
    const data = await readJson(response);
    if (!response.ok) throw new Error(data.detail || "无法读取会话");
    renderSessions(data.sessions || []);
  } catch (error) {
    els.sessionList.innerHTML = `<div class="sidebar-empty">${escapeHtml(error.message)}</div>`;
    els.sessionCount.textContent = "0";
  }
}

function renderSessions(sessions) {
  els.sessionCount.textContent = String(sessions.length);
  els.sessionList.replaceChildren();
  if (!sessions.length) {
    els.sessionList.innerHTML = '<div class="sidebar-empty">还没有会话</div>';
    return;
  }
  sessions.forEach((session) => {
    const activeRun = activeRunForSession(session.session_id);
    const displayStatus = activeRun?.status || session.status;
    const button = document.createElement("button");
    button.type = "button";
    button.className = `session-item${state.sessionId === session.session_id ? " active" : ""}`;
    button.innerHTML = `
      <span class="session-item-icon">${sessionIcon(displayStatus)}</span>
      <span><strong>${escapeHtml(session.title || "未命名会话")}</strong><small>${escapeHtml(pathName(session.workspace))} · ${escapeHtml(sessionTime(session.updated_at))} · ${escapeHtml(labelStatus(displayStatus))}</small></span>`;
    button.addEventListener("click", () => loadSession(session.session_id));
    els.sessionList.appendChild(button);
  });
}

async function syncActiveRuns() {
  try {
    const response = await fetch("/api/runs");
    const data = await readJson(response);
    if (!response.ok) throw new Error(data.detail || "无法读取运行任务");
    state.activeRuns = new Map((data.runs || []).map((run) => [run.run_id, run]));
  } catch (_) {
    // A temporary polling failure should not interrupt the current conversation.
  }
}

function activeRunForSession(sessionId) {
  return [...state.activeRuns.values()].find((run) => run.session_id === sessionId) || null;
}

async function refreshBackgroundRuns() {
  await syncActiveRuns();
  await loadSessions({silent: true});
  if (state.runActive && state.runId && !state.source && state.activeRuns.has(state.runId)) {
    const run = state.activeRuns.get(state.runId);
    connectEvents(run.run_id, run.latest_sequence || state.currentSequence);
  }
}

async function loadSession(sessionId) {
  disconnectEvents();
  try {
    setConnection("正在打开会话", "busy");
    await syncActiveRuns();
    const response = await fetch(`/api/sessions/${encodeURIComponent(sessionId)}`);
    const data = await readJson(response);
    if (!response.ok) throw new Error(data.detail || "无法打开会话");
    const activeRun = activeRunForSession(data.session_id);
    state.draft = true;
    state.workspace = data.workspace || state.workspace;
    state.sessionId = data.session_id;
    state.sessionTitle = data.title || "未命名会话";
    state.runId = activeRun?.run_id || null;
    state.currentSequence = Number(activeRun?.latest_sequence || 0);
    state.runActive = Boolean(activeRun);
    state.cancellationRequested = false;
    state.currentTurn = Math.max(1, Number(data.turn_count || 1));
    state.turnModelCalls = 0;
    state.turnToolCalls = 0;
    state.usedSessionMemory = state.currentTurn > 1;
    state.eventCount = 0;
    state.executionGroups = (data.execution || []).map((item) => ({
      title: item.title,
      details: item.details || [],
      time: formatTime(item.time),
      icon: item.icon || "✓",
      count: 1,
    }));
    state.readGroup = null;
    state.taskBrief = data.working_memory?.task_brief || null;
    state.phase = data.phase || "finish";
    state.changesExpanded = false;
    state.changes = new Map();
    (data.changes || []).filter((item) => item.undo_status === "active").forEach((item) => {
      state.changes.set(item.change_id, {...item, key: item.change_id});
    });
    els.welcomeState.classList.add("hidden");
    els.messageList.replaceChildren();
    (data.conversation || []).forEach((message) => addMessage(message.role, message.content));
    els.conversationEyebrow.textContent = "历史会话";
    els.conversationTitle.textContent = state.sessionTitle;
    els.sessionName.textContent = state.sessionTitle;
    els.runStatus.textContent = labelStatus(activeRun?.status || data.status);
    setVerificationFromSession(data.verification_status, data.verifications || []);
    renderChanges();
    renderExecutionGroups("这个会话没有保存可展示的执行过程。 ");
    renderTurnSummary("已完成");
    els.task.value = "";
    els.task.disabled = Boolean(activeRun);
    els.task.placeholder = "继续告诉 Agent 接下来要做什么…";
    els.runButton.disabled = Boolean(activeRun);
    els.runButton.innerHTML = activeRun ? "<span>●</span> 执行中" : "<span>▶</span> 继续执行";
    els.stopRunButton.classList.toggle("hidden", !activeRun);
    els.stopRunButton.disabled = false;
    els.stopRunButton.textContent = "停止任务";
    updateProjectDisplay();
    els.composerHint.textContent = activeRun
      ? "这个任务仍在后台运行，你可以切换到其他会话"
      : "可以在这个会话中继续提出修改或追问";
    hideApprovalCard();
    if (activeRun) {
      addProgressMessage();
      renderProgressSteps();
      renderTurnSummary(labelStatus(activeRun.status));
      if (activeRun.pending_approval) showApproval(activeRun.pending_approval);
      connectEvents(activeRun.run_id, activeRun.latest_sequence || 0);
    } else {
      hideApprovalCard();
    }
    await loadSessions({silent: true});
    setConnection(activeRun ? labelStatus(activeRun.status) : "已就绪", activeRun ? "busy" : "");
    scrollConversation();
  } catch (error) {
    setConnection("打开失败", "error");
    toast(error.message, true);
  }
}

function resetDraft(options = {}) {
  disconnectEvents();
  state.runId = null;
  state.currentSequence = 0;
  state.sessionId = null;
  state.draft = true;
  state.runActive = false;
  state.cancellationRequested = false;
  state.currentTurn = 1;
  state.turnModelCalls = 0;
  state.turnToolCalls = 0;
  state.usedSessionMemory = false;
  state.eventCount = 0;
  state.executionGroups = [];
  state.readGroup = null;
  state.taskBrief = null;
  state.phase = "discover";
  state.changesExpanded = false;
  state.changes = new Map();
  state.pendingApproval = null;
  if (!options.keepTitle) state.sessionTitle = "";
  els.conversationEyebrow.textContent = "新会话";
  els.conversationTitle.textContent = state.sessionTitle || "准备开始一个任务";
  els.sessionName.textContent = state.sessionTitle || "新会话";
  els.runStatus.textContent = "尚未开始";
  els.welcomeState.classList.remove("hidden");
  els.messageList.replaceChildren();
  els.task.disabled = false;
  els.task.placeholder = "描述你希望 Agent 完成的任务…";
  els.runButton.disabled = false;
  els.runButton.innerHTML = "<span>▶</span> 开始执行";
  els.stopRunButton.classList.add("hidden");
  els.approvalPanel.classList.add("hidden");
  renderChanges();
  setVerification("neutral", "未运行", "完成修改后，Agent 会运行项目内的检查命令。");
  renderExecutionGroups();
  renderTurnSummary("尚未开始");
  updateProjectDisplay();
  setConnection("已就绪");
}

async function startRun() {
  if (!state.draft) return;
  const task = els.task.value.trim();
  if (!task) {
    toast("请先描述希望 Agent 完成的任务。", true);
    els.task.focus();
    return;
  }
  if (!state.workspace || (!state.sessionTitle && !state.sessionId)) {
    openSessionModal(true);
    return;
  }
  prepareRunningView(task);
  try {
    const response = await fetch("/api/runs", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({
        title: state.sessionTitle,
        session_id: state.sessionId || null,
        task,
        workspace: state.workspace,
        config_path: state.configPath || null,
        auto: false,
      }),
    });
    const data = await readJson(response);
    if (!response.ok) throw new Error(data.detail || "无法开始执行任务");
    state.runId = data.run_id;
    state.currentSequence = 0;
    state.activeRuns.set(data.run_id, data);
    els.runStatus.textContent = labelStatus(data.status);
    connectEvents(data.run_id, 0);
  } catch (error) {
    markProgressDone("任务未能启动");
    setConnection("启动失败", "error");
    els.runStatus.textContent = "启动失败";
    els.task.disabled = false;
    els.runButton.disabled = false;
    els.runButton.innerHTML = `<span>▶</span> ${state.sessionId ? "继续执行" : "开始执行"}`;
    state.runActive = false;
    state.runId = null;
    if (state.sessionId) state.currentTurn = Math.max(1, state.currentTurn - 1);
    els.stopRunButton.classList.add("hidden");
    renderTurnSummary("启动失败");
    renderChanges();
    toast(friendlyError(error.message), true);
  }
}

async function stopRun() {
  if (!state.runId || !state.runActive || state.cancellationRequested) return;
  state.cancellationRequested = true;
  els.stopRunButton.disabled = true;
  els.stopRunButton.textContent = "正在停止…";
  els.runStatus.textContent = "正在停止";
  markProgressStopping();
  try {
    const response = await fetch(`/api/runs/${encodeURIComponent(state.runId)}/cancel`, {
      method: "POST",
    });
    const data = await readJson(response);
    if (!response.ok) throw new Error(data.detail || "无法停止当前任务");
  } catch (error) {
    state.cancellationRequested = false;
    els.stopRunButton.disabled = false;
    els.stopRunButton.textContent = "停止任务";
    els.runStatus.textContent = "正在执行";
    const heading = document.getElementById("progressTitle");
    if (heading) heading.textContent = "正在了解项目并完成任务…";
    setConnection("正在执行", "busy");
    toast(friendlyError(error.message), true);
  }
}

function prepareRunningView(task) {
  const continuing = Boolean(state.sessionId);
  state.runActive = true;
  state.cancellationRequested = false;
  state.currentTurn = continuing ? state.currentTurn + 1 : 1;
  state.turnModelCalls = 0;
  state.turnToolCalls = 0;
  state.usedSessionMemory = continuing;
  state.eventCount = 0;
  state.executionGroups = [];
  state.readGroup = null;
  state.taskBrief = null;
  state.phase = "discover";
  if (!continuing) state.changes = new Map();
  state.pendingApproval = null;
  els.welcomeState.classList.add("hidden");
  if (!continuing) els.messageList.replaceChildren();
  addMessage("user", task);
  addProgressMessage();
  els.conversationEyebrow.textContent = "正在协作";
  els.conversationTitle.textContent = state.sessionTitle;
  els.sessionName.textContent = state.sessionTitle;
  els.runStatus.textContent = "正在启动";
  els.task.disabled = true;
  els.runButton.disabled = true;
  els.runButton.innerHTML = "<span>●</span> 执行中";
  els.stopRunButton.disabled = false;
  els.stopRunButton.textContent = "停止任务";
  els.stopRunButton.classList.remove("hidden");
  els.approvalPanel.classList.add("hidden");
  renderChanges();
  setVerification("neutral", "等待运行", "Agent 完成修改后会运行本地检查。");
  renderExecutionGroups();
  renderTurnSummary("执行中");
  setConnection("正在启动", "busy");
}

function disconnectEvents() {
  if (state.source) state.source.close();
  state.source = null;
}

function connectEvents(runId, after = 0) {
  disconnectEvents();
  state.currentSequence = Number(after || 0);
  const source = new EventSource(`/api/runs/${encodeURIComponent(runId)}/events?after=${state.currentSequence}`);
  state.source = source;
  source.addEventListener("open", () => {
    if (state.runId === runId) setConnection("正在执行", "busy");
  });
  source.addEventListener("run-event", (message) => {
    if (state.runId !== runId || state.source !== source) return;
    const envelope = JSON.parse(message.data);
    state.currentSequence = Math.max(state.currentSequence, Number(envelope.sequence || 0));
    const active = state.activeRuns.get(runId) || {run_id: runId};
    state.activeRuns.set(runId, {...active, latest_sequence: state.currentSequence});
    const wasCurrent = state.runId === runId;
    handleEvent(envelope);
    if (terminalEvents.has(envelope.event)) {
      source.close();
      if (state.source === source) state.source = null;
      state.activeRuns.delete(runId);
      refreshSnapshot(runId, wasCurrent);
    }
  });
  source.onerror = () => {
    if (state.source === source && state.runId === runId) setConnection("正在重新连接…", "busy");
  };
}

function handleEvent(envelope) {
  const name = envelope.event;
  const payload = envelope.payload || {};
  const timestamp = envelope.timestamp;
  if (name === "session_created") {
    state.sessionId = payload.session_id || state.sessionId;
    const active = state.activeRuns.get(state.runId);
    if (active) state.activeRuns.set(state.runId, {...active, session_id: state.sessionId});
    loadSessions({silent: true});
  } else if (name === "session_turn_started") {
    state.currentTurn = Math.max(1, Number(payload.turn || state.currentTurn));
    state.usedSessionMemory = state.currentTurn > 1;
    renderTurnSummary("执行中");
  } else if (name === "workspace_overview_generated") {
    addExecution("先了解了一下项目结构", overviewDetail(payload), timestamp, "⌁");
  } else if (name === "task_framed") {
    state.taskBrief = payload;
    renderTaskBrief();
    addExecution("已经理解你想完成什么", taskBriefDetail(payload), timestamp, "◎");
  } else if (name === "phase_changed") {
    state.phase = payload.phase || state.phase;
    updatePhaseHeading(state.phase);
  } else if (name === "completion_reserve_started") {
    addExecution("开始收尾，优先完成和检查已有改动", "不再扩展可选内容", timestamp, "→");
    updatePhaseHeading("finish");
  } else if (name === "tool_call_requested") {
    state.turnToolCalls += 1;
    renderTurnSummary("执行中");
    handleToolRequest(payload, timestamp);
  } else if (name === "approval_required") {
    showApproval(payload);
  } else if (name === "approval_resolved" || name === "approval_expired") {
    hideApproval(payload.approved, name === "approval_expired");
  } else if (name === "change_preview") {
    rememberChangePreview(payload, timestamp);
  } else if (name === "change_applied") {
    rememberAppliedChange(payload);
  } else if (name === "verification_completed") {
    const supporting = payload.verification_mode === "expected_rejection" && payload.check_passed;
    const title = supporting
      ? "确认了无效输入会被正确拒绝"
      : (payload.passed ? "运行测试，确认功能可以正常使用" : "运行测试后发现还有问题");
    addExecution(title, verificationDetail(payload), timestamp, (payload.passed || supporting) ? "✓" : "!");
    if (supporting) setVerification("neutral", "还需检查", friendlyVerification(payload));
    else setVerification(payload.passed ? "passed" : "failed", payload.passed ? "已通过" : "未通过", friendlyVerification(payload));
  } else if (name === "verification_invalidated") {
    setVerification("neutral", "需要重跑", "代码又发生了变化，需要重新运行检查。 ");
  } else if (name === "tool_call_denied") {
    addExecution("操作已被拒绝", friendlyToolName(payload.tool), timestamp, "×");
  } else if (name === "model_response_received") {
    state.turnModelCalls += 1;
    renderTurnSummary("执行中");
  } else if (name === "model_error") {
    addExecution("模型请求遇到问题", payload.error || "稍后可以重新尝试", timestamp, "!");
  } else if (name === "context_compacted") {
    state.usedSessionMemory = state.currentTurn > 1;
    renderTurnSummary("执行中");
    addExecution("已整理较长的上下文", "保留重要信息后继续执行", timestamp, "·");
  } else if (name === "run_completed") {
    updatePhaseHeading("finish");
  } else if (name === "completion_evidence") {
    const paths = Array.isArray(payload.changed_files) ? payload.changed_files : [];
    const checks = Array.isArray(payload.checks) ? payload.checks : [];
    const detail = [
      paths.length ? `修改：${paths.join("、")}` : "没有修改文件",
      payload.verification_status === "passed"
        ? "正常运行检查已通过"
        : (checks.length ? "检查结果尚未完全通过" : "这次没有需要运行的检查"),
      payload.remaining_issue ? `仍需处理：${payload.remaining_issue}` : "",
    ].filter(Boolean).join("\n");
    addExecution(payload.completed ? "已经整理好本次结果" : "已保存当前进度", detail, timestamp, payload.completed ? "✓" : "!");
  } else if (name === "controller_cancel_requested") {
    state.cancellationRequested = true;
    els.runStatus.textContent = "正在停止";
    els.stopRunButton.disabled = true;
    els.stopRunButton.textContent = "正在停止…";
    els.approvalPanel.classList.add("hidden");
    markProgressStopping();
  } else if (name === "controller_run_finished") {
    finishRun(payload);
  } else if (name === "controller_run_failed") {
    failRun(payload.error || "本次执行失败");
  }
}

function handleToolRequest(payload, timestamp) {
  const tool = payload.tool || "tool";
  const args = payload.arguments || {};
  if (readTools.has(tool)) {
    const detail = args.path || args.query || friendlyToolName(tool);
    if (state.readGroup) {
      state.readGroup.count += 1;
      state.readGroup.title = `查看和阅读了项目文件（${state.readGroup.count} 项）`;
      state.readGroup.details.push(`${friendlyToolName(tool)}：${detail}`);
      renderExecutionGroups();
    } else {
      state.readGroup = addExecution("查看和阅读了项目文件", `${friendlyToolName(tool)}：${detail}`, timestamp, "⌕", true);
    }
    return;
  }
  state.readGroup = null;
  if (tool === "write_file" || tool === "edit_file") {
    return;
  } else if (tool === "run_command") {
    const verifying = args.purpose === "verify";
    addExecution(verifying ? "运行测试，检查修改是否正确" : "运行一项本地操作", args.command || "执行本地命令", timestamp, "▶");
    if (verifying) setVerification("running", "验证中", "正在运行项目测试…");
  } else {
    addExecution(friendlyToolName(tool), technicalToolDetail(tool, args), timestamp, "↳");
  }
}

function addExecution(title, detail, timestamp, icon = "✓", mergeableRead = false) {
  const group = {
    title,
    details: detail ? [String(detail)] : [],
    time: formatTime(timestamp),
    icon,
    count: 1,
    mergeableRead,
  };
  state.executionGroups.push(group);
  state.eventCount = state.executionGroups.length;
  renderExecutionGroups();
  renderProgressSteps();
  return group;
}

function renderExecutionGroups(emptyMessage = "执行步骤会以简明方式显示") {
  els.eventCount.textContent = `${state.executionGroups.length} 项`;
  els.executionList.replaceChildren();
  if (!state.executionGroups.length) {
    els.executionList.innerHTML = `<div class="detail-empty">${escapeHtml(emptyMessage)}</div>`;
    return;
  }
  state.executionGroups.forEach((group) => {
    const details = document.createElement("details");
    details.className = "execution-item";
    details.innerHTML = `
      <summary><span class="execution-icon">${escapeHtml(group.icon)}</span><span>${escapeHtml(group.title)}</span><span class="execution-time">${escapeHtml(group.time)}</span></summary>
      <pre class="execution-details">${escapeHtml(group.details.join("\n"))}</pre>`;
    els.executionList.appendChild(details);
  });
  els.executionList.scrollTop = els.executionList.scrollHeight;
}

function addProgressMessage() {
  const wrapper = document.createElement("div");
  wrapper.className = "message agent progress-message";
  wrapper.id = "activeProgressMessage";
  wrapper.innerHTML = `
    <div class="message-author"><span class="author-dot">AI</span><span>Mini Coder</span></div>
    <div id="activeProgressCard" class="progress-card">
      <div class="progress-card-header"><span class="progress-spinner"></span><span id="progressTitle">正在了解项目并完成任务…</span></div>
      <div id="taskBriefCard" class="task-brief-card hidden"></div>
      <div id="progressSteps" class="progress-steps"><div class="progress-step"><span class="progress-step-icon">·</span><span>准备开始</span></div></div>
    </div>`;
  els.messageList.appendChild(wrapper);
  scrollConversation();
}

function renderTaskBrief() {
  const target = document.getElementById("taskBriefCard");
  if (!target || !state.taskBrief) return;
  const brief = state.taskBrief;
  const assumptions = Array.isArray(brief.assumptions) ? brief.assumptions : [];
  const checks = Array.isArray(brief.acceptance_checks) ? brief.acceptance_checks : [];
  target.classList.remove("hidden");
  target.innerHTML = `
    <strong>我会这样理解这次任务</strong>
    <p>${escapeHtml(brief.goal || "完成你刚刚提出的目标")}</p>
    ${(assumptions.length || checks.length) ? `<details><summary>查看我采用的默认理解</summary><ul>${assumptions.slice(0, 4).map((item) => `<li>${escapeHtml(item)}</li>`).join("")}${checks.slice(0, 3).map((item) => `<li>${escapeHtml(item)}</li>`).join("")}</ul></details>` : ""}`;
}

function updatePhaseHeading(phase) {
  const heading = document.getElementById("progressTitle");
  if (!heading) return;
  const labels = {
    discover: "正在了解项目…",
    frame: "正在确认任务目标…",
    locate: "正在找到需要处理的位置…",
    implement: "正在完成修改…",
    verify: "正在检查结果…",
    finish: "正在整理结果…",
    analyze: "正在了解项目…",
    summarize: "正在整理结果…",
  };
  heading.textContent = labels[phase] || "正在完成任务…";
}

function renderProgressSteps() {
  const target = document.getElementById("progressSteps");
  if (!target) return;
  target.replaceChildren();
  const visible = state.executionGroups.slice(-6);
  if (!visible.length) {
    target.innerHTML = '<div class="progress-step"><span class="progress-step-icon">·</span><span>准备开始</span></div>';
    return;
  }
  visible.forEach((group) => {
    const item = document.createElement("div");
    item.className = "progress-step";
    const technical = group.details.join("\n");
    item.innerHTML = `<span class="progress-step-icon">${escapeHtml(group.icon)}</span><div><span>${escapeHtml(group.title)}</span>${technical ? `<details><summary>查看详情</summary><pre>${escapeHtml(technical)}</pre></details>` : ""}</div>`;
    target.appendChild(item);
  });
  scrollConversation();
}

function markProgressDone(title = "任务执行结束") {
  const card = document.getElementById("activeProgressCard");
  const heading = document.getElementById("progressTitle");
  if (card) card.classList.add("done");
  if (heading) heading.textContent = title;
}

function markProgressStopping() {
  const heading = document.getElementById("progressTitle");
  if (heading) heading.textContent = "正在安全停止并保存当前进度…";
}

function addMessage(role, content) {
  const normalizedRole = role === "assistant" ? "agent" : "user";
  const wrapper = document.createElement("div");
  wrapper.className = `message ${normalizedRole}`;
  const author = normalizedRole === "agent" ? "Mini Coder" : "你";
  const mark = normalizedRole === "agent" ? "AI" : "你";
  wrapper.innerHTML = `<div class="message-author"><span class="author-dot">${mark}</span><span>${author}</span></div><div class="message-bubble">${escapeHtml(content || "任务已结束。")}</div>`;
  els.messageList.appendChild(wrapper);
  scrollConversation();
}

function finishRun(payload) {
  const completed = payload.result_status === "completed";
  markProgressDone(completed ? "任务已完成" : "任务已停止");
  addMessage("assistant", conversationalFinalText(payload.final_text, completed));
  els.conversationEyebrow.textContent = completed ? "会话已完成" : "会话已停止";
  els.runStatus.textContent = completed ? "已完成" : labelStatus(payload.result_status);
  els.task.value = "";
  els.task.disabled = false;
  els.task.placeholder = "继续告诉 Agent 接下来要做什么…";
  els.runButton.disabled = false;
  els.runButton.innerHTML = "<span>▶</span> 继续执行";
  state.runActive = false;
  state.cancellationRequested = false;
  state.runId = null;
  els.stopRunButton.classList.add("hidden");
  els.stopRunButton.disabled = false;
  els.stopRunButton.textContent = "停止任务";
  state.draft = true;
  els.composerHint.textContent = "可以继续提出修改、追问或新的验收要求";
  renderTurnSummary(completed ? "已完成" : "已停止");
  renderChanges();
  setConnection(completed ? "执行完成" : "已停止");
}

function renderTurnSummary(status = "") {
  const parts = [`第 ${Math.max(1, state.currentTurn)} 轮`];
  if (status) parts.push(status);
  if (state.turnModelCalls) parts.push(`思考 ${state.turnModelCalls} 次`);
  if (state.turnToolCalls) parts.push(`操作 ${state.turnToolCalls} 项`);
  if (state.usedSessionMemory) parts.push("已使用会话记忆，不重放旧工具记录");
  els.turnSummary.textContent = parts.join(" · ");
}

function failRun(message) {
  markProgressDone("执行遇到问题");
  addMessage("assistant", `这次任务没有顺利完成：${friendlyError(message)}`);
  els.conversationEyebrow.textContent = "会话已停止";
  els.runStatus.textContent = "执行失败";
  els.task.disabled = false;
  els.runButton.disabled = false;
  els.runButton.innerHTML = "<span>▶</span> 继续执行";
  state.runActive = false;
  state.cancellationRequested = false;
  state.runId = null;
  els.stopRunButton.classList.add("hidden");
  state.draft = true;
  renderTurnSummary("未完成");
  renderChanges();
  setConnection("发生错误", "error");
}

function showApproval(payload) {
  state.pendingApproval = {...payload, run_id: state.runId};
  const items = Array.isArray(payload.items) && payload.items.length
    ? payload.items
    : [{
        tool: payload.tool || "tool",
        risk: payload.risk || "write",
        arguments: payload.arguments || {},
        description: approvalItemDescription(payload.tool, payload.arguments || {}),
      }];
  const summary = payload.summary || items[0].description || "Agent 想执行一项需要确认的操作";
  const oldMessage = document.getElementById("inlineApprovalMessage");
  if (oldMessage) oldMessage.remove();
  const wrapper = document.createElement("div");
  wrapper.id = "inlineApprovalMessage";
  wrapper.className = "message agent approval-message";
  const visibleItems = items.slice(0, 10);
  const remainder = items.length - visibleItems.length;
  const technical = items.map((item, index) => {
    const label = item.description || approvalItemDescription(item.tool, item.arguments || {});
    return `${index + 1}. ${label}\n${JSON.stringify(item.arguments || {}, null, 2)}`;
  }).join("\n\n");
  wrapper.innerHTML = `
    <div class="message-author"><span class="author-dot">AI</span><span>Mini Coder</span></div>
    <div class="inline-approval-card">
      <div class="inline-approval-header"><strong>需要你的确认</strong><span class="risk-badge">${escapeHtml(friendlyRisk(payload.risk))}</span></div>
      <div class="inline-approval-body"><p>${escapeHtml(summary)}。允许后才会真正执行。</p>
        <ul class="approval-item-list">${visibleItems.map((item) => `<li>${escapeHtml(item.description || approvalItemDescription(item.tool, item.arguments || {}))}</li>`).join("")}${remainder > 0 ? `<li>另外还有 ${remainder} 项同类操作</li>` : ""}</ul>
      </div>
      <details class="inline-technical-details"><summary>查看具体内容</summary><pre>${escapeHtml(technical)}</pre></details>
      <div class="approval-actions"><button class="secondary-button inline-deny" type="button">拒绝</button><button class="primary-button inline-approve" type="button">允许这些操作</button></div>
    </div>`;
  wrapper.querySelector(".inline-deny").addEventListener("click", () => decideApproval(false));
  wrapper.querySelector(".inline-approve").addEventListener("click", () => decideApproval(true));
  els.messageList.appendChild(wrapper);

  const args = payload.arguments || items[0]?.arguments || {};
  const tool = payload.tool || items[0]?.tool || "tool";
  els.approvalRisk.textContent = friendlyRisk(payload.risk);
  els.approvalHeading.textContent = "Agent 需要你的确认";
  if (tool === "edit_file" || tool === "write_file") {
    els.approvalTitle.textContent = `Agent 想修改 ${args.path || "一个项目文件"}。确认后才会真正写入。`;
  } else if (tool === "run_command") {
    els.approvalTitle.textContent = "Agent 想运行一条本地命令，用来检查项目或完成任务。";
  } else {
    els.approvalTitle.textContent = `Agent 想执行“${friendlyToolName(tool)}”。`;
  }
  els.approvalArguments.textContent = technical || JSON.stringify(args, null, 2);
  els.approvalPanel.classList.add("hidden");
  els.runStatus.textContent = "等待你的确认";
  setConnection("等待确认", "busy");
  scrollConversation();
}

function hideApproval(approved, expired = false) {
  state.pendingApproval = null;
  els.approvalPanel.classList.add("hidden");
  const message = document.getElementById("inlineApprovalMessage");
  if (message) {
    const card = message.querySelector(".inline-approval-card");
    if (card) {
      card.classList.add("resolved");
      card.innerHTML = `<div class="inline-approval-result">${expired ? "确认已超时，这批操作没有执行" : approved ? "你已允许这批操作，Agent 正在继续" : "你已拒绝这批操作，文件不会因此被修改"}</div>`;
    }
    message.removeAttribute("id");
  }
  if (expired) toast("确认等待超时，Agent 已停止这项操作。", true);
  else if (approved === false) toast("已拒绝这项操作。 ");
  els.runStatus.textContent = "正在执行";
  setConnection("正在执行", "busy");
}

async function decideApproval(approved) {
  const pending = state.pendingApproval;
  const runId = pending?.run_id || state.runId;
  if (!pending || !runId) return;
  els.approveButton.disabled = true;
  els.denyButton.disabled = true;
  document.querySelectorAll("#inlineApprovalMessage button").forEach((button) => { button.disabled = true; });
  try {
    const response = await fetch(`/api/runs/${runId}/approvals/${pending.approval_id}`, {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({approved}),
    });
    const data = await readJson(response);
    if (!response.ok) throw new Error(data.detail || "无法提交确认结果");
  } catch (error) {
    toast(error.message, true);
  } finally {
    els.approveButton.disabled = false;
    els.denyButton.disabled = false;
    document.querySelectorAll("#inlineApprovalMessage button").forEach((button) => { button.disabled = false; });
  }
}

function hideApprovalCard() {
  state.pendingApproval = null;
  els.approvalPanel.classList.add("hidden");
  const message = document.getElementById("inlineApprovalMessage");
  if (message) message.remove();
}

function approvalItemDescription(tool, args = {}) {
  if (tool === "write_file" || tool === "edit_file") return `修改文件 ${args.path || "项目文件"}`;
  if (tool === "run_command") {
    return args.purpose === "verify" ? `运行项目检查：${args.command || "本地命令"}` : `运行本地命令：${args.command || "未命名命令"}`;
  }
  return friendlyToolName(tool);
}

function rememberChangePreview(payload, timestamp) {
  const key = payload.tool_execution_id || `preview:${payload.path}`;
  state.changes.set(key, {...payload, key, previewOnly: true});
  const action = payload.before_hash == null ? "准备创建" : "准备修改";
  addExecution(`${action} ${payload.path || "项目文件"}`, `预计新增 ${Number(payload.additions || 0)} 行，删除 ${Number(payload.deletions || 0)} 行。`, timestamp, "Δ");
  renderChanges();
}

function rememberAppliedChange(payload) {
  const previewKey = payload.tool_execution_id || `preview:${payload.path}`;
  const preview = state.changes.get(previewKey) || {};
  if (previewKey !== payload.change_id) state.changes.delete(previewKey);
  const key = payload.change_id || previewKey;
  state.changes.set(key, {...preview, ...payload, key, previewOnly: false});
  renderChanges();
}

function renderChanges() {
  const changes = [...state.changes.values()];
  const latestByPath = new Map();
  changes.forEach((item) => latestByPath.set(item.path || item.key, item));
  const visibleChanges = [...latestByPath.values()];
  const fileCount = visibleChanges.length;
  els.changeCount.textContent = String(fileCount);
  const additions = changes.reduce((sum, item) => sum + Number(item.additions || 0), 0);
  const deletions = changes.reduce((sum, item) => sum + Number(item.deletions || 0), 0);
  els.diffStats.textContent = changes.length ? `+${additions} / -${deletions}` : "暂无修改";
  els.changesToggleButton.disabled = !fileCount;
  els.changesToggleButton.setAttribute("aria-expanded", String(Boolean(fileCount && state.changesExpanded)));
  els.changesToggleButton.textContent = `${state.changesExpanded ? "收起" : "展开"} ${fileCount} 个文件`;
  els.changeList.classList.toggle("hidden", !fileCount || !state.changesExpanded);
  els.changeList.replaceChildren();
  if (!changes.length) {
    updateUndoButton();
    return;
  }
  if (!state.changesExpanded) {
    updateUndoButton();
    return;
  }
  visibleChanges.forEach((change) => {
    const row = document.createElement("div");
    row.className = "change-item";
    const openButton = document.createElement("button");
    openButton.type = "button";
    openButton.className = "change-open-button";
    openButton.innerHTML = `
        <span class="change-icon">&lt;/&gt;</span>
        <span><strong>${escapeHtml(change.path || "项目文件")}</strong><small>${change.previewOnly ? "等待确认" : "点击查看完整代码"}</small></span>
        <span class="change-stats">+${Number(change.additions || 0)} / -${Number(change.deletions || 0)}</span>`;
    openButton.addEventListener("click", () => openChange(change));
    row.appendChild(openButton);
    if (state.sessionId && !state.runActive && !change.previewOnly && change.change_id) {
      const undoFileButton = document.createElement("button");
      undoFileButton.type = "button";
      undoFileButton.className = "file-undo-button";
      undoFileButton.textContent = "撤销此文件";
      undoFileButton.addEventListener("click", () => undoFileChange(change, undoFileButton));
      row.appendChild(undoFileButton);
    }
    els.changeList.appendChild(row);
  });
  updateUndoButton();
}

function updateUndoButton() {
  const canUndo = Boolean(
    state.sessionId
    && !state.runActive
    && [...state.changes.values()].some((item) => !item.previewOnly),
  );
  els.undoButton.classList.toggle("hidden", !canUndo);
  els.undoButton.disabled = false;
  els.undoButton.textContent = "撤销上次修改";
}

async function undoLastChange() {
  if (!state.sessionId || state.runActive) return;
  els.undoButton.disabled = true;
  els.undoButton.textContent = "正在撤销…";
  try {
    const response = await fetch(
      `/api/sessions/${encodeURIComponent(state.sessionId)}/undo-last`,
      {method: "POST"},
    );
    const data = await readJson(response);
    if (!response.ok) throw new Error(data.detail || "无法撤销这次修改");
    toast(`已撤销 ${data.path || "上一次文件修改"}`);
    await loadSession(state.sessionId);
  } catch (error) {
    els.undoButton.disabled = false;
    els.undoButton.textContent = "撤销上次修改";
    toast(friendlyError(error.message), true);
  }
}

async function undoFileChange(change, button) {
  if (!state.sessionId || state.runActive || !change.change_id) return;
  button.disabled = true;
  button.textContent = "正在撤销…";
  try {
    const response = await fetch(
      `/api/sessions/${encodeURIComponent(state.sessionId)}/changes/${encodeURIComponent(change.change_id)}/undo`,
      {method: "POST"},
    );
    const data = await readJson(response);
    if (!response.ok) throw new Error(data.detail || "无法撤销这个文件的修改");
    toast(`已撤销 ${data.path || change.path || "这个文件"}`);
    await loadSession(state.sessionId);
  } catch (error) {
    button.disabled = false;
    button.textContent = "撤销此文件";
    toast(friendlyError(error.message), true);
  }
}

async function openChange(change) {
  try {
    let detail;
    if (change.change_id && state.sessionId && !change.previewOnly) {
      const response = await fetch(`/api/sessions/${encodeURIComponent(state.sessionId)}/changes/${encodeURIComponent(change.change_id)}`);
      detail = await readJson(response);
      if (!response.ok) throw new Error(detail.detail || "无法读取完整代码");
    } else {
      detail = {
        path: change.path,
        diff: change.diff || "",
        before: null,
        after: null,
        additions: change.additions || 0,
        deletions: change.deletions || 0,
        matches_agent_version: true,
      };
    }
    state.codeDetail = detail;
    state.codeTab = "diff";
    els.codeDialogTitle.textContent = detail.path || "代码变更";
    els.codeVersionWarning.classList.toggle("hidden", detail.matches_agent_version !== false);
    els.codeModal.classList.remove("hidden");
    document.body.classList.add("modal-open");
    selectCodeTab("diff");
  } catch (error) {
    toast(error.message, true);
  }
}

function selectCodeTab(tab) {
  const detail = state.codeDetail;
  if (!detail) return;
  if ((tab === "after" && detail.after === null) || (tab === "before" && detail.before === null)) {
    toast("这项修改尚未写入，目前只能查看修改对比。 ");
    tab = "diff";
  }
  state.codeTab = tab;
  const buttons = {diff: els.diffTabButton, after: els.afterTabButton, before: els.beforeTabButton};
  Object.entries(buttons).forEach(([name, button]) => button.classList.toggle("active", name === tab));
  els.afterTabButton.disabled = detail.after === null;
  els.beforeTabButton.disabled = detail.before === null;
  const content = tab === "diff" ? detail.diff : tab === "after" ? detail.after : detail.before;
  renderCode(content || "", tab === "diff");
  els.fullCodeStats.textContent = `新增 ${Number(detail.additions || 0)} 行 · 删除 ${Number(detail.deletions || 0)} 行`;
}

function renderCode(content, isDiff) {
  const code = document.createElement("code");
  String(content).split("\n").forEach((line) => {
    const span = document.createElement("span");
    span.className = `code-line${isDiff ? ` ${diffClass(line)}` : ""}`;
    span.textContent = line || " ";
    code.appendChild(span);
  });
  els.fullCodeView.replaceChildren(code);
  els.fullCodeView.scrollTop = 0;
  els.fullCodeView.scrollLeft = 0;
}

function closeCodeModal() {
  els.codeModal.classList.add("hidden");
  state.codeDetail = null;
  if (els.folderModal.classList.contains("hidden") && els.sessionModal.classList.contains("hidden")) {
    document.body.classList.remove("modal-open");
  }
}

function openFolderBrowser() {
  els.folderModal.classList.remove("hidden");
  document.body.classList.add("modal-open");
  loadDirectory(els.workspace.value.trim() || state.workspace || null);
}

function closeFolderBrowser() {
  els.folderModal.classList.add("hidden");
  if (els.sessionModal.classList.contains("hidden") && els.codeModal.classList.contains("hidden")) {
    document.body.classList.remove("modal-open");
  }
}

function chooseCurrentFolder() {
  if (!state.folderPath) return;
  els.workspace.value = state.folderPath;
  closeFolderBrowser();
  toast("已选择工作文件夹");
}

async function loadDirectory(path) {
  els.folderList.classList.add("folder-loading");
  els.folderList.innerHTML = '<div class="folder-empty">正在读取文件夹…</div>';
  els.folderChooseButton.disabled = true;
  try {
    const query = path ? `?path=${encodeURIComponent(path)}` : "";
    const response = await fetch(`/api/directories${query}`);
    const data = await readJson(response);
    if (!response.ok) throw new Error(data.detail || "无法读取这个文件夹");
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
    button.className = `root-button${String(current).toLowerCase().startsWith(String(root).toLowerCase()) ? " active" : ""}`;
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
    button.innerHTML = `<span class="folder-icon">▰</span><span>${escapeHtml(directory.name)}</span>`;
    button.addEventListener("click", () => loadDirectory(directory.path));
    els.folderList.appendChild(button);
  });
}

function setVerificationFromSession(status, records) {
  const latest = records.length ? records[records.length - 1] : null;
  if (status === "passed") setVerification("passed", "已通过", latest ? friendlyVerification(latest) : "项目检查已通过。 ");
  else if (status === "failed") setVerification("failed", "未通过", latest ? friendlyVerification(latest) : "项目检查未通过。 ");
  else if (status === "stale") setVerification("neutral", "需要重跑", "检查完成后代码又发生了变化。 ");
  else if (status === "unverified") setVerification("neutral", "未验证", "修改尚未通过本地检查。 ");
  else setVerification("neutral", "无需验证", "这次会话没有需要运行的项目检查。 ");
}

function setVerification(tone, label, message) {
  els.verificationBadge.className = `verification-badge ${tone}`;
  els.verificationBadge.textContent = label;
  els.verificationStatus.textContent = label;
  els.verificationMessage.textContent = message;
}

async function refreshSnapshot(runId, refreshOpenSession = false) {
  try {
    const response = await fetch(`/api/runs/${runId}`);
    const data = await readJson(response);
    if (!response.ok) return;
    const sessionId = data.result?.session_id || data.session_id || state.sessionId;
    if (sessionId && refreshOpenSession) state.sessionId = sessionId;
    await syncActiveRuns();
    await loadSessions({silent: true});
    if (refreshOpenSession && sessionId && state.sessionId === sessionId) {
      await loadSession(sessionId);
    }
  } catch (_) {
    // The event stream already delivered the user-facing result.
  }
}

function setConnection(label, tone = "") {
  els.connectionLabel.textContent = label;
  els.connectionDot.className = `connection-dot${tone ? ` ${tone}` : ""}`;
}

function friendlyToolName(tool) {
  const names = {
    read_file: "读取文件",
    list_files: "查看文件列表",
    search_text: "搜索项目内容",
    edit_file: "编辑文件",
    write_file: "写入文件",
    run_command: "运行本地命令",
  };
  return names[tool] || String(tool || "工具").replaceAll("_", " ");
}

function technicalToolDetail(tool, args) {
  if (tool === "run_command") return args.command || "";
  if (args.path) return `${friendlyToolName(tool)}：${args.path}`;
  return JSON.stringify(args || {}, null, 2);
}

function friendlyRisk(risk) {
  const risks = {read: "只读", write: "写入", execute: "运行", elevated: "高风险", dangerous: "高风险"};
  return risks[String(risk || "write").toLowerCase()] || "需确认";
}

function overviewDetail(payload) {
  const files = [...(payload.manifests || []), ...(payload.entry_points || []), ...(payload.test_paths || [])];
  return files.length ? `发现：${files.slice(0, 6).join("、")}` : "已查看主要目录和项目入口";
}

function taskBriefDetail(payload) {
  const intentNames = {build: "从零构建", fix: "定位并修复问题", feature: "增加功能", improve: "改进现有项目", explain: "分析并解释"};
  const assumptions = Array.isArray(payload.assumptions) ? payload.assumptions : [];
  return `${intentNames[payload.intent] || "完成任务"}${assumptions.length ? `\n默认：${assumptions.slice(0, 3).join("；")}` : ""}`;
}

function verificationDetail(payload) {
  const duration = Number(payload.duration_seconds || 0).toFixed(2);
  const expected = Array.isArray(payload.expected_exit_codes) ? payload.expected_exit_codes.join("/") : "0";
  return `${payload.command || "项目检查"}\n退出码：${payload.exit_code ?? "?"} · 预期：${expected} · 用时：${duration} 秒`;
}

function friendlyVerification(payload) {
  const duration = Number(payload.duration_seconds || 0).toFixed(1);
  if (payload.verification_mode === "expected_rejection" && payload.check_passed) {
    return `无效输入被正确拒绝了；这是一项补充检查，还需要正常运行检查才能完成验收（${duration} 秒）。`;
  }
  if (payload.environment_error) {
    return "检查环境或依赖没有准备好，因此不能把这次运行算作通过。";
  }
  return payload.passed
    ? `检查成功完成（${duration} 秒）。`
    : `检查结果不符合预期（退出码 ${payload.exit_code ?? "?"}，${duration} 秒）。`;
}

function friendlyFinalText(text) {
  let result = String(text || "").trim();
  for (const marker of ["\n\nOutcome:", "\n\nLocal change summary:"]) {
    if (result.includes(marker)) result = result.split(marker, 1)[0].trim();
  }
  return result || "任务已经结束。 ";
}

function conversationalFinalText(raw, completed) {
  if (!completed) {
    const friendly = friendlyFinalText(raw || "任务没有完成。 ");
    const firstLine = friendly.split("\n", 1)[0].replace(/[`#*_]/g, "").trim();
    return `这次任务还没有完全完成。\n\n${firstLine}`;
  }
  const changes = [...state.changes.values()].filter((item) => !item.previewOnly);
  const paths = [...new Set(changes.map((item) => item.path).filter(Boolean))];
  if (paths.length) {
    const created = changes.every((item) => item.before_hash == null);
    const verb = created ? "创建了" : "创建或修改了";
    const paragraphs = ["已经完成了。", `我${verb} ${joinChinese(paths)}。`];
    if (els.verificationStatus.textContent === "已通过") {
      paragraphs[1] = `${paragraphs[1].slice(0, -1)}，也运行了项目测试，结果全部通过。`;
    } else if (els.verificationStatus.textContent === "未通过") {
      paragraphs.push("项目测试还没有通过，具体情况可以在右侧查看。 ");
    } else {
      paragraphs.push("这次修改还没有经过完整的项目测试。 ");
    }
    paragraphs.push("想看具体内容的话，可以点击右侧的文件名查看完整改动。 ");
    return paragraphs.join("\n\n");
  }
  return cleanModelReply(raw) || "已经完成了。 ";
}

function cleanModelReply(text) {
  let result = friendlyFinalText(text || "");
  result = result.replace(/```[\s\S]*?```/g, "");
  result = result.replace(/`([^`]+)`/g, "$1");
  result = result.replace(/^[-*]\s+/gm, "");
  result = result.replace(/\n{3,}/g, "\n\n").trim();
  return result;
}

function joinChinese(values) {
  if (values.length <= 1) return values[0] || "";
  return `${values.slice(0, -1).join("、")} 和 ${values[values.length - 1]}`;
}

function friendlyError(message) {
  const text = String(message || "发生未知问题");
  if (text.includes("session has no active file change")) return "这个会话没有可以撤销的文件修改。";
  if (text.includes("Cannot undo") && text.includes("file changed after")) return "这个文件后来又被修改过，为了保护你的内容，Agent 没有覆盖它。";
  if (text.includes("API") || text.includes("model")) return "模型服务暂时无法完成请求，请检查配置后重试。";
  return text;
}

function labelStatus(status) {
  const labels = {
    created: "已创建",
    running: "正在执行",
    waiting_for_approval: "等待确认",
    cancelling: "正在停止",
    completed: "已完成",
    completed_verified: "已完成",
    completed_unverified: "已完成（未验证）",
    verification_failed: "验证未通过",
    failed: "执行失败",
    denied: "已拒绝",
    interrupted: "已中断",
    cancelled: "已取消",
  };
  return labels[String(status || "")] || String(status || "未知状态").replaceAll("_", " ");
}

function sessionIcon(status) {
  if (String(status).startsWith("completed")) return "✓";
  if (status === "running" || status === "waiting_for_approval") return "●";
  if (status === "failed" || status === "verification_failed") return "!";
  return "○";
}

function sessionTime(value) {
  try {
    const date = new Date(value);
    const now = new Date();
    if (date.toDateString() === now.toDateString()) return date.toLocaleTimeString([], {hour: "2-digit", minute: "2-digit"});
    return date.toLocaleDateString([], {month: "2-digit", day: "2-digit"});
  } catch (_) { return ""; }
}

function suggestedTitle(task) {
  const compact = String(task || "").replace(/\s+/g, " ").trim();
  return compact.length > 28 ? `${compact.slice(0, 28)}…` : compact;
}

function pathName(path) {
  const normalized = String(path || "").replace(/[\\/]+$/, "");
  return normalized.split(/[\\/]/).pop() || normalized;
}

function joinPath(root, child) {
  const separator = String(root).includes("\\") ? "\\" : "/";
  return `${String(root).replace(/[\\/]+$/, "")}${separator}${child}`;
}

function samePath(left, right) {
  return String(left || "").replace(/[\\/]+$/, "").toLowerCase()
    === String(right || "").replace(/[\\/]+$/, "").toLowerCase();
}

function diffClass(line) {
  if (line.startsWith("+++") || line.startsWith("---")) return "header";
  if (line.startsWith("+")) return "add";
  if (line.startsWith("-")) return "remove";
  if (line.startsWith("@@")) return "hunk";
  return "";
}

function formatTime(value) {
  try { return new Date(value).toLocaleTimeString([], {hour: "2-digit", minute: "2-digit"}); }
  catch (_) { return ""; }
}

function scrollConversation() {
  window.requestAnimationFrame(() => { els.conversation.scrollTop = els.conversation.scrollHeight; });
}

function escapeHtml(value) {
  const node = document.createElement("span");
  node.textContent = String(value ?? "");
  return node.innerHTML;
}

async function readJson(response) {
  try { return await response.json(); }
  catch (_) { return {}; }
}

let toastTimer = null;
function toast(message, error = false) {
  window.clearTimeout(toastTimer);
  els.toast.textContent = message;
  els.toast.className = `toast show${error ? " error" : ""}`;
  toastTimer = window.setTimeout(() => { els.toast.className = "toast"; }, 3400);
}
