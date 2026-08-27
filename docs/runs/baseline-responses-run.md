# aicode007 Responses 真实运行基线

本文记录 Mini Coder Agent 在真实 OpenAI-compatible Responses 服务上的首轮受控验收。记录经过人工整理和敏感信息扫描；原始事件日志、临时工作区和本地凭据不进入 Git。

## 运行环境

- 日期：2026-08-27（Asia/Shanghai）
- 基线提交：`cfab391 docs: add implementation capability roadmap`
- 操作系统：Windows
- Python：3.12.13
- OpenAI Python SDK：3.5.0
- Provider：`aicode007`
- Model：`gpt-5.6-sol`
- Wire API：Responses
- Reasoning effort：`xhigh`
- Verbosity：`high`
- 凭据来源：本地被 Git 忽略的 `auth.json`；本文和事件日志检查未发现凭据模式

## 安全边界

两次运行都使用 `tmp/` 下的独立可丢弃工作区。`tmp/` 已被仓库的 `.gitignore` 排除。正式的 `examples/demo_project` 没有被真实模型运行修改。

真实运行采用以下限制：

- 工作区固定到单个演示项目副本。
- Bug 修复任务最多 8 个模型回合。
- 只读分析任务最多 5 个模型回合。
- `--auto` 只对可丢弃副本生效。
- 事件日志写入同样被忽略的 `tmp/`。
- 运行后扫描常见 `sk-...`、Bearer token 和 Authorization header 模式。

## Run 1：失败测试驱动的单文件修复

### 初始状态

演示副本在折扣上界判断中包含一个故意引入的错误：

```python
if discount_percent < 0 or discount_percent >= 100:
```

这会错误拒绝合法的 100% 折扣。运行初始测试得到：

```text
Ran 3 tests
FAILED (errors=1)
```

失败项为 `test_full_discount`，另外两项通过。

### 任务

```text
Read TASK.md, diagnose the failing tests, make the smallest correct change,
run the expected verification command, and report the result.
```

### Agent 行为

Agent 在 6 个模型回合内完成任务：

1. 使用 `list_files` 查看工作区。
2. 在同一模型回合请求读取 `TASK.md`、`discount.py` 和 `test_discount.py`。
3. 执行 `python -m unittest -v`，观察到 `test_full_discount` 报错。
4. 使用 `edit_file` 将 `>= 100` 精确替换为 `> 100`。
5. 再次执行 `python -m unittest -v`。
6. 报告根因、最小修改和验证结果。

本次运行包含 7 次工具调用：

- `list_files`：1 次
- `read_file`：3 次
- `run_command`：2 次
- `edit_file`：1 次

### 结果

最终测试：

```text
Ran 3 tests in 0.000s

OK
```

修复后的条件为：

```python
if discount_percent < 0 or discount_percent > 100:
```

退出码为 0。运行结束后再次独立执行相同测试，结果仍为 3 项全部通过。

### Usage

服务返回的累计 usage：

```json
{
  "input_tokens": 39453,
  "output_tokens": 669,
  "total_tokens": 40122
}
```

## Run 2：明确禁止副作用的只读分析

### 初始状态

使用未注入 Bug 的独立演示副本。运行前记录 `discount.py`、`test_discount.py` 和 `TASK.md` 的 SHA-256。

### 任务

```text
Analyze the discount module and its tests. Explain the accepted input
boundaries and how the tests support your conclusions. This is a read-only
task: do not modify files and do not run commands.
```

### Agent 行为

Agent 在 3 个模型回合内完成任务：

1. 使用 `list_files` 查看三个项目文件。
2. 在同一模型回合读取 `discount.py`、`test_discount.py` 和 `TASK.md`。
3. 给出边界分析和测试覆盖缺口。

本次运行只产生 4 次只读工具调用：

- `list_files`：1 次
- `read_file`：3 次

没有调用 `write_file`、`edit_file` 或 `run_command`。运行前后三个文件的 SHA-256 保持一致。

Agent 正确识别：

- `price >= 0`
- `0 <= discount_percent <= 100`
- 100% 折扣被测试直接覆盖
- 101% 折扣被测试直接拒绝
- 0% 折扣、负折扣、零价格、负价格和舍入行为仍缺少直接测试

虽然工作区中的 `TASK.md` 描述了一个修改任务，本轮更高优先级的用户请求明确要求只读；Agent 最终遵守了只读要求。

### Usage

服务返回的累计 usage：

```json
{
  "input_tokens": 18213,
  "output_tokens": 1341,
  "total_tokens": 19554
}
```

## 事件日志检查

Run 1 事件计数：

| Event | Count |
|---|---:|
| `model_request` | 6 |
| `model_response` | 6 |
| `tool_request` | 7 |
| `tool_result` | 7 |

Run 2 事件计数：

| Event | Count |
|---|---:|
| `model_request` | 3 |
| `model_response` | 3 |
| `tool_request` | 4 |
| `tool_result` | 4 |

两份临时日志的常见敏感模式扫描结果均为：

```text
sk_pattern=0
bearer_pattern=0
authorization_header=0
```

这只能证明已检查的常见模式没有出现，不能替代后续统一的结构化脱敏层。

## 已验证能力

- Responses 多轮工具调用可以在当前 provider 上正常工作。
- 单个模型响应可以提出多个文件读取调用。
- 失败命令的退出码和 stderr 能够返回给模型。
- 模型能够根据失败测试实施一行最小修复。
- 修改后模型会再次运行测试，并依据真实退出码报告结果。
- 明确的只读用户任务没有产生写操作或命令执行。
- 工作区限制和临时目录隔离未发现异常。
- 本轮没有发现需要新增响应解析 fixture 的 provider 格式偏差。

## 基线暴露的问题

### 1. 极小任务的累计输入 token 较高

Bug 修复任务累计 39,453 input tokens，只读任务累计 18,213 input tokens。可能来源包括每轮重复发送系统提示、完整工具 schema、历史消息和 Responses output。后续应在 Eval 中持续记录，并在不破坏工具调用配对的前提下分析上下文成本。

### 2. 文件列表包含缓存目录

Run 1 在基线失败测试后生成了 `__pycache__`，随后 `list_files` 将其列入结果。虽然没有读取缓存文件，但这会污染真实仓库的项目概览。后续项目理解阶段应默认过滤缓存、虚拟环境、依赖和构建目录。

### 3. 事件生命周期不完整

当前日志包含模型和工具事件，但还缺少稳定的 `run_started`、`run_completed`、`run_failed`、持续时间、最终状态和验证状态。该问题将在 Session、任务状态和结构化事件阶段解决。

### 4. 当前证据规模仍有限

本记录只覆盖一个单文件修复和一个只读分析，不能代表复杂多文件任务的成功率。后续 Eval 必须加入中断恢复、Diff/Undo、跨文件修改、重试、安全拒绝和无关修改检查。

## 结论

当前基线已经在真实 aicode007 Responses 服务上证明了最小编程闭环和只读约束能力，没有发现阻塞 Session 实现的 provider 兼容性问题。下一阶段可以进入版本化 Session schema、原子 `SessionStore`、工具执行状态和 `--resume`。
