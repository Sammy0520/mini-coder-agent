项目名称：Mini Coder Agent

Git 仓库地址：https://github.com/Sammy0520/mini-coder-agent

运行方法：安装 Python 3.11+，在项目目录执行“python -m pip install -e .”。复制不含密钥的 agent.toml.example，使用 provider TOML 配置模型、base URL、Responses/Chat Completions 协议、推理强度和输出详细度；API key 通过环境变量或已被 Git 忽略的本地 auth.json 提供。然后运行“mini-coder --config agent.toml --workspace <项目目录> <任务>”。默认情况下，写文件和执行命令需人工确认；在可丢弃的演示目录可添加 --auto。每次运行会在工作区的 .mini-coder/sessions 中原子保存版本化 Session；中断后可通过“mini-coder --config agent.toml --resume <Session 文件>”恢复。

特色功能：项目未使用任何 Agent 框架。自行实现对话与上下文管理、工具定义和本地执行、模型响应解析、Agent 循环、最大步数、连续重复调用检测及错误处理。通过 ModelClient 抽象隔离模型厂商；OpenAICompatibleClient 支持 Responses 与 Chat Completions 双协议，并在 Responses 多轮工具调用中本地保留完整 output 项。内置文件列举、读取、搜索、写入、精确编辑和命令执行工具；文件工具限制在工作区内，并过滤常见敏感文件；命令具有超时和输出截断。版本化 Session 保存完整消息、provider items、工具状态、审批结果和累计 usage，但不保存 API key；恢复时不会重复已完成工具，并会阻止状态不确定的副作用被自动重放。用户检查工作区后，可用显式参数把不确定执行确认为已完成或未完成，人工决定会写回 Session。ChangeTracker 在写入前生成 Diff 并核对 hash，使用原子替换，保存快照和有序修改历史；可离线查看 Session Diff，并在 hash 未冲突时安全撤销最后一次受追踪修改。标准库测试使用假模型验证完整工具循环、恢复和 Undo 语义，无需 API key。

安全说明：API key 通过环境变量或已被 Git 忽略的本地 auth.json 提供，不进入 provider TOML。通用 shell 不是完整操作系统沙箱，因此自动批准模式只用于受控目录。
