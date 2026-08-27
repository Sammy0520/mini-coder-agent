# 多文件修改、测试与逆序 Undo 综合验收

本记录保存一次接近评审现场的真实模型综合演练。项目完全由本地临时生成，只包含一个虚构 checkout 业务、4 项单元测试和说明文档；不存在仓库源代码、用户数据、个人信息或凭据。原始 Session、Responses `encrypted_content` 和 JSONL 日志位于被 Git 忽略的 `tmp/`，不提交仓库。

## 环境

- 日期：2026-08-27
- Provider：`aicode007`
- Model：`gpt-5.6-sol`
- Wire API：Responses
- Session schema：2
- Session ID：`b9e7784aca0e479391235c9179c9fdc3`
- 审批模式：`auto`，仅用于可丢弃合成目录

项目文件：

```text
README.md
pricing.py
checkout.py
test_checkout.py
```

任务要求 `PricingPolicy` 持有默认 5,000 分免邮阈值，免邮资格以会员折扣后的金额判断，未达阈值时继续收取 500 分运费，并保持负金额异常；禁止修改 README 和测试，必须运行完整 unittest。

## 初始失败基线

真实 Agent 启动前独立运行：

```text
python -m unittest -v
```

结果：

```text
Ran 4 tests
FAILED (failures=2, errors=1)
```

- 2 项失败：实现始终收取运费。
- 1 项错误：`PricingPolicy` 缺少 `free_shipping_threshold_cents`。
- 1 项通过：负金额仍抛出 `ValueError`。

## 真实 Agent 闭环

真实模型共运行 5 步：

1. `list_files`
2. 并行读取 README、两个实现文件和测试
3. 修改 `pricing.py` 与 `checkout.py`
4. 执行 `python -m unittest -v`
5. 汇总修改与真实测试结果

ChangeTracker 保存两项变更：

```text
pricing.py   +1/-0
checkout.py  +2/-0
```

第一项给 `PricingPolicy` 增加默认 `free_shipping_threshold_cents = 5_000`；第二项在 `checkout_total` 中以折扣后金额比较策略阈值。两个 Change ID 均与各自 Tool Execution ID 正确关联，Diff 未截断，README 和测试没有 ChangeRecord。

Agent 内部执行的 `run_command`：

```text
command: python -m unittest -v
exit_code: 0
Ran 4 tests
OK
```

Agent 退出后再次从外部独立运行同一命令，仍然是 4/4 通过，排除了只相信模型总结或工具消息的情况。

Session 终态：

```text
status: completed_unverified
current_step: 5
stop_reason: model_completed
changes: 2
active changes: 2
run_command ok: true
run_command exit_code: 0
```

当前仍为 `completed_unverified` 是阶段 E 之前的已知限制：尽管 Session 中已有真实退出码，本地尚未把命令事实提升为正式验证状态。

Provider usage：

```text
input_tokens   35,176
output_tokens     940
total_tokens   36,116
```

## 逆序 Undo 与行为复测

第一次执行本地 `--undo-last`：

- 只恢复最后修改的 `checkout.py`。
- `checkout.py=undone`，`pricing.py=active`。
- Session UndoRecord 数量变为 1。
- 重跑测试：3 项失败，退出码 1。

第二次执行 `--undo-last`：

- 恢复 `pricing.py`。
- 两项 ChangeRecord 都是 `undone`。
- Session UndoRecord 数量变为 2。
- Undo 事件顺序为 `checkout.py, pricing.py`。
- 两个实现文件 SHA-256 都精确等于各自 ChangeRecord 的 `before_hash`。
- 重跑测试回到初始基线：2 项失败、1 项错误、退出码 1。

这证明 Undo 改变的是磁盘上的真实程序行为，不只是 Session 标记。

## 真实使用暴露并修复的问题

首次 `list_files` 把 `.mini-coder/sessions` 和 `__pycache__` 展示给了模型。虽然本轮没有读取 Session 文件，但 Agent 内部状态和 Python 缓存不应进入模型上下文。

修复后 `WorkspacePolicy` 明确拒绝：

```text
.mini-coder/
__pycache__/
```

并增加回归测试，确认列表隐藏这两类目录，直接读取也被路径策略拒绝。

## 凭据扫描

Responses 的 opaque `encrypted_content` 是 URL-safe 随机密文，可能偶然包含类似 `sk-` 的字节片段；它不是可解释消息或凭据，但为跨轮 reasoning replay 必须保存在被忽略的 Session 中。因此扫描时先结构化排除 `encrypted_content` 字段，再检查全部可解释 Session 与日志文本。

扫描结果：

```text
小写 sk- 凭据形态         0
Bearer 凭据形态          0
Authorization 赋值形态   0
```

原始 Session、密文和日志没有提交。

## 结论与阶段 E 基线

综合验收确认了真实多文件链路：失败基线、代码定位、两文件 Diff、原子写入、实际测试、独立复测、Session 关联、逆序 Undo、行为回退和字节级恢复。它也明确暴露阶段 E 的目标：已有成功命令事实不应继续停留在 `completed_unverified`，Undo 或后续修改必须使先前验证失效，最终报告必须由本地验证记录而非模型文本决定。
