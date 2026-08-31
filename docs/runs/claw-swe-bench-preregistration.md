# SWE-bench Verified Easy 预注册实验

## 研究问题

在相同的 aicode007 Responses 接口、模型和推理强度下，Mini Coder 能否在定义清楚、
规模适中的真实仓库任务上，以更轻量的上下文和工具系统取得有竞争力的成功率与成本？

本实验是 SWE-bench Verified Easy 的预注册 16 题 pilot，不是完整排行榜成绩。

## 固定数据与抽样

- 数据集：`princeton-nlp/SWE-bench_Verified`
- 固定提交：`c104f840cc67f8b6eec6f759ebc8b2693d585d4a`
- 固定 parquet SHA-256：`a45b1fe4e2f0c8390b2b2938ac83e92ed5979000856808f3679c07812e9e6dcd`
- 难度：`<15 min fix`，固定版本内共 194 题
- Claw runner：`fcece5f4c0817430ce953b52c80c931a40cd9b83`
- 官方 SWE-bench 评分器：`726c5461e2ef52d83cf1ea2107870a8bb3328d57`（v4.1.0）
- 公开种子：本仓库提交 `7725d67d9825b931a2c91b832eb2fff3d3995d2d`

抽样只读取 `instance_id`、仓库和公开难度，不使用题面、测试或标准补丁。每个 Phase
先以固定 SHA-256 规则排列仓库，再从八个不同仓库各选一个未使用任务；另一个独立
哈希决定运行顺序。详细名单固定在 `benchmarks/claw_swe_bench/manifest.json`。

## 公平运行条件

- 两个 Agent 使用相同任务、模型、接口、推理强度和输出详细度。
- 每题从相同 `base_commit` 的全新容器开始，严格串行运行。
- Agent 不接触标准补丁和隐藏测试结果。
- 容器无公网出口，只允许访问共同的模型 API；外网访问尝试使该轮无效。
- 每个 pair 前检查余额、认证和连接；基础设施失败不计入 Agent 失败。
- 每题最长 1800 秒。Mini Coder 默认限制为 12 次模型调用、60 次工具调用和
  120,000 个供应商报告 token，报告必须单列因预算停止的任务。
- 每个 Phase 有四题 Mini Coder 先运行、四题 Codex 先运行。
- 模型开始处理后的普通失败不重跑；仅明确的基础设施失败允许恢复。

## 阶段与指标

Phase 1 和 Phase 2 各 8 题；Phase 2 已提前锁定，不能根据 Phase 1 结果换题。首要
指标是官方 harness 的 `resolved`，同时报告空补丁、超时、墙钟时间、模型调用、工具
调用、供应商 token 和可可靠归因的实际费用。样本量较小，只作为成本—能力 pilot，
不宣称统计显著或全面优于 Codex。

2026-08-31 的 Claw-SWE-Bench Lite 运行因余额耗尽和答案污染作废，相关结果只能作为
历史诊断，不能混入本 Easy pilot。
