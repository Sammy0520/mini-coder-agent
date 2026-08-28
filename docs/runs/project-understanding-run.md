# 阶段 G：项目理解与工具效率真实验收

本记录保存阶段 G 的脱敏多文件验收结果。原始 Session、事件日志和 API 凭据不提交仓库。

## 演示项目

隔离项目包含：

```text
AGENTS.md
pyproject.toml
notes.txt
shop/
  __init__.py
  cli.py
  pricing.py
  service.py
tests/
  __init__.py
  test_pricing.py
node_modules/...
build/...
```

项目是独立 Git 仓库。提交基线后，`notes.txt` 被用户预先修改但未提交；Agent 启动前保留该状态。`pricing.py` 把免运费阈值错误实现为 `subtotal > 50`，导致边界与服务集成测试失败。

初始验证：

```text
Ran 4 tests
FAILED (failures=2)
```

## 启动概览

有界发现扫描 16 个条目、跳过 8 个内部/依赖/构建条目，没有截断。它在模型调用前准确给出：

- 清单：`pyproject.toml`
- 入口：`shop/cli.py`
- 测试：`tests/`、`tests/test_pricing.py`
- 验证候选：`python -m unittest discover -v`
- 项目说明：根目录 `AGENTS.md`
- Git 分支：`master`
- 任务开始前已有 Git 改动：`notes.txt`，共 1 项

`node_modules`、`build`、`__pycache__`、`.git` 和 `.mini-coder` 均未进入模型概览或 Session 可解释消息。

## 真实 Responses 运行

- Provider：aicode007
- Model：`gpt-5.6-sol`
- Wire API：Responses
- Session schema：v5
- 最终状态：`completed_verified`

模型没有再次列举根目录。第一轮直接读取项目说明、清单、测试，并只列举概览指出的 `shop` 子树；随后读取相关实现，精确修改 `shop/pricing.py` 一行：

```diff
-    if vip or subtotal > 50:
+    if vip or subtotal >= 50:
```

模型没有修改测试或 `notes.txt`。验证命令退出码为 0，Agent 退出后独立复测仍为：

```text
Ran 4 tests
OK
```

## 效率与归属

| 指标 | 结果 |
|---|---:|
| 模型请求 | 5 |
| 工具调用 | 10 |
| 失败工具 | 0 |
| 非法工具 | 0 |
| 重复读取提示 | 0 |
| 重试 | 0 |
| input tokens | 38,521 |
| output tokens | 844 |
| total tokens | 39,365 |
| 总耗时 | 52.69 秒 |

阶段 B 的单文件修复使用 7 次工具调用；阶段 G 项目文件更多，因此总读取数不宜直接比较，但两者失败/非法调用均为 0。阶段 G 没有无意义的根目录重复扫描，没有探测错误测试 runner，也没有读取依赖或构建产物。因此“无效工具调用不增加”的验收成立。

最终报告把 `notes.txt` 明确列为任务开始前已有改动，只把 `shop/pricing.py` 的 ChangeTracker 记录归因给 Agent。事件日志在工作区内创建，因此被诚实列为任务期间出现的非托管进程文件；它没有被归因成代码修改。

## 离线回归

完整测试集扩展到 106 项，覆盖：

- 有界项目发现和说明文件优先级。
- 清单、入口、测试和验证命令识别。
- 依赖、缓存和构建目录剪枝。
- 文件列表、搜索和读取的继续元数据。
- 搜索过滤原因和扫描上限。
- Git 起始修改、文件指纹和非托管变化比较。
- v1～v4 Session 向 v5 迁移。
- 失败/非法工具与重复读取指标。
- 结构化工具错误码和修复建议。

## 结论

阶段 G 已把“让模型自己盲扫仓库”改成“本地先提供有界证据，再让模型定向读取”。真实多文件运行能够定位入口、遵守项目说明、选择正确测试命令、保护用户原有改动，并保持无效工具调用为零。
