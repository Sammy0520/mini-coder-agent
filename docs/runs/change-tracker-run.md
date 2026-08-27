# ChangeTracker、Diff 与 Undo 真实验收记录

本记录保存一次经过脱敏的真实模型阶段 D 验收结果。模型只接触专门生成的合成临时夹具，不使用仓库源代码、用户文件、个人信息或凭据。原始 Session 和 JSONL 日志位于被 Git 忽略的 `tmp/`，不提交仓库。

## 运行环境

- 日期：2026-08-27
- Provider：`aicode007`
- Model：`gpt-5.6-sol`
- Wire API：Responses
- Session schema：2
- Session ID：`e49ca7e645eb4fbfb79370b70dd18258`
- 审批模式：`auto`，仅用于可丢弃的合成目录

临时工作区只包含两行虚构配置：一行说明注释和 `TIMEOUT_SECONDS = 10`。任务要求读取文件、用 `edit_file` 将数值改为 15、禁止运行命令并总结准确修改。

## Agent 闭环

真实模型共运行 4 步：

1. `list_files`
2. `read_file settings.py`
3. `edit_file`，精确替换 `TIMEOUT_SECONDS = 10`
4. 自然语言总结

写入前 CLI 显示了 ChangeTracker 生成的完整预览：

```diff
--- a/settings.py
+++ b/settings.py
@@ -1,2 +1,2 @@
 # Synthetic disposable fixture for ChangeTracker validation.
-TIMEOUT_SECONDS = 10
+TIMEOUT_SECONDS = 15
```

本地统计为 `+1/-1`，Diff 未截断。初始和修改后 SHA-256 分别为：

```text
before  2f6cace9a6485fd35840c0332b7a4260c150f7e3385b025cd2b0f8205d45776c
after   babe046ae87c4f32a8dc8d9612a19a4507eb0c66dab34b4f6a48cc0fb89bcdf2
```

Session 中保存 1 条 ChangeRecord；`edit_file` 工具执行记录的 `change_id` 与该 ChangeRecord 一致。最终本地报告包含：

```text
Local change summary:
- settings.py: 1 change(s), +1/-1; current hash matches
```

模型自然语言总结自行生成了一个不准确的 Windows 文件链接。这不会改变文件或 Session 事实，也进一步说明变更路径、hash、Diff 和最终统计应以本地 ChangeTracker 为准，而不能只相信模型文本。

累计 provider usage：

- input tokens：23,711
- output tokens：290
- total tokens：24,001

## 离线查看与 Undo

真实运行完成后，使用同一个 Session 执行本地命令：

```powershell
mini-coder --resume "<session-file>" --show-changes
mini-coder --resume "<session-file>" --undo-last --log "<undo-log>"
```

两个操作都没有加载模型或要求 API Key。`--show-changes` 显示完整路径、状态、增删统计、Change ID、Tool Execution ID、两个 hash 和 unified diff。

`--undo-last` 完成后：

- 文件 SHA-256 恢复为原始 `2f6cace9...d45776c`。
- ChangeRecord 从 `active` 变为 `undone` 并记录 `undone_at`。
- Session 增加 1 条 UndoRecord。
- UndoRecord 的 `restored_hash` 等于原始 `before_hash`。
- JSONL 写入 `change_undone`，事件中的 Change ID 与 Session 一致。

## 自动化与安全检查

阶段 D 加入后，完整离线测试为 66 项并全部通过。覆盖新文件、空文件、普通修改、多行删除、Diff 截断、审批拒绝、原子替换失败、批准窗口冲突、恢复已批准修改、CRLF、UTF-8 BOM、二进制/大文件/符号链接策略、单次 Undo、多次逆序 Undo、新文件 Undo 和 Undo 冲突。

对原始 Session、运行日志和 Undo 日志执行大小写敏感扫描：

- 小写 `sk-` 凭据形态：0
- `Bearer` 凭据形态：0
- Authorization 请求头赋值形态：0

一次不区分大小写的初始扫描曾匹配 provider `encrypted_content` 中的随机大写 `Sk-` 片段；定位字段和匹配性质后确认是密文误报。原始密文和日志继续由 `.gitignore` 排除。

## 结论

这次验收确认：真实模型的 `edit_file` 会进入 ChangeTracker；写入前 Diff、hash 和统计可见；成功变更与工具执行关联并进入 Session；最终本地汇总会检查磁盘 hash；变更历史可在离线模式查看；安全 Undo 能恢复原始字节并将操作记录到 Session 与事件日志。
