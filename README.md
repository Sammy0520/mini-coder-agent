# Mini Coder Agent

一个不依赖 Agent 框架、核心逻辑可检查的命令行编程智能体。模型负责选择下一步，本项目自行负责对话历史、上下文裁剪、工具定义与本地执行、响应解析、审批、循环终止和错误处理。

## 当前能力

- `ModelClient`：与厂商无关的模型边界。
- `OpenAICompatibleClient`：首个实现，可按 provider 配置选择 Responses 或 Chat Completions，可连接 OpenAI 或兼容服务。
- 本地工具：`list_files`、`read_file`、`search_text`、`write_file`、`edit_file`、`run_command`。
- 安全控制：工作区路径限制、常见敏感文件过滤、写入防误覆盖、精确文本替换、命令超时、输出截断。
- 跨平台执行：向模型说明实际操作系统与默认 shell，文件搜索和修改优先使用内置工具；子命令中的 `python`/`pip` 默认跟随启动 Agent 的虚拟环境。
- 运行控制：最大步骤数、连续重复调用检测、工具参数校验、上下文压缩、Ctrl+C 中断。
- Session：每次 CLI 运行原子保存版本化 Session；支持 `--resume`，保留 Responses provider items、工具执行状态、审批结果和累计 usage，并阻止不确定副作用被自动重放。
- 权限模式：默认 `safe`，写文件和执行命令需要确认；`--auto` 只适合受控或可丢弃的演示目录。

## 架构

```text
CLI
 └─ AgentRunner                 本地实现循环、停止条件、审批与历史
     ├─ ModelClient             可替换的模型抽象
     │   └─ OpenAICompatibleClient
     ├─ ContextManager          本地限制发送给模型的上下文
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

可通过 `--log agent-events.jsonl` 保存可选的本地事件日志。日志可能含有代码或命令输出，因此默认关闭，也不应提交。

`run_command` 会把启动 `mini-coder` 的 Python 环境放在子进程 `PATH` 最前面。因此从 `.venv` 启动时，模型执行普通的 `python` 或 `python -m pip` 也会使用同一个 `.venv`，而不是意外落到系统 Python 或 Anaconda。模型服务所用的 `OPENAI_API_KEY` 与 `CODING_AGENT_API_KEY` 不会自动传给项目子进程。

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

在后续验证闭环完成前，正常结束的 Session 暂时标记为 `completed_unverified`；CLI 的原有成功退出行为保持不变。

一次真实 Responses 跨进程恢复的脱敏验收结果见 [`docs/runs/session-resume-run.md`](docs/runs/session-resume-run.md)。

## 测试

核心测试使用标准库的 `unittest` 和假模型，不需要 API key，也不会产生模型费用：

```powershell
$env:PYTHONPATH = "src"
python -m unittest discover -s tests -v
```

测试覆盖 Agent 工具循环、用户拒绝写入、重复调用停止、路径逃逸、敏感文件、精确编辑、搜索、命令执行、上下文压缩、provider TOML、Responses 历史重放、两种兼容响应解析，以及 Session 原子保存、损坏文件拒绝、状态转换、Ctrl+C 恢复和不确定副作用阻断。

## 安全边界

文件工具会解析真实路径并拒绝工作区之外的访问，也会隐藏 `.env`、`.git`、私钥等常见敏感位置。`run_command` 是普通本地 shell 进程，不是完整操作系统沙箱；命令理论上可以访问当前用户有权限访问的其他资源。因此默认采用人工确认，`--auto` 只能在隔离、受控、可恢复的工作区使用。

## 下一步

- 实现 ChangeTracker、统一 Diff、原子文件修改和安全 Undo。
- 增加任务验证状态、错误重试、运行预算和日志脱敏。
- 建立 Eval、跨平台 CI 和多文件演示项目。

完整实现顺序、验收标准和可勾选任务见 [`ROADMAP.md`](ROADMAP.md)。

Responses 的 output 重放、`function_call_output` 和函数工具结构参考[官方 function calling 文档](https://developers.openai.com/api/docs/guides/function-calling)；GPT-5.6 的 Responses、reasoning effort 和 verbosity 参数参考[官方模型指南](https://developers.openai.com/api/docs/guides/latest-model)。
