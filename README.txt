项目名称：Mini Coder Agent

Git 仓库地址：TODO（创建题目发布后的公开仓库后填写）

运行方法：安装 Python 3.11+，在项目目录执行“python -m pip install -e .”。复制不含密钥的 agent.toml.example，使用 provider TOML 配置模型、base URL、Responses/Chat Completions 协议、推理强度和输出详细度；API key 通过环境变量或已被 Git 忽略的本地 auth.json 提供。然后运行“mini-coder --config agent.toml --workspace <项目目录> <任务>”。默认情况下，写文件和执行命令需人工确认；在可丢弃的演示目录可添加 --auto。

特色功能：项目未使用任何 Agent 框架。自行实现对话与上下文管理、工具定义和本地执行、模型响应解析、Agent 循环、最大步数、连续重复调用检测及错误处理。通过 ModelClient 抽象隔离模型厂商；OpenAICompatibleClient 支持 Responses 与 Chat Completions 双协议，并在 Responses 多轮工具调用中本地保留完整 output 项。内置文件列举、读取、搜索、写入、精确编辑和命令执行工具；文件工具限制在工作区内，并过滤常见敏感文件；命令具有超时和输出截断。标准库测试使用假模型验证完整工具循环，无需 API key。

安全说明：API key 通过环境变量或已被 Git 忽略的本地 auth.json 提供，不进入 provider TOML。通用 shell 不是完整操作系统沙箱，因此自动批准模式只用于受控目录。
