项目名称：Mini Coder Agent

Git 仓库地址：https://github.com/Sammy0520/mini-coder-agent

运行方法：安装 Python 3.11+，命令行版本执行“python -m pip install -e .”；开发中的本地 GUI 执行“python -m pip install -e \".[gui]\"”，然后运行“mini-coder-gui”。复制不含密钥的 agent.toml.example，使用 provider TOML 配置模型、base URL、Responses/Chat Completions 协议、推理强度和输出详细度；API key 通过环境变量或已被 Git 忽略的本地 auth.json 提供。命令行运行“mini-coder --config agent.toml --workspace <项目目录> <任务>”。默认情况下，写文件和执行命令需人工确认；在可丢弃的演示目录可添加 --auto。每次运行会在工作区的 .mini-coder/sessions 中原子保存版本化 Session；中断后可通过“mini-coder --config agent.toml --resume <Session 文件>”恢复。离线评测运行“mini-coder-eval --output eval-results/local”；真实评测必须同时显式指定“--live --scenario <名称>”。

特色功能：项目未使用任何 Agent 框架。自行实现对话与上下文管理、项目发现、工具定义和本地执行、模型响应解析、Agent 循环、运行预算、有限重试、Diff/审批、验证状态和准确最终报告。通过 ModelClient 抽象隔离模型厂商；OpenAICompatibleClient 支持 Responses 与 Chat Completions 双协议。内置文件列举、分页读取、搜索、写入、精确编辑和命令执行工具；文件工具限制在工作区内并过滤敏感/依赖/构建目录。版本化 Session 保存消息、provider items、工具状态、审批结果、Git 基线和累计 usage，但不保存 API key；恢复不会重复已完成工具，并阻止不确定副作用被自动重放。ChangeTracker 在写入前生成 Diff 并核对 hash，使用原子替换，保存快照和有序历史；可离线查看 Diff，并在 hash 未冲突时安全 Undo。开发中的本地 GUI 通过线程安全 RunController 和 SSE 复用真实 Agent 内核，已能展示运行时间线、Diff、页面审批、验证状态和最终摘要；它不是重新实现的聊天演示，也还不是完整 IDE。独立 Eval runner 在可重置工作区运行 10 个确定性场景，覆盖单文件、多文件、首次测试失败后继续、只读、路径逃逸、Session 恢复、审批拒绝、Undo 冲突、429 重试和长输出；真实模型使用同一评分协议。v0.1.0 的 115 项离线测试和 10 项确定性 Eval 全部通过；当前 GUI 开发分支增加 5 项控制器与 HTTP/SSE 测试。GitHub Actions 的 Windows/Ubuntu 与 Python 3.11/3.12 四个组合全部通过。

安全说明：API key 通过环境变量或已被 Git 忽略的本地 auth.json 提供，不进入 provider TOML、Session 或 Eval 报告。文件工具有真实路径边界；通用 shell 不是完整操作系统沙箱，因此自动批准模式只用于可丢弃的受控目录。ChangeTracker 不声称能撤销命令、Git、网络或依赖安装副作用。

发布状态：v0.1.0 已采用 MIT License 并完成公开 tag/Release。当前在独立 GUI 开发分支准备 v0.2.0；先完成视频展示需要的真实运行、审批、验证、Session Resume 和 Undo，再进入未知任务盲测和数据驱动效率优化。
