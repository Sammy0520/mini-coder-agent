# Claw-SWE-Bench Phase 1 无效 pilot 记录

## 结论

2026-08-31 的 `minicoder-claw-lite-v1-phase1` 不构成 Mini Coder 与 Codex 的有效
能力比较，不得计入正式成功率。原始产物保留用于审计，正式重跑使用新的 run prefix。

## 发生了什么

- 8 个预注册 pair 均被调度，但只有第 1 个 Django 实例真正获得模型响应。
- Mini Coder 在 Django 上运行 14 次模型调用和 71 次工具调用，供应商报告总 token
  为 253,613；达到 240,000 token 预算后停止，未生成补丁。
- Codex 在同一题生成非空补丁，但执行过程中使用 GitHub Search API 和
  `raw.githubusercontent.com` 定位并读取参考修复，因此结果存在答案泄漏。
- 随后供应商余额耗尽。剩余 7 个实例上两个 Agent 的首次请求均返回 HTTP 403
  `billing_error`，这些记录是基础设施错误，不是 14 次解题失败。
- 评分器将包含全部 8 条记录的预测文件分别交给 7 条 Multilingual 子集和 1 条
  Verified 子集，官方 harness 因预测 ID 不属于当前数据集而拒绝评分。

## 修复后的门槛

- 评分前按 Agent、数据来源和实例 ID 生成精确预测文件。
- pair 开始前检查模型接口余额/认证；基础设施错误立即停止。
- Agent 容器默认无公网出口，只能连接共同模型 API；外网命令另行审计。
- Mini Coder pilot 预算收紧，并明确记录预算终止，不与普通未解决混为一类。
- 旧 Lite 题目不再作为主实验重跑；后续使用独立预注册的 SWE-bench Verified Easy
  清单和新 run prefix，不能与本轮产物合并。
