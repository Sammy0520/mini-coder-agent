# Mini Coder

[![CI](https://github.com/Sammy0520/mini-coder-agent/actions/workflows/ci.yml/badge.svg)](https://github.com/Sammy0520/mini-coder-agent/actions/workflows/ci.yml)

Mini Coder 是面向本地、中等复杂度开发任务的轻量 Code Agent。它不追求覆盖所有场景，而是用可检查的阶段化 ReAct 循环，在更少的无效上下文、模型调用和多 Agent 开销下完成日常 Build、Fix 与 Feature 任务。Agent 会在 Discover、Frame、Locate、Implement、Verify、Finish 六个阶段间依据工具证据动态往返，最终交付变更、验证证据与已知限制。

## 特色

- **意图识别**：本地判断任务类型、是否先给方案及软调用预算，不额外消耗一次模型调用。
- **记忆缓存**：Memory V2 将稳定规则与项目检查点冻结为可复用前缀，仅保留近期操作为热上下文。
- **按需并行**：只在边界清晰且相互独立时启用最多两个隔离子 Agent，由主 Agent 合并并做集成验证。
- **可信收尾**：验证结果绑定代码版本；可将耗时检查与无工具的交付说明草稿重叠，但只有 Finish Gate 通过才允许宣称成功。
- **本地安全**：写入限于工作区，危险命令需要确认，修改可撤销，运行过程可在 GUI 中检查。

## 安装与运行

需要 Python 3.11+。Windows PowerShell：

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[gui]"
Copy-Item agent.toml.example agent.toml
& .\scripts\set-local-api-key.ps1
.\.venv\Scripts\mini-coder-gui.exe
```

浏览器访问 `http://127.0.0.1:8765`。CLI 示例：

```powershell
.\.venv\Scripts\mini-coder.exe --config agent.toml --workspace D:\project "修好刷新后丢失的数据"
```

Linux/macOS 将 `.venv\Scripts` 换为 `.venv/bin`。可在 `agent.toml` 中配置兼容 Responses API 的供应商；密钥只保存在本机并已被 Git 忽略。

## 验证与边界

```powershell
.\.venv\Scripts\python.exe scripts\run-unit-tests.py
```

Mini Coder 适合受信任的本地工作区，不提供操作系统级沙箱，也不以替代 Codex 等通用商业 Agent 为目标。
