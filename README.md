# Mini Coder Agent

一个不依赖 Agent 框架、核心逻辑可检查的命令行编程智能体。模型负责选择下一步，本项目自行负责对话历史、上下文裁剪、工具定义与本地执行、响应解析、审批、循环终止和错误处理。

## 当前能力

- `ModelClient`：与厂商无关的模型边界。
- `OpenAICompatibleClient`：首个实现，可按 provider 配置选择 Responses 或 Chat Completions，可连接 OpenAI 或兼容服务。
- 本地工具：`list_files`、`read_file`、`search_text`、`write_file`、`edit_file`、`run_command`。
- 安全控制：工作区路径限制、常见敏感文件过滤、写入防误覆盖、精确文本替换、命令风险分级、子进程树清理和统一脱敏。
- 跨平台执行：向模型说明实际操作系统与默认 shell，文件搜索和修改优先使用内置工具；子命令中的 `python`/`pip` 默认跟随启动 Agent 的虚拟环境。
- 运行控制：最大步骤、总时间、模型/工具调用、单次/累计工具输出和 provider token 预算；连续重复调用检测、上下文压缩与可恢复中断。
- Session：每次 CLI 运行原子保存版本化 Session；支持 `--resume`，保留 Responses provider items、工具执行状态、审批结果和累计 usage，并阻止不确定副作用被自动重放。
- ChangeTracker：写入前生成 unified diff 和 hash 检查，成功修改保存快照与有序历史；支持冲突安全的 Session 级 Undo。
- 验证闭环：Session 记录 `analyze`、`implement`、`verify`、`summarize` 阶段以及真实验证命令、退出码、耗时和输出摘要；最终状态由本地事实决定。
- 错误恢复：认证、权限、限流、超时、网络、服务端、请求和响应解析错误分类；只对可恢复错误做带抖动和硬上限的有限重试。
- 权限模式：默认 `safe`；命令按 `read_only`、`workspace_write`、`external_effect`、`dangerous`、`unknown` 分级，`--auto` 也不会自动批准后三类。

## 架构

```text
CLI
 └─ AgentRunner                 本地实现循环、停止条件、审批与历史
     ├─ ModelClient             可替换的模型抽象
     │   └─ OpenAICompatibleClient
     ├─ ContextManager          本地限制发送给模型的上下文
     ├─ ChangeTracker           Diff、hash、原子写入、冲突检测与 Undo
     ├─ VerificationTracker     验证命令、修改版本、失效规则与完成判定
     └─ ToolRegistry            本地校验和分发工具
         ├─ filesystem/search   本地读写和检索
         └─ command             本地进程执行
```

模型只能看到 AgentRunner 主动发送的消息，不能直接访问磁盘。模型返回 function tool call 后，由 ToolRegistry 在本机执行，再把结果放回下一轮。Responses 模式会在本地保留并重放上一轮全部 output 项和对应的 `function_call_output`；Chat Completions 模式使用标准 `assistant`/`tool` 消息。项目不使用 OpenAI Agents SDK、LangChain、AutoGen 等 Agent 框架，也不调用托管的 Code Interpreter 或 Files 工具。

## 安装

需要 Python 3.11 或更新版本：

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -e .
```

Linux/macOS 激活命令为：

```bash
source .venv/bin/activate
```

## 配置

API key 不得写进仓库。项目支持 provider TOML，当前 `agent.toml` 已按 aicode007 配置；`agent.toml.example` 是可复制的不含凭据示例：

```toml
model_provider = "aicode007"
model = "gpt-5.6-sol"
model_reasoning_effort = "xhigh"
model_verbosity = "high"

[model_providers.aicode007]
name = "aicode007"
base_url = "https://api.aicode007.com"
wire_api = "responses"
requires_openai_auth = true
```

`base_url` 必须是普通 URL 字符串，不能写成 Markdown 的 `[链接](链接)` 形式。`requires_openai_auth = true` 表示启动时必须获得 key；TOML 本身不保存 key。

开发期可以运行一次本地凭据设置脚本。它会隐藏键盘输入，并把 key 写入与 `agent.toml` 同目录的 `auth.json`：

```powershell
& ".\scripts\set-local-api-key.ps1"
```

`auth.json` 已被 `.gitignore` 排除，之后启动 `mini-coder` 会自动读取，不必在每个新终端重复设置。这个文件仍然包含明文密钥，只适用于可信的个人开发电脑，不要发送给他人、同步到网盘或强制加入 Git。提交前删除它：

```powershell
Remove-Item -LiteralPath ".\auth.json"
```

如需只在当前终端临时覆盖本地凭据：

```powershell
$env:OPENAI_API_KEY = "..."
```

也可以完全通过环境变量配置：

```powershell
$env:CODING_AGENT_API_KEY = "..."
$env:CODING_AGENT_BASE_URL = "https://api.openai.com/v1"
$env:CODING_AGENT_MODEL = "支持 function calling 的模型名"
$env:CODING_AGENT_WIRE_API = "responses"
$env:CODING_AGENT_REASONING_EFFORT = "xhigh"
$env:CODING_AGENT_VERBOSITY = "high"
```

命令行覆盖项优先于环境变量，API key 环境变量优先于本地 `auth.json`，其他环境变量优先于 TOML。`CODING_AGENT_BASE_URL` 可省略，此时客户端使用其默认服务地址。对本地兼容服务，也要把 API key 设置成服务端接受的值。

其他可选变量见 `.env.example`。

## 运行

默认安全模式：

```powershell
mini-coder --config agent.toml --workspace D:\path\to\project "修复失败的测试，并说明原因"
```

受控演示目录中自动批准写入与命令：

```powershell
mini-coder --config agent.toml --auto --workspace examples\demo_project "修复折扣计算的边界错误并运行测试"
```

可通过 `--log agent-events.jsonl` 保存可选的本地 JSONL 事件日志。每项事件包含 schema 版本、UTC 时间、run/session ID、step 和运行时长。日志写入失败只产生可见警告，不会中断主任务。事件内容会经过统一凭据脱敏，但仍可能含有代码或命令输出，因此默认关闭，也不应提交。

`run_command` 会把启动 `mini-coder` 的 Python 环境放在子进程 `PATH` 最前面。因此从 `.venv` 启动时，模型执行普通的 `python` 或 `python -m pip` 也会使用同一个 `.venv`，而不是意外落到系统 Python 或 Anaconda。模型服务所用的 `OPENAI_API_KEY` 与 `CODING_AGENT_API_KEY` 不会自动传给项目子进程。命令超时或用户按下 Ctrl+C 时，Runner 会终止它启动的进程组/进程树，并把退出码、超时、耗时和输出截断状态保存到 Session。

## 错误恢复与运行预算

客户端关闭 SDK 内部的隐式重试，由 Runner 统一决定是否恢复：网络连接、超时、HTTP 429 和 500/502/503/504 可以有限重试；HTTP 400/401/403 不重试；响应解析错误最多重试一次。429 的 `Retry-After` 支持秒数和 HTTP 日期，指数退避带随机抖动，最终等待时间受硬上限约束。错误分类和诊断写入前会先脱敏。

下面的上限都可以通过 CLI 覆盖：

```powershell
mini-coder --config agent.toml --workspace D:\path\to\project `
  --max-steps 20 --max-seconds 900 `
  --max-model-calls 40 --max-tool-calls 100 `
  --max-tool-output 12000 --max-total-tool-output 200000 `
  --max-total-tokens 500000 --max-retries 2 `
  "修复失败的测试并验证"
```

`--max-tool-output` 限制单次送回模型的工具结果，`--max-total-tool-output` 限制整个 Session 累计工具结果。`--max-total-tokens` 只使用 provider 明确返回的 usage；缺少 usage 时报告 `unknown` 或 `partial`，不会自行估算成精确数字。达到任一预算后 Session 进入 `interrupted`，保存停止原因和待执行工具，用户可以用更高上限 `--resume` 继续。

命令风险分类是保守的本地策略，不是通用 shell 静态分析或操作系统沙箱。已知只读命令可直接执行；`safe` 模式仍会确认写工作区的命令；`--auto` 仅可自动批准已知 `read_only` 和 `workspace_write`。安装依赖、联网、Git 远端操作等 `external_effect`，删除/覆盖等 `dangerous`，以及无法理解的 `unknown` 命令始终要求人工确认。审批提示会显示命令、工作目录、风险等级和预期副作用。

## Session 持久化与恢复

CLI 运行开始后会在工作区内创建版本化 Session：

```text
<workspace>/.mini-coder/sessions/<session-id>.json
```

`.mini-coder/` 已被本仓库的 `.gitignore` 排除。Session 不保存 API Key，但可能包含任务文本、代码片段、模型消息和命令输出，因此仍应作为本地敏感开发数据处理，不要直接提交或分享未经检查的 Session 文件。

Session 文件通过同目录临时文件和原子替换写入；在支持 POSIX 权限的平台上，临时文件由操作系统以仅当前用户可访问的方式创建。Windows 上文件继承工作区目录的 ACL，因此不要把 Session 放在其他账号可读的共享目录中，必要时应由用户收紧该目录权限。

CLI 会在运行开始时显示 Session ID 和文件位置。正常的模型请求中断或 Ctrl+C 后，可以通过完整文件路径恢复：

```powershell
mini-coder --config agent.toml --resume "D:\path\to\workspace\.mini-coder\sessions\<session-id>.json"
```

也可以在明确提供相同工作区时使用 Session ID：

```powershell
mini-coder --config agent.toml --workspace "D:\path\to\workspace" --resume <session-id>
```

恢复时不能同时提供新任务。当前配置的 provider、model 和 wire API 必须与保存值一致，API Key 仍从当前环境变量或本地 `auth.json` 重新加载，不会从 Session 恢复。

工具调用按 `requested`、`approved`、`running`、`completed`、`failed`、`denied` 和 `uncertain` 记录：

- 已经完成并保存结果的工具不会在恢复时重复执行。
- 已保存但尚未开始的工具可以继续执行；需要审批的操作会重新遵守当前审批策略。
- 进程结束时处于 `running` 的工具会转成 `uncertain`。因为它可能已经产生文件或外部副作用，Agent 会停止恢复并要求先检查工作区，而不会盲目重放。

检查工作区后，可以显式确认某一项不确定执行已经完成，或确认它没有完成，再继续恢复：

```powershell
mini-coder --config agent.toml --resume "<session-file>" `
  --resolve-uncertain <execution-id>=completed

mini-coder --config agent.toml --resume "<session-file>" `
  --resolve-uncertain <execution-id>=failed
```

执行 ID 会显示在恢复摘要中。这个选项不会重放原工具；它只记录人工检查结论并为模型补入对应工具结果。`completed` 表示副作用确实已经发生，`failed` 表示确认没有完成。不要在没有检查文件或外部状态时使用它。

没有代码修改的只读或分析任务可以正常完成，而不会被要求运行无意义测试。发生文件修改后，只有针对当前修改版本的真实验证命令成功，Session 才会标记为 `completed_verified`；未运行验证或验证后又发生修改时为 `completed_unverified`，最近一次验证失败时为 `failed`。恢复 Session 时还会核对最后一次受追踪修改的文件 hash；文件被外部编辑后，旧验证会自动失效，已完成任务会转为可继续恢复的 `interrupted`。用户拒绝完成任务所需的写入或命令时会记录为 `denied`。CLI 对 verified 和 unverified 的正常完成返回成功退出码，对失败、拒绝和中断返回非零退出码。

一次真实 Responses 跨进程恢复的脱敏验收结果见 [`docs/runs/session-resume-run.md`](docs/runs/session-resume-run.md)。

当前 Session schema 为 v4，增加模型调用数、缺失 usage 次数、累计工具输出，以及命令退出码、超时、截断和耗时。v1/v2/v3 Session 会逐级在内存中迁移并在下一次保存时写成 v4，不会丢失原有消息、工具执行或变更记录。

## Diff、变更历史与 Undo

在 Agent 执行 `write_file` 或 `edit_file` 之前，ChangeTracker 会读取当前文件并生成：

- 工作区相对路径。
- 修改前和修改后的 SHA-256。
- unified diff。
- 新增和删除行数。
- 修改前快照。
- 与工具执行 ID 关联的变更 ID。

默认安全模式会先在审批提示之前打印 Diff。过大的 Diff 会截断并明确显示 `[truncated]`，但增删统计仍基于完整 Diff。`--auto` 模式也会生成并记录同样的预览，只是不等待人工批准。

准备修改时的 `before_hash` 会和审批内容一起保存。实际写入前 ChangeTracker 会再次计算文件 hash；如果用户、编辑器或其他进程在准备或审批期间改变了文件，操作会作为冲突失败，不会覆盖新内容。写入使用同目录临时文件、磁盘刷新和原子替换，并尽量保留原文件权限、UTF-8 BOM 和 CRLF/LF 换行风格。

当前追踪策略只处理不超过 2,000,000 字节的 UTF-8 文本文件。二进制文件、超过限制的文件、目录和符号链接写入会被明确拒绝。

查看某个 Session 的完整变更历史和 Diff 不需要 API Key，也不会调用模型：

```powershell
mini-coder --resume "<session-file>" --show-changes
```

检查当前文件仍与 Agent 写入后的 hash 一致后，可以撤销最后一项仍有效的修改：

```powershell
mini-coder --resume "<session-file>" --undo-last
```

Undo 会把已有文件原子恢复到修改前快照；如果原操作创建了新文件，则恢复为“不存在”。同一文件多次修改时，可以多次运行 `--undo-last` 按逆序恢复。文件在 Agent 修改后又被用户改动时，Undo 会报告冲突并保留用户内容。Undo 状态与 `change_undone` 事件会写回 Session。

文件写入和 Undo 都会推进 Session 的修改版本，并把此前验证标记为 stale。Undo 一个已经完成的 Session 后，状态会回到可恢复的 `interrupted`，防止旧测试结果继续冒充当前代码的验证证据。

Undo 只保证撤销由 ChangeTracker 管理的 `write_file` 和 `edit_file` 修改，不会尝试撤销 `run_command` 产生的任意文件、Git、网络或其他外部副作用。

一次真实 Responses 修改、Diff 展示、Session 追踪和离线 Undo 的脱敏验收结果见 [`docs/runs/change-tracker-run.md`](docs/runs/change-tracker-run.md)。

更完整的多文件失败基线、真实修复、4/4 测试通过、两次逆序 Undo 和行为复测见 [`docs/runs/multifile-workflow-run.md`](docs/runs/multifile-workflow-run.md)。

阶段 E 的真实失败验证、修改后失效、重新验证通过和 `completed_verified` 本地判定记录见 [`docs/runs/verification-loop-run.md`](docs/runs/verification-loop-run.md)。

阶段 F 的真实 Responses 修复、v4 预算统计、命令风险、结构化事件和离线故障注入验收见 [`docs/runs/resilience-budget-run.md`](docs/runs/resilience-budget-run.md)。

## 测试

核心测试使用标准库的 `unittest` 和假模型，不需要 API key，也不会产生模型费用：

```powershell
$env:PYTHONPATH = "src"
python -m unittest discover -s tests -v
```

测试覆盖 Agent 工具循环、审批与风险分类、有限重试和 `Retry-After`、运行预算、集中脱敏、事件 envelope、日志故障隔离、进程树超时/Ctrl+C 清理、路径逃逸、敏感文件、命令执行、上下文压缩、Responses 历史重放、Session 原子保存/迁移/恢复、ChangeTracker、Undo，以及本地验证完成规则。

## 安全边界

文件工具会解析真实路径并拒绝工作区之外的访问，也会隐藏 `.env`、`.git`、私钥等常见敏感位置。`run_command` 是普通本地 shell 进程，不是完整操作系统沙箱；命令理论上可以访问当前用户有权限访问的其他资源。风险分类和人工确认降低误操作概率，但不能替代容器、低权限账号或虚拟机。因此 `--auto` 只能在隔离、受控、可恢复的工作区使用。

Agent 内部的 `.mini-coder/` Session 目录和 Python `__pycache__/` 也会从文件列表隐藏，并拒绝通过文件工具直接读取，避免运行状态或缓存污染模型上下文。

## 下一步

- 增强项目结构理解、忽略规则、搜索体验和失败诊断。
- 建立独立 Eval、跨平台 CI 和提交演示。

完整实现顺序、验收标准和可勾选任务见 [`ROADMAP.md`](ROADMAP.md)。

Responses 的 output 重放、`function_call_output` 和函数工具结构参考[官方 function calling 文档](https://developers.openai.com/api/docs/guides/function-calling)；GPT-5.6 的 Responses、reasoning effort 和 verbosity 参数参考[官方模型指南](https://developers.openai.com/api/docs/guides/latest-model)。
