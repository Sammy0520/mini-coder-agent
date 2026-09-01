# Mini Coder 工具与模型重叠执行设计

状态：P0、P1 首版已实现；P1 默认关闭，等待稳定 API 环境重新 A/B
范围：P0 只读工具并行；P1 最终验证与推测收尾重叠  
非目标：通用异步 Agent 框架、任意工具 Future、推理服务器 KV cache 暂停/恢复

## 1. 背景与目标

Mini Coder 当前采用同步的模型—工具循环：模型返回一批工具调用后，主循环逐项执行；全部结果写回消息后，才发起下一次模型请求。这种顺序具有明确的审批、会话和变更语义，但没有利用独立 I/O 之间的等待时间。

本设计增加两种有界 overlap：

1. **P0：同一模型响应中的纯只读工具并行。** 不增加模型调用，只缩短多个独立本地观察的墙钟时间。
2. **P1：最终本地验证与候选交付说明推测生成并行。** 在正确性仍由真实验证决定的前提下，隐藏一部分最终等待时间。

设计目标：

- 不改变工作区写入、审批、Diff、Undo 和 verification gate 的安全语义。
- 不增加开放式 Agent 树或后台任务轮询。
- 所有结果按确定顺序持久化并可审计。
- 推测失败时能够无副作用地丢弃。
- 单独统计 overlap 带来的延迟收益和额外 Token，证明而非假设其价值。

## 2. 当前边界

在标准 OpenAI-compatible Responses/Chat Completions API 中，已经发送的模型请求不能在生成途中插入稍后返回的工具结果。因此应用层无法实现推理引擎级的“暂停 sequence—插入工具 token—复用同一 KV cache 继续生成”。

本设计属于 Agent runtime 层：

- P0 在下一次模型请求前并行完成独立工具。
- P1 启动一个独立、只产出候选文本的推测请求，并在真实验证完成后进行 commit/abort。

P1 候选请求不是父会话的权威续写，只有提交条件全部成立后才进入最终会话结果。

## 3. P0：纯只读工具并行

### 3.1 语义

模型一次返回的多个工具调用按原始顺序分成若干执行段：

```text
read A ─┐
read B ─┼─ parallel segment 1 ─┐
search C┘                      │
                               ├─ ordered commit
write D ─── serial barrier ────┤
                               │
read E ─┐                      │
search F┴─ parallel segment 2 ─┘
```

只有连续的 `parallel_safe` 工具进入同一并行段。写入、命令、审批、未知工具和 Subagent 协调器都是屏障。

### 3.2 首版允许并行的工具

- `list_files`
- `read_file`
- `search_text`

首版明确不并行：

- `write_file`
- `edit_file`
- `run_command`
- `delegate_subagents`
- `apply_subagent_patches`
- 以后新增但没有显式声明 `parallel_safe` 的工具

风险等级 `READ` 不能自动等同于 `parallel_safe`。例如 `delegate_subagents` 对真实工作区只读，但会启动模型、持有协调器状态并长时间运行，不能被通用只读调度器处理。

### 3.3 工具能力声明

在 `Tool` 基类增加：

```python
parallel_safe: bool = False
```

三个内置观察工具显式设为 `True`。默认值必须是 `False`，确保新增工具不会未经审计自动并行。

### 3.4 执行模型

- 单个并行段最多两个工具同时执行。
- 使用局部 `ThreadPoolExecutor(max_workers=2)` 或由 Runner 复用的有界执行器。
- 工具线程只执行 `registry.execute()`，不直接修改父 Session、消息、预算或事件序列。
- 每个线程使用独立的临时观察上下文；共享工作区策略、取消信号、输出上限和运行目录，不共享可变的读取/搜索缓存。
- 主线程等待整段完成后，严格按原 tool-call 顺序：
  1. 更新 ToolExecutionRecord；
  2. 追加 tool message；
  3. 累计输出预算；
  4. 发出完成/失败事件；
  5. 原子保存 Session。

这样并发完成顺序不会改变模型可见的消息顺序。

### 3.5 缓存策略

当前 `ToolContext.read_cache` 和 `search_cache` 是普通字典，不支持并发的检查—更新事务。P0 首版不在工具线程之间共享观察缓存。

理由：

- 同一批并行调用应当彼此独立，重复读取本来就不应被并行调度。
- 避免为三个小工具给整个缓存实现加细粒度锁。
- 工具结果已经进入模型历史；同一轮不依赖并发缓存复用。

段结束后不强行合并临时缓存。若后续数据表明观察缓存收益明显受损，再增加只在主线程提交的缓存 delta 协议。

### 3.6 审批和写屏障

- P0 只处理无需审批的纯读取。
- 一旦遇到写入、命令或需要审批的工具，先 join 之前的读取段。
- 写入完成并使观察缓存失效后，才能启动后续读取段。
- 不允许读取与真实工作区写入 overlap，避免读取到不确定 revision。

### 3.7 取消与错误

- 所有工具接收相同的父任务取消信号。
- 一个读取失败不取消同段其他读取。
- join 后分别记录成功或失败，不把整段压成一个结果。
- 父任务取消后不启动新段；正在运行的读取协作退出，无法即时退出的短读取等待返回。

### 3.8 事件与指标

新增事件：

```text
parallel_tool_batch_started
parallel_tool_batch_completed
```

公开字段：

```json
{
  "batch_id": "...",
  "tool_count": 2,
  "tools": ["read_file", "search_text"],
  "duration_seconds": 0.18,
  "serial_duration_seconds": 0.31,
  "overlapped_seconds": 0.13
}
```

其中 `serial_duration_seconds` 是各工具实际耗时之和，不是预测值；`overlapped_seconds = max(0, serial_duration - wall_duration)`。

Session 统计增加：

- `parallel_tool_batches`
- `parallel_tool_calls`
- `parallel_tool_overlap_seconds`
- `parallel_tool_peak_concurrency`

### 3.9 P0 验收测试

1. 两个阻塞 Fake ReadTool 通过 Barrier 证明同时运行，峰值为 2。
2. 三个读取在并发上限 2 下分两波执行。
3. 结果完成顺序相反时，模型消息仍保持请求顺序。
4. `read/read/write/read/read` 被切成两个并行段和一个串行屏障。
5. `run_command`、`delegate_subagents` 即使风险为 READ 也不进入 P0。
6. 一个读取失败不影响同段另一个结果。
7. 取消能阻止新段并保持 Session 可恢复。
8. 工具调用与输出预算和串行模式口径一致。
9. Windows、Linux 上都不依赖事件循环实现。
10. 原有 Observation Cache、审批和重复调用测试不回归。

## 4. P1：最终验证与推测收尾重叠

### 4.1 核心思路

顺序流程：

```text
final verification ── wait ── final model response
```

P1 流程：

```text
                         ┌─ local verification future ───────────┐
changes ready ── fork ───┤                                       ├─ validate ─ commit/abort
                         └─ speculative finalizer model future ──┘
```

候选回答只在以下事实全部成立时提交：

- 最终验证通过；
- 不是 environment error；
- 验证覆盖当前任务所需范围；
- 验证开始后的工作区 change revision 没有变化；
- 没有 unresolved issue；
- 候选输出协议合法且没有工具调用；
- 父任务没有取消或超出预算。

否则候选回答被丢弃，真实验证结果进入普通 Agent 循环。

### 4.2 为什么只做最终验证

最终验证具有较高成功概率，而且候选输出只是文本。推测失败不会修改文件、运行命令或产生外部副作用。相比让完整主 Agent 在任意长工具期间继续行动，这个切入点更安全、可度量且实现较小。

首版不对以下场景推测：

- 定位阶段的搜索或命令；
- 会影响下一步修改策略的测试；
- 安装、联网、发布或危险命令；
- 当前已有失败验证或 unresolved issue；
- 分析/解释类不需要验证的任务；
- 验证前仍存在待应用补丁；
- 同一轮已经发生过一次失败推测。

### 4.3 启动条件

P1 同时满足以下条件才启用：

1. `speculative_finish_enabled = true`。
2. Session 位于 `verify` 或明确的最终收尾阶段。
3. 至少存在一个当前 Agent 管理的修改。
4. 待运行命令被 VerificationTracker 识别为正常成功型最终验证。
5. 当前没有未解决问题、pending approval、pending Subagent bundle。
6. 当前变更 revision 被冻结记录。
7. 验证命令启动后经过 grace period 仍未结束。
8. 本轮尚未使用 speculative finish。

默认 grace period 建议 800 ms。快速语法检查和小型单测通常会在此之前结束，不值得额外发模型请求。

### 4.4 候选请求

候选请求使用独立模型客户端，不能复用正在运行的主模型客户端实例。它使用固定、短小、缓存稳定的 system/developer 前缀，并且工具定义为空。

动态上下文只包括：

- 用户目标的紧凑摘要；
- TaskLedger 当前意图和完成条件；
- changed files 和每个文件的一句话作用；
- 已知限制；
- 明确声明“最终验证仍在运行，不得声称已经通过”。

不发送：

- 完整旧对话；
- 文件全文；
- 原始工具日志；
- Diff 正文；
- API key；
- 未完成验证的推测结果。

候选模型只允许返回短文本，建议最大 300 output tokens，默认 reasoning effort 为 `low`、verbosity 为 `low`。主 Agent 仍可保持用户指定的 `sol-high`。

### 4.5 事务和 revision 校验

启动时记录：

```python
SpeculationSnapshot(
    speculation_id,
    session_id,
    turn,
    change_revision,
    changed_files,
    verification_command,
    started_at,
)
```

提交条件至少要求：

```text
snapshot.change_revision == session.change_revision
```

首版不实现路径级 MVCC。即使后续修改看似不相关，只要 revision 改变就丢弃候选与旧验证，沿用现有 verification invalidation 规则。

### 4.6 协议提交

候选模型请求是旁路计算，不直接向父 Session 追加 provider item。真实工具完成后，先按正常协议记录 `function_call_output` 和 VerificationRecord。

若候选被接受：

- 将候选文本作为最终 assistant 内容提交；
- Session 记录它来自 `speculative_finalizer`；
- 最终状态仍由 `_completion_outcome()` 和 VerificationTracker 决定；
- 事件中记录候选的 provider usage 和接受依据。

若候选被拒绝：

- 候选正文不写入用户对话；
- 只保留脱敏后的统计、拒绝原因和 usage；
- 主 Agent 使用真实工具结果继续下一轮。

### 4.7 失败与取消

- finalizer 模型失败不影响真实验证；退化为当前顺序流程。
- 首版会等待已经发出的候选请求返回，以完整记录 provider usage；验证失败后候选仍必定丢弃。后续只有在客户端提供可靠取消与 usage 回传语义时才增加主动取消。
- 用户取消继续传播给验证进程树；候选请求没有写入、工具或提交能力，即使底层兼容客户端无法中途取消，也不能改变工作区或最终状态。
- 应用退出时，正在运行的本地命令按现有规则变为 uncertain/interrupted；旁路候选永远不能在恢复后自行提交。
- 恢复 Session 时所有 `running` speculation 统一迁移为 `aborted`，重新走正常验证。

### 4.8 成本与启发式

顺序成功路径：

```text
latency = verification_time + final_model_time
```

推测成功路径：

```text
latency = max(verification_time, finalizer_time)
saved = min(verification_time, finalizer_time)
```

失败路径会浪费一次短 finalizer 请求。因此启用条件应偏保守，并保存以下指标：

- `attempts`
- `accepted`
- `discarded`
- `input_tokens`
- `output_tokens`
- `cached_tokens`
- `reasoning_tokens`
- `model_seconds`
- `overlapped_seconds`
- `critical_path_seconds_saved`
- `last_discard_reason`

只有 provider 明确返回的 Token 才作为精确数据；缺失字段保持 unknown。

### 4.9 配置建议

```toml
parallel_read_tools_enabled = true

speculative_finish_enabled = false
speculative_finish_delay_ms = 800
speculative_finish_reasoning_effort = "low"
```

P0 的并发上限在首版固定为 2。P1 第一版默认关闭；离线 commit/abort 测试已经完成，但必须等兼容 API 恢复稳定后重新进行真实 A/B，再决定是否默认开启。

### 4.10 P1 验收测试

1. 长 Fake Verification 和 Fake Finalizer 通过 Barrier 证明 overlap。
2. 快速验证在 grace period 内完成，不启动候选请求。
3. 验证通过、revision 未变时接受候选，并比顺序路径缩短墙钟时间。
4. 验证失败时丢弃候选并继续主循环。
5. environment error 不得接受候选。
6. 推测期间 revision 变化时，即使验证返回 0 也必须丢弃。
7. 候选返回工具调用或声称未经确认的通过状态时拒绝。
8. finalizer 模型错误时无损退化到顺序模式。
9. 用户取消同时终止/忽略两条支路，Session 可恢复。
10. accepted/discarded 两种路径的 usage 都计入父任务总成本。
11. 推测正文在 abort 路径中不出现在用户对话和 GUI。
12. Windows 进程树清理和 Linux 进程组清理保持通过。

## 5. 实现顺序

### 阶段 P0

1. 增加 `parallel_safe` 工具能力标记。
2. 实现纯函数式的调用分段器并做单元测试。
3. 实现独立观察上下文和有界执行器。
4. 将执行结果在主线程按原顺序提交。
5. 增加事件、指标和完整回归。
6. 用含多个读取的非 GUI 真实任务比较串行/并行墙钟，不比较功能能力。

### 阶段 P1

1. 抽出可后台等待的最终验证执行句柄，但不向模型暴露通用 Future 工具。
2. 增加 SpeculationSnapshot、状态和指标。
3. 增加只返回文本的独立 finalizer client。
4. 实现 grace period、启动条件和 commit/abort gate。
5. 接入 Session、事件、预算和 GUI 简要状态。
6. 完成确定性成功/失败/revision/取消测试。
7. 用固定任务进行顺序与推测 A/B，统计接受率、Token 和关键路径收益。

## 6. 明确不做

首版不实现：

- 任意 `start_tool` / `poll_tool` / `await_tool` 暴露给模型；
- 模型自行轮询后台任务；
- 多轮推测 Agent；
- 推测路径中的写入或命令调用；
- 通用依赖 DAG；
- 路径级 MVCC；
- 推理服务器 KV cache pause/resume；
- 为追求 overlap 而增加 Subagent 数量。

如果 P0/P1 的真实指标不能证明延迟收益，功能应保持可关闭，而不是为了展示异步而长期增加复杂度。

## 7. 对外说明建议

可以将这套设计概括为：

> Mini Coder 使用分层的有界并行：本地独立观察在同一轮内并行执行，不增加模型调用；复杂且互不重叠的工作才交给最多两个隔离 Subagent；任务收尾时，最终验证可以与无工具的候选交付说明推测生成重叠。所有写入只有一个提交点，所有推测都必须通过真实验证与工作区 revision 校验后才能提交。
