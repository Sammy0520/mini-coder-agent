# 阶段 F：错误恢复、预算、命令风险与事件验收

本记录保存阶段 F 的脱敏验收结果。真实 API 凭据、请求头、完整 Session 和原始事件日志均未提交。

## 验收环境

- 平台：Windows / PowerShell
- provider：aicode007
- wire API：Responses
- 模型：`gpt-5.6-sol`
- Session schema：v4
- event schema：v1
- 工作区：Git 忽略的隔离临时目录

## 任务

演示项目包含一个算术平均值函数和两个测试。初始实现错误地使用 `len(values) - 1` 作为除数。运行基线测试得到：

```text
Ran 2 tests
FAILED (failures=1, errors=1)
```

真实模型任务要求读取现有文件、做最小修复，并执行 `python -m unittest discover -v` 验证。

## 真实运行结果

Agent 完成了以下闭环：

1. 列举工作区文件。
2. 读取实现和测试。
3. 生成并应用一行 unified diff，将除数改为 `len(values)`。
4. 将验证命令分类为 `workspace_write`，在受控 `--auto` 模式下执行。
5. 本地记录命令退出码为 0、未超时、输出未截断。
6. 两项测试全部通过，最终 Session 状态为 `completed_verified`，验证状态为 `passed`。

运行统计来自 provider usage 和 v4 Session，不是本地估算：

| 指标 | 结果 |
|---|---:|
| 模型请求 | 5 |
| 工具调用 | 5 |
| 重试 | 0 |
| 累计工具输出 | 2,116 字符 |
| input tokens | 32,942 |
| output tokens | 617 |
| total tokens | 33,559 |
| 总耗时 | 40.50 秒 |

本次正常网络运行没有人为制造 429/5xx，因此没有发生真实重试。429、500/502/503/504、超时、网络错误、400/401/403、`Retry-After`、解析错误单次重试上限和重试硬上限由可重复的离线故障注入测试覆盖。

## 事件与脱敏检查

- 写入 34 条 JSONL 事件。
- 所有事件的 `event_schema_version` 均为 1。
- 所有事件都有 UTC 时间戳和 run ID。
- 核心路径包含 `run_started`、模型请求/响应、工具请求/批准/完成、变更、验证和 `run_completed`。
- 对 Session 和事件日志扫描 Bearer、常见 key 前缀、Authorization、API key 字段和模型密钥环境变量，未发现命中。
- 模型密钥未进入验证命令的子进程环境。

## 离线回归

阶段 F 完成后运行完整测试集：

```text
Ran 100 tests
OK
```

新增覆盖包括错误分类与有限重试、运行预算、未知命令审批、进程树 timeout/Ctrl+C 清理、命令结果持久化、统一脱敏、事件 envelope 和日志写入故障隔离。

## 结论

阶段 F 的真实服务主路径和离线故障路径均通过。Runner 能在网络波动和预算耗尽时有限恢复或可恢复地停止；命令自动批准受风险等级约束；本地执行结果、usage 与结构化事件可追踪，凭据经过集中脱敏。
