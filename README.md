# Mini Coder Agent

[![CI](https://github.com/Sammy0520/mini-coder-agent/actions/workflows/ci.yml/badge.svg)](https://github.com/Sammy0520/mini-coder-agent/actions/workflows/ci.yml)

一个不依赖 Agent 框架、控制逻辑可检查、能够修改并验证真实代码的本地编程智能体，提供 CLI 和本地浏览器控制台。模型只负责选择下一步；项目自行实现多轮会话、工具执行、Diff、审批、安全边界、失败恢复、验证状态和可重复 Eval。

**30 秒了解差异点：**

- 不是把模型套进 Agent SDK：核心循环、Responses/Chat Completions 适配和工具协议都在仓库内。
- 不要求用户先写规格文档：一句模糊的自然语言目标会先被整理为意图、保守默认值和最低验收条件，再进入执行。
- 不把“模型说测试通过”当成事实：完成状态由本地命令、修改版本和 Session 记录决定。
- 不把写文件当黑盒：每次写入都有 Diff、前后 hash、原子替换、变更归属和冲突安全 Undo。
- 不只展示一次成功录屏：10 个隔离、确定性的 Eval 覆盖修复、恢复、拒绝、越界、429 和输出预算；真实 provider 使用同一评分协议。

## 已验证发布基线

| 证据 | 结果 |
|---|---:|
| 离线单元测试 | 177/177 通过 |
| 确定性端到端 Eval | 10/10 通过 |
| 真实 Responses Eval | 1/1 `completed_verified` |
| 两分钟多文件演示 | 5/5 测试通过，未修改测试 |
| GitHub Actions | Windows/Ubuntu × Python 3.11/3.12 全部通过 |
| 公开仓库全新环境复现 | 克隆、安装、CLI、测试、Eval 和演示 baseline 全部通过 |

持续集成状态见 [GitHub Actions CI](https://github.com/Sammy0520/mini-coder-agent/actions/workflows/ci.yml)，完整的公开仓库复现过程、对应运行编号和发布元数据检查见 [`docs/runs/release-candidate-audit.md`](docs/runs/release-candidate-audit.md)。

## 当前能力

- `ModelClient`：与厂商无关的模型边界。
- `OpenAICompatibleClient`：首个实现，可按 provider 配置选择 Responses 或 Chat Completions，可连接 OpenAI 或兼容服务。
- 本地工具：`list_files`、`read_file`、`search_text`、`write_file`、`edit_file`、`run_command`。
- 安全控制：工作区路径限制、常见敏感文件过滤、写入防误覆盖、精确文本替换、命令风险分级、子进程树清理和统一脱敏。
- 跨平台执行：向模型说明实际操作系统与默认 shell，文件搜索和修改优先使用内置工具；子命令中的 `python`/`pip` 默认跟随启动 Agent 的虚拟环境。
- 运行控制：最大步骤、总时间、模型/工具调用、单次/累计工具输出和 provider token 预算；连续重复调用检测、旧读取/搜索/写入压缩、重叠读取区间与等价搜索复用，以及可恢复中断。
- Session：原子保存版本化 Session；支持中断恢复和同一会话追加多轮任务，保留 Responses provider items、工具执行状态、审批结果、会话内工作记忆和累计 usage，并阻止不确定副作用被自动重放。
- ChangeTracker：写入前生成 unified diff 和 hash 检查，成功修改保存快照与有序历史；支持冲突安全的 Session 级 Undo。
- 任务成形：运行时识别构建、修复、加功能、改进和解释类意图，按 `discover → frame → locate → implement → verify → finish` 推进；TaskLedger 在同一会话内保留目标、假设、相关文件、修改、证据和未解决项。
- 轻量 Skills：内置修复问题、从零构建小项目和整理项目文档三种工作方式；运行时只按任务意图加载相关的一项，也支持用户添加自己的 Skill，不把全部说明重复塞进模型上下文。
- 验证闭环：Session 记录真实验证命令、覆盖路径/技术区域、退出码、耗时和输出摘要；正常验收必须成功退出，负向输入检查只能作为补充证据，依赖或运行环境错误不能伪装成通过。
- 项目理解：启动时注入有界工作区概览，识别清单、入口、测试、验证命令、项目说明和 Git 起始状态，并跳过依赖、缓存与构建目录。
- 工具体验：文件列表和搜索支持分页，读取支持明确的继续行号，搜索返回过滤原因；失败结果包含稳定错误码和下一步建议。
- 本地 GUI：浏览器页面与 CLI 复用同一个 `AgentRunner`，支持全局会话列表、运行中自由切换会话、同会话连续对话、SSE 事件续接、对话内批量审批、协作式停止、整批或单文件安全 Undo、完整文件查看、运行时间线、Diff、验证结果和会话记忆提示。
- 错误恢复：认证、权限、限流、超时、网络、服务端、请求和响应解析错误分类；只对可恢复错误做带抖动和硬上限的有限重试。
- 权限模式：默认 `safe`；命令按 `read_only`、`workspace_write`、`external_effect`、`dangerous`、`unknown` 分级，`--auto` 也不会自动批准后三类。

## 架构

```text
Interfaces
 ├─ CLI                         终端事件、审批、恢复与 Undo
 └─ Local GUI                   RunController、SSE、页面审批与可视化
      │
      └─ AgentRunner             本地实现循环、停止条件、审批与历史
          ├─ ModelClient         可替换的模型抽象
          │   └─ OpenAICompatibleClient
          ├─ WorkspaceInspector  清单、入口、测试、说明与 Git 基线
          ├─ TaskFramer/Ledger   模糊意图、默认假设、阶段与收敛状态
          ├─ SkillRegistry       确定性选择并按需注入一个相关 Skill
          ├─ ContextManager      本地限制发送给模型的上下文
          ├─ ChangeTracker       Diff、hash、原子写入、冲突检测与 Undo
          ├─ VerificationTracker 验证命令、修改版本、失效规则与完成判定
          └─ ToolRegistry        本地校验和分发文件、搜索与命令工具

EvalRunner                       隔离工作区、确定性/真实模型、评分与报告
```

模型只能看到 AgentRunner 主动发送的消息，不能直接访问磁盘。模型返回 function tool call 后，由 ToolRegistry 在本机执行，再把结果放回下一轮。Responses 模式会在本地保留并重放上一轮全部 output 项和对应的 `function_call_output`；Chat Completions 模式使用标准 `assistant`/`tool` 消息。项目不使用 OpenAI Agents SDK、LangChain、AutoGen 等 Agent 框架，也不调用托管的 Code Interpreter 或 Files 工具。

## 安装

需要 Python 3.11 或更新版本：

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -e .
```

本地 GUI 使用可选依赖，安装方式为：

```powershell
python -m pip install -e ".[gui]"
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
model_streaming = true
prompt_cache_enabled = true
prompt_cache_key = "mini-coder-agent-v1"

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
$env:CODING_AGENT_STREAMING = "true"
$env:CODING_AGENT_PROMPT_CACHE = "true"
$env:CODING_AGENT_PROMPT_CACHE_KEY = "mini-coder-agent-v1"
```

命令行覆盖项优先于环境变量，API key 环境变量优先于本地 `auth.json`，其他环境变量优先于 TOML。`CODING_AGENT_BASE_URL` 可省略，此时客户端使用其默认服务地址。对本地兼容服务，也要把 API key 设置成服务端接受的值。

Responses 默认优先使用流式传输，并发送稳定的 prompt cache key。兼容服务明确拒绝流式或缓存字段时，客户端只针对该能力回退，不会把普通超时误判为“不支持”。Session 会保存 provider 返回的 `cached_tokens`、`cache_write_tokens` 和 `reasoning_tokens`；供应方不返回这些字段时保持未知，不会伪造缓存命中率。

复杂任务采用 cache-stable context：系统提示、用户请求和当前工具历史在舒适预算内保持追加式前缀，逐步变化的验收进度只放在请求尾部。接近上下文预算后，才按稳定的批次边界压缩较早工具记录，并始终保留最近的完整工具调用。这样既避免每一步重写旧前缀导致缓存失效，也能控制长任务后半程的输入体积。完整变更仍保存在 Session 和 ChangeTracker 中，需要核对时可以重新读取文件。

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

### 本地 GUI

安装 `gui` 可选依赖后启动：

```powershell
mini-coder-gui
```

服务默认只监听 `http://127.0.0.1:8765`，并自动打开浏览器；服务器环境可以使用 `mini-coder-gui --no-browser`。页面提交的任务仍由现有 `AgentRunner`、工具、Session、Diff 和验证逻辑真实执行。默认使用 `safe` 审批模式，写入或有副作用的命令会在页面等待批准；API key 继续从环境变量或本地 `auth.json` 加载，不会显示在页面。

当前 GUI 已包含：新建会话时选择工作文件夹、跨文件夹的全局会话列表、运行中切换并重新接入会话、同一会话连续追加任务、Markdown 对话、六阶段进度、结构化事件时间线、SSE 实时推送、Diff、完整文件查看、对话内多文件/多命令批量审批、协作式停止、整批或单文件安全 Undo、验证状态、轮次/会话记忆提示、Skill 管理和自然语言结果。页面不展示 Token 用量，也不把自己包装成完整 IDE。

### Skills

左下角的拼图按钮会打开 Skill 页面。内置 Skill 不可删除；用户可以添加名称、适用场景和希望 Agent 遵循的做法，自定义内容保存在当前用户的 `~/.mini-coder/skills/` 下，GUI 与 CLI 共用。也可以通过 `MINI_CODER_SKILLS_DIR` 指向其他本地目录。

Skill 选择发生在本地，不额外调用一次模型。每轮最多选择一个：明确提到名称时优先使用对应 Skill，其次根据文档关键词、自定义 Skill 名称/简介以及构建或修复意图选择。只有选中的说明会追加到本轮提示词；系统规则、工具定义和安全边界保持稳定并始终拥有更高优先级。

可通过 `--log agent-events.jsonl` 保存可选的本地 JSONL 事件日志。每项事件包含 schema 版本、UTC 时间、run/session ID、step 和运行时长。日志写入失败只产生可见警告，不会中断主任务。事件内容会经过统一凭据脱敏，但仍可能含有代码或命令输出，因此默认关闭，也不应提交。

`run_command` 会把启动 `mini-coder` 的 Python 环境放在子进程 `PATH` 最前面。因此从 `.venv` 启动时，模型执行普通的 `python` 或 `python -m pip` 也会使用同一个 `.venv`，而不是意外落到系统 Python 或 Anaconda。模型服务所用的 `OPENAI_API_KEY` 与 `CODING_AGENT_API_KEY` 不会自动传给项目子进程。命令超时或用户按下 Ctrl+C 时，Runner 会终止它启动的进程组/进程树，并把退出码、预期退出码、超时、耗时和输出截断状态保存到 Session。标准验收必须正常退出；负向验收需明确标记为 `expected_rejection`，它只能作为补充证据，缺少依赖或导入失败不会被误报为任务通过。

每次运行还会向项目命令提供 `MINI_CODER_RUNTIME_DIR`。需要临时数据库、导出文件或其他冒烟数据时，Agent 应把它们放在这个 Session 专用目录，而不是先通过 `write_file` 创建项目文件。这样运行时产物不会混入代码 Diff、Undo 或 ChangeTracker 冲突判断。

## 错误恢复与运行预算

客户端关闭 SDK 内部的隐式重试，由 Runner 统一决定是否恢复：网络连接、HTTP 429 和 500/502/503 可以有限重试；HTTP 400/401/403 不重试；响应解析错误最多重试一次。超时以及 HTTP 504/524 不再原样连续重发，只允许一次缩小操作批次的恢复请求。429 的 `Retry-After` 支持秒数和 HTTP 日期，指数退避带随机抖动，最终等待时间受硬上限约束。错误分类和诊断写入前会先脱敏。

复杂或从零构建的任务会被限制为小批次：默认每次模型响应最多 8 个工具调用，其中最多 3 个文件写入，总新增内容约 18,000 字符。超出的文件操作会被明确退回给模型，要求下一轮继续。可通过 `CODING_AGENT_MAX_RESPONSE_TOOL_CALLS`、`CODING_AGENT_MAX_RESPONSE_WRITE_CALLS` 和 `CODING_AGENT_MAX_RESPONSE_WRITE_CHARS` 调整。

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

本地策略覆盖常见开发验证入口：Python unittest/pytest/doctest/编译检查和主流 lint/type check，Node 语法检查及 npm/pnpm/yarn/bun 的标准 test/build/lint/check 脚本，Deno、Rust、Go、JVM、.NET、C/C++ 构建测试，Ruby、PHP、Swift、Dart/Flutter、Elixir/Erlang、Zig，以及 Shell、文档、YAML/TOML、Compose 配置、Terraform/OpenTofu validate 和 Helm lint。可能生成缓存或构建产物的命令归为 `workspace_write`，不假装成纯只读。带安装、发布、部署、联网审计、远端操作、删除、覆盖、重定向或复合 shell 语法的相似命令仍不会自动放行。

## 项目理解、分页与 Git 基线

新任务开始时，本地会做一次有界、确定性的工作区发现，并把摘要作为 developer 消息提供给模型。它只记录理解项目所需的元数据，不把整个仓库内容塞入上下文：

- 最多扫描 1,500 个条目和 5 层目录。
- 识别 Python、Node、Rust、Java、Gradle 和 Go 的常见清单。
- 识别 `main.py`、`cli.py`、`index.ts`、`main.rs` 等常见入口候选。
- 识别测试目录、测试文件和已有测试配置。
- 根据项目证据推荐 `unittest`、pytest、npm/pnpm/yarn、Cargo、Maven、Gradle 或 Go 验证命令。
- 列出 `AGENTS.md`、Copilot instructions、`CLAUDE.md`、`CONTRIBUTING.md` 和 README；系统安全规则始终优先，嵌套 `AGENTS.md` 在自身子树内比根目录说明更具体。
- 跳过 `.git`、`.mini-coder`、虚拟环境、依赖目录、缓存、coverage、`build`、`dist`、`target` 和常见编译产物。

`list_files` 和 `search_text` 在结果截断时返回 `next_offset`，`read_file` 返回 `next_start_line`、总行数和文件大小。搜索最多遍历 10,000 个可见条目，超过后明确标记 `scan_truncated`；它还区分 `no_match`、`all_candidates_filtered` 和“已扫描文件无结果但存在被过滤候选”，并分别统计策略、glob、二进制、大文件和解码过滤。

如果模型用不同范围重复读取同一个文件且至少一半内容重叠，结果会附带轻量效率提示。Session 记录失败工具数、非法工具数和重复读取提示数；连续完全相同的调用仍保留硬停止规则。真实 Eval 显示 Responses 可以在同一轮并行请求多个现有工具，因此没有再增加批量读取或批量搜索工具。

如果工作区属于 Git 仓库，本地只运行受控的只读状态查询，保存任务开始时已有改动和最多 2 MB 文件的指纹。最终报告只把 ChangeTracker 管理的写入归因给 Agent，并单独报告任务期间新增的非托管 Git 变化。它不会自动 commit、push、reset、checkout 或清理工作区；`run_command` 产生的文件变化也不会被错误包装成 ChangeTracker 修改。

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

中断中的 Session 可以直接恢复原任务，也可以在 GUI 或 CLI 中给出新的继续说明，作为同一会话的下一轮接着处理。已经结束的 Session 同样可以继续输入；不同 Session 之间不会共享工作记忆。当前配置的 provider、model 和 wire API 必须与保存值一致，API Key 仍从当前环境变量或本地 `auth.json` 重新加载，不会从 Session 恢复。

```powershell
mini-coder --config agent.toml --resume "<session-file>" "继续在刚才的实现上增加导出功能并验证"
```

每次模型请求都会记录本轮消息数、发送消息数、本地 Token 估算、工具 schema 大小、耗时、provider usage、provider 返回的模型标识，以及兼容两种常见计数口径的缓存复用比例。原始消息仍完整落盘以便审计；进入下一轮时只发送本 Session 的结构化工作记忆、上一轮结论和最新用户请求，不重放旧工具日志。单轮内部优先保持可缓存的追加历史，只在接近预算时成批压缩。文件读取会合并本轮已经看过的行号区间，重叠请求只返回尚未见过的行；大小写等价的普通搜索和已覆盖分页会复用先前结果。写入或可能改变工作区的命令会让目录级搜索缓存失效，文件读取则继续用内容指纹判断是否仍然有效。缓存命中次数会保存到 Session 和运行事件中。

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

没有代码修改的只读或分析任务可以正常完成，而不会被要求运行无意义测试。发生代码或配置修改后，只有真实验证命令符合声明的退出码预期，Session 才会标记为 `completed_verified`；未运行验证、相关实现再次变化或验证结果不符合预期时会分别记为未验证、失效或失败。验证可声明覆盖路径或 Python、网页、文档、原生代码等技术区域，无关修改不会废弃仍有效的检查；相同命令和范围在文件未变化时也不会重复执行。每次请求还会附带短小的用户要求与本地证据清单，检查已通过后明确提示 Agent 收敛并直接回答。恢复 Session 时还会核对最后一次受追踪修改的文件 hash；文件被外部编辑后，旧验证会自动失效，已完成任务会转为可继续恢复的 `interrupted`。用户拒绝完成任务所需的写入或命令时会记录为 `denied`。CLI 对 verified 和 unverified 的正常完成返回成功退出码，对失败、拒绝和中断返回非零退出码。

一次真实 Responses 跨进程恢复的脱敏验收结果见 [`docs/runs/session-resume-run.md`](docs/runs/session-resume-run.md)。

当前 Session schema 为 v7，增加会话轮次、面向用户的对话记录、仅限当前 Session 的结构化工作记忆和逐次模型调用统计。v1～v6 Session 会逐级在内存中迁移并在下一次保存时写成 v7，不会丢失原有消息、工具执行或变更记录。

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

GUI 除“撤销上次修改”外，还会在展开文件列表后提供“撤销此文件”。单文件撤销使用同样的 hash 冲突检查；如果该文件后来又有新的修改，就不会覆盖当前内容。

文件写入和 Undo 都会推进 Session 的修改版本；代码、测试和配置变化会把相关验证标记为 stale，普通文档变化不会无条件废弃与文档无关的代码测试。Undo 一个已经完成的 Session 后，状态会回到可恢复的 `interrupted`，防止旧测试结果继续冒充当前代码的验证证据。

Undo 只保证撤销由 ChangeTracker 管理的 `write_file` 和 `edit_file` 修改，不会尝试撤销 `run_command` 产生的任意文件、Git、网络或其他外部副作用。

一次真实 Responses 修改、Diff 展示、Session 追踪和离线 Undo 的脱敏验收结果见 [`docs/runs/change-tracker-run.md`](docs/runs/change-tracker-run.md)。

更完整的多文件失败基线、真实修复、4/4 测试通过、两次逆序 Undo 和行为复测见 [`docs/runs/multifile-workflow-run.md`](docs/runs/multifile-workflow-run.md)。

阶段 E 的真实失败验证、修改后失效、重新验证通过和 `completed_verified` 本地判定记录见 [`docs/runs/verification-loop-run.md`](docs/runs/verification-loop-run.md)。

阶段 F 的真实 Responses 修复、v4 预算统计、命令风险、结构化事件和离线故障注入验收见 [`docs/runs/resilience-budget-run.md`](docs/runs/resilience-budget-run.md)。

阶段 G 的多文件项目入口、验证命令、忽略规则、Git 起始改动归属和工具效率验收见 [`docs/runs/project-understanding-run.md`](docs/runs/project-understanding-run.md)。

## 测试

核心测试使用标准库的 `unittest` 和假模型，不需要 API key，也不会产生模型费用；当前完整套件为 166 项：

```powershell
$env:PYTHONPATH = "src"
python -m unittest discover -s tests -v
```

测试覆盖 Agent 工具循环、审批与风险分类、有限重试和 `Retry-After`、运行预算、集中脱敏、事件 envelope、日志故障隔离、进程树超时/Ctrl+C 清理、路径逃逸、敏感文件、命令执行、Token-aware 上下文压缩、同会话多轮、Responses 历史重放、Session 原子保存/迁移/恢复、ChangeTracker、Undo、本地验证完成规则以及 Eval/发布脚本。

## Eval 与可重复证据

确定性 Eval 使用假模型，但会真实执行 AgentRunner、文件工具、命令、Session、Diff、审批和安全策略，不需要 API key，也不会产生模型费用：

```powershell
mini-coder-eval --list
mini-coder-eval --output eval-results\local
```

只运行指定场景：

```powershell
mini-coder-eval --scenario failed_then_fix --output eval-results\focused
```

真实模型模式必须同时显式给出 `--live` 和至少一个 `--scenario`，因此 CI 或误输入不会自动产生模型调用：

```powershell
mini-coder-eval --live --scenario multifile_interface `
  --config agent.toml --output eval-results\live
```

每次运行生成 `eval-report.json`、`summary.md` 和单场景 JSON。失败场景会额外保留脱敏 Session 与事件；确定性失败保留隔离工作区，真实失败默认只保留工作区文件 hash 清单，避免自动复制可能敏感的源码。只有显式使用 `--keep-workspaces` 才会保留真实工作区内容。`eval-results/` 已被 Git 忽略。

当前基线：

| 模式 | 结果 | 关键证据 |
|---|---:|---|
| 确定性完整套件 | 10/10 | 修复、跨文件、失败后继续、只读、越界、恢复、拒绝、Undo 冲突、429、长输出 |
| 真实 Responses `multifile_interface` | 1/1 | `completed_verified`，2 个预期文件，0 个无关修改，4 次模型/9 次工具调用，46.81 秒 |

真实 Eval 的 provider usage 为 29,506 input、1,025 output、30,531 total tokens；只有 provider 实际返回 usage 时才记录。完整脱敏证据见 [`docs/runs/eval-ci-release-run.md`](docs/runs/eval-ci-release-run.md)。

发布候选还从公开 GitHub URL 克隆到一次性目录，使用全新虚拟环境重新安装并通过完整测试、确定性 Eval、CLI 入口和演示初始状态复现；该过程没有复制本地 API Key。详见 [`docs/runs/release-candidate-audit.md`](docs/runs/release-candidate-audit.md)。

## 两分钟多文件演示

演示夹具比单文件折扣示例更接近小型服务：它包含项目说明、定价、服务、CLI 和测试，初始状态同时存在缺失策略模块与边界失败。每次演示先一键恢复到同一初始状态：

```powershell
python scripts\reset-demo.py
cd examples\order_service\workspace
python -m unittest discover -s tests -v
cd ..\..\..
mini-coder --config agent.toml --auto `
  --workspace examples\order_service\workspace "Read TASK.md and complete it."
```

任务要求新增 `DiscountPolicy`、修改 pricing/service 调用链、保持公共 API，并运行完整测试；`AGENTS.md` 明确禁止修改测试。生成的 `workspace/` 被 Git 忽略，原始 `fixture/` 不会被 Agent 修改。

## 安全边界

文件工具会解析真实路径并拒绝工作区之外的访问，也会隐藏 `.env`、`.git`、私钥等常见敏感位置。`run_command` 是普通本地 shell 进程，不是完整操作系统沙箱；命令理论上可以访问当前用户有权限访问的其他资源。风险分类和人工确认降低误操作概率，但不能替代容器、低权限账号或虚拟机。因此 `--auto` 只能在隔离、受控、可恢复的工作区使用。

Agent 内部的 `.mini-coder/` Session 目录和 Python `__pycache__/` 也会从文件列表隐藏，并拒绝通过文件工具直接读取，避免运行状态或缓存污染模型上下文。

## 设计取舍与已知限制

- `run_command` 是受风险分级、审批、超时和脱敏约束的本地 shell，不是容器或完整操作系统沙箱；不可信仓库应放入虚拟机、容器或低权限账号。
- 路径策略严格约束专用文件工具，但无法从操作系统层面阻止一个已获批准的任意 shell 命令访问当前账号可见资源。
- 当前只有 `OpenAICompatibleClient`，支持 Responses 与 Chat Completions function calling；其他厂商可通过 `ModelClient` 扩展，但尚无内置适配器。
- ChangeTracker 追踪 `write_file`/`edit_file`，不声称可以撤销命令、依赖安装、Git 或网络副作用。
- 文本修改采用可审计的精确替换，不提供 AST 重构或模糊补丁；精确片段不匹配时会返回最接近的当前代码供下一轮修正，单个受追踪文本文件上限为 2 MB。
- 当前 GUI 是复用真实 Agent 内核的本地展示控制台，不是完整代码编辑器或 IDE；停止模型请求只能在该次请求返回或超时后生效，但审批等待、本地命令和步骤间停止会立即响应。
- 项目不包含多 Agent、向量数据库、通用 RAG、MCP 生态或自动 commit/push/PR；这些不属于当前考核核心闭环。
- Eval 能证明预先声明场景的行为，不能保证模型在任意仓库中都成功；真实模型结果会受 provider、模型版本和网络状态影响。

## 许可证

本项目采用 [MIT License](LICENSE)。

## 下一步

- `v0.1.0` 作为首个公开版本发布，代码、测试、Eval、CI 和全新环境复现证据见本页及发布候选审计记录。
- `v0.2.0` 已形成适合视频展示的真实本地 GUI，并支持同一会话连续协作。
- 下一步用固定真实任务比较优化前后的成功率、无关修改、模型调用、Token 和耗时，再决定是否增加服务端续接或自适应 reasoning 档位。

完整实现顺序、验收标准和可勾选任务见 [`ROADMAP.md`](ROADMAP.md)。

Responses 的 output 重放、`function_call_output` 和函数工具结构参考[官方 function calling 文档](https://developers.openai.com/api/docs/guides/function-calling)；GPT-5.6 的 Responses、reasoning effort 和 verbosity 参数参考[官方模型指南](https://developers.openai.com/api/docs/guides/latest-model)。
