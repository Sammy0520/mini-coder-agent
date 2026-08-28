# v0.1.0 发布候选审计

日期：2026-08-28（Asia/Shanghai）

本记录验证公开仓库在不依赖开发目录、既有虚拟环境或本地凭据的情况下可安装并运行。审计不复制 API Key、`auth.json`、Session、事件日志或临时工作区。

## 审计基线

- 公开仓库：`https://github.com/Sammy0520/mini-coder-agent`
- 分支：`main`
- 复现提交：`1606419b20d8c7f972633dff5bee5c1735de2dc6`
- Python：3.12.13
- 本地复现系统：Windows
- 远程 CI：[GitHub Actions run 33156464072](https://github.com/Sammy0520/mini-coder-agent/actions/runs/33156464072)

## 全新克隆复现

从公开 GitHub URL 克隆到一次性目录后，按 README 创建新的虚拟环境并执行可编辑安装。该目录没有复用开发仓库的 `.venv`、构建产物、Eval 输出或本地认证文件。

验证结果：

| 检查 | 结果 |
|---|---:|
| 从公开 URL 克隆 `main` | 通过 |
| `python -m venv .venv` | 通过 |
| `python -m pip install -e .` | 通过 |
| `python -m pip check` | 通过，无损坏依赖 |
| `mini-coder --help` | 通过 |
| `mini-coder-eval --list` | 通过，列出 10 个场景 |
| 完整离线单元测试 | 115/115 通过 |
| 完整确定性 Eval | 10/10 通过 |
| 演示重置脚本 | 通过，并建立隔离 Git baseline |
| 演示初始失败状态 | 按设计复现：缺少 `shop.policy` 且边界行为失败 |

全新克隆没有配置真实 provider key，因此审计没有产生新的模型调用或费用。真实 Responses Eval 和两分钟演示沿用已经脱敏保存的阶段 H 证据。

## 跨平台 CI

最新公开 CI 在同一提交上完成以下四个组合：

| 系统 | Python | 结果 |
|---|---:|---:|
| Ubuntu | 3.11 | 通过 |
| Ubuntu | 3.12 | 通过 |
| Windows | 3.11 | 通过 |
| Windows | 3.12 | 通过 |

每个组合安装项目、检查依赖、运行 115 项离线测试和 10 项确定性 Eval、构建 wheel，并扫描候选文件中的凭据和私钥材料。

首轮 Windows CI 暴露了同一临时目录的长路径与 8.3 短路径表示差异。测试现先规范化预期路径再比较；CI 测试入口也会把失败用例和关键 traceback 写成 GitHub 注释。修复后四个组合全部通过。

## GitHub 公共页面

在已登录浏览器中按普通仓库访问路径检查公开页面，确认：

- 仓库为 Public，默认分支为 `main`。
- README 在仓库首页正常渲染。
- CI 徽章链接到公开 workflow，最新提交显示绿色检查状态。
- 架构、安装、Eval、真实运行证据、安全边界和已知限制可从首页直接阅读。
- README 中的仓库内证据链接解析到公开文件。
- 当前没有 tag 或 GitHub Release。
- About 区域当前没有 description、website 或 topics。
- 仓库当前没有许可证文件或 GitHub 识别的 license。

最后三项不会影响代码运行，但属于发布元数据。description/topics 可以在发布前补充；许可证需要由仓库所有者选择；`v0.1.0` tag 和 Release 只在所有者确认候选内容后创建。

## 结论

当前提交满足离线测试、确定性 Eval、真实模型证据、跨平台 CI、公开页面可见性和全新环境复现要求，可以作为 `v0.1.0` 发布候选。剩余工作是同步本次审计文档、决定仓库元数据和许可证，并由仓库所有者确认是否创建 tag/Release。
