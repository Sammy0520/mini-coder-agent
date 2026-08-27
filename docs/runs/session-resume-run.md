# Responses Session 跨进程恢复验收记录

本记录保存一次经过脱敏的真实模型验收结果。验收使用专门生成的无敏感临时夹具，不使用仓库源代码、用户文件或凭据内容。原始 Session 与 JSONL 日志位于被 Git 忽略的 `tmp/` 中，不提交仓库。

## 运行环境

- 日期：2026-08-27
- Provider：`aicode007`
- Model：`gpt-5.6-sol`
- Wire API：Responses
- Session schema：1
- Session ID：`856174970d2a4594a86477bcf6664399`
- 审批模式：`auto`，仅用于可丢弃的合成夹具目录

临时工作区只包含 `public_fixture.txt`。文件明确声明自己是合成测试数据，并包含验证标记 `ALPHA-42`；不存在项目代码、个人信息或凭据。

## 验收方法

任务要求模型使用 `read_file` 读取合成文件、禁止修改和命令执行，并报告验证标记。

第一次启动将 `max_steps` 设置为 1，强制在任务中途停止。模型在第 1 步执行了 `list_files`，工具结果成功保存；进程以 `max_steps` 返回非零退出码。此时 Session 状态为：

- `status=failed`
- `current_step=1`
- `stop_reason=max_steps`
- 1 条工具执行记录，`list_files=completed`
- 4 条已保存消息

第二次使用同一个 Session 文件启动独立进程，并将 `max_steps` 提高到 5。恢复摘要显示最后完成步骤为 1、没有待处理或不确定工具。运行从第 2 步继续：

1. 第 2 步执行新的 `read_file`，没有重放第 1 步已完成的 `list_files`。
2. 第 3 步输出验证标记 `ALPHA-42`。
3. 进程以成功退出码结束。

## 最终结果

- `status=completed_unverified`
- `current_step=3`
- `stop_reason=model_completed`
- 7 条消息
- 2 条工具执行记录：`list_files=completed`、`read_file=completed`
- 两个不同且稳定的 `tool_execution_id`
- 没有写文件或运行命令
- 合成夹具运行前后 SHA-256 均为 `6CCE1118DF4B3CE8442C24413E4ACAB56CF793EBC2226EE849ACBDC570082DAF`

累计 provider usage：

- input tokens：17,510
- output tokens：139
- total tokens：17,649

第一段日志以 `run_finished/result_status=max_steps` 结束；恢复日志以 `session_resumed` 开始，并以 `run_finished/result_status=completed/session_status=completed_unverified` 结束。CLI、事件日志和 Session 的终态一致。

## 安全检查

对 Session 和两段 JSONL 日志扫描以下常见凭据形态：

- `sk-...`
- `Bearer ...`
- Authorization 请求头字段

三类匹配数均为 0。原始文件继续由 `.gitignore` 排除。

## 结论

这次验收确认了 Responses 多轮工具上下文可以跨进程恢复；已完成工具不会重复执行；恢复后的模型能够继续获得合法、足够的消息与函数结果配对；步骤、usage、工具状态和最终事件会持续写回同一个版本化 Session。
