# Claw-SWE-Bench Lite 预注册实验

## 研究问题

在完全相同的 `aicode007` Responses 接口、`gpt-5.6-sol`、`xhigh` 推理强度和 `high` 输出详细度下，Mini Coder 能否用更轻量的上下文与工具系统，在真实仓库问题上保留可接受的解决能力，同时降低未缓存输入、总费用或运行时间？

本实验不是 Claw-SWE-Bench Lite 官方排行榜成绩，而是从官方 Lite-80 中预注册的 16 题配对试验。主结果必须明确写成 “Claw-SWE-Bench Lite-80 的预注册 16 题 pilot”。

## 固定数据与抽样

- 数据集：`TokenRhythm/Claw-SWE-Bench`
- 固定提交：`ca9da7416154a31015f43df71dcf742c6725b312`
- Claw runner 固定提交：`fcece5f4c0817430ce953b52c80c931a40cd9b83`
- 官方 SWE-bench 评分器固定提交：`7a21e05772954cc81471ae19d56f436cecf43c54`（包版本 `5.0.2`）
- `lite80_ids.json` SHA-256：`09738eeb71e7fc4b2f2511da963c5cbd47b503a9cccba30cc561703d7003766f`
- `lite-test.parquet` SHA-256：`40fd4e1f9ac40c11c38ac68113b9b5b2026ae916a11d8ade39b40afd4adf0412`
- 公开种子：本仓库提交 `7725d67d9825b931a2c91b832eb2fff3d3995d2d`

抽样只使用 `instance_id` 和语言，不查看题面、测试补丁或标准补丁。每种语言的 10 个题目按固定 SHA-256 规则排序，选择前两题。第一题组成 Phase 1（8 题），第二题组成预先锁定的 Phase 2（8 题）。详细名单与先后顺序保存在 `benchmarks/claw_swe_bench/manifest.json`。

## 公平运行条件

- 两个 Agent 使用相同模型、接口、推理强度、输出详细度和任务原文。
- 每次只运行一个 Agent，`workers=1`，不并发调用，便于在供应商面板按时间核对费用。
- 每个 Agent 都从相同 `base_commit` 的全新官方 SWE-bench 容器开始。
- 不加载桌面 Codex 的历史、技能、MCP、项目记忆或已有会话；Mini Coder 也不恢复历史会话。
- Agent 不接触标准补丁和隐藏测试结果。补丁由 Claw runner 在 Agent 退出后统一收集。
- Codex 的审批与内部 sandbox 仅在一次性 SWE-bench Docker 容器内关闭；宿主机仍由 Docker 的文件系统、内存和进程限制隔离。这样避免嵌套 sandbox 不兼容，并与 Mini Coder 的容器内 `--auto` 条件一致。
- 每题最长 1800 秒。Mini Coder 的 300 步上限只是防失控保护；Codex CLI 没有完全等价的公开步数开关，因此主要公平停止条件是共同的墙钟时间。
- 每题成对运行。每个 Phase 有 4 题 Mini Coder 先运行、4 题 Codex 先运行，以减弱先后顺序和供应商缓存热度影响。
- 模型已经开始处理后的失败不重跑。只有可以证明发生在第一次模型请求之前的环境故障，才允许修复后重跑，并必须记录原因。

## 分阶段规则

1. 先完成不计分的环境 smoke；smoke 题不能来自预注册 16 题。
2. 运行 Phase 1 的 8 个配对题并独立报告结果。
3. Phase 2 的名单已经锁定，只有在预算允许时继续，不能根据 Phase 1 的输赢重新挑题。
4. 两个 Agent 在同一题上的运行应相邻进行，但严格串行。

## 主要指标

- 官方 harness 是否 `resolved`（首要指标）
- 空补丁、超时和运行失败率
- 墙钟时间、模型请求数、工具调用数、修改文件数
- 输入、缓存输入、输出和推理 token（以供应商实际返回字段为准）
- 供应商面板费用；若无法可靠归因则标记未知，不推算或补造

报告同时给出逐题配对结果和总体汇总。成功率差异样本很小，只作 pilot 证据，不宣称统计显著或全面优于 Codex。

## 无效结果

此前自定义任务和旧的跨文件测试不属于本预注册实验。任何未使用固定数据提交、相同模型配置、全新容器或规定运行顺序的结果，均不得混入主表。
