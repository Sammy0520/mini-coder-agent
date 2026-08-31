# Claw-SWE-Bench excluded smoke result

本记录只验证 Mini Coder 与原生 Codex 的外部 benchmark 全链路，不属于预注册
16 题，也不计入 Phase 1 成功率。

## 条件

- 实例：`django__django-11790`（来自 Verified-mini）
- 两边均使用 `gpt-5.6-sol`、`xhigh`、`high`，通过同一 aicode007 接口串行运行
- 推理结果来自已有的 Mini v3 与 Codex v4，评分阶段不再调用模型
- 固定 Claw parquet SHA-256：`40fd4e1f9ac40c11c38ac68113b9b5b2026ae916a11d8ade39b40afd4adf0412`
- 官方评分器：SWE-bench `v4.1.0`，提交 `726c5461e2ef52d83cf1ea2107870a8bb3328d57`
- 官方实例镜像：`swebench/sweb.eval.x86_64.django_1776_django-11790:latest`

## 结果

| Agent | 官方结论 | resolved | unresolved | error | 评分耗时 |
|---|---:|---:|---:|---:|---:|
| Mini Coder v3 | 通过 | 1 | 0 | 0 | 24.42 秒 |
| Codex v4 | 通过 | 1 | 0 | 0 | 22.06 秒 |

两份报告的 `resolved_ids` 都只有 `django__django-11790`。Mini 的本地依赖检查
曾受镜像环境影响，但官方隐藏测试最终通过；这也说明 Agent 自己报告的“完成”或
“本地验证通过”都不能替代 benchmark 验收。

此前两轮尝试分别暴露了评分适配与网络问题：第一次把旧 parquet 直接交给
SWE-bench 5.0.2，因数据结构不兼容而在测试前退出；第二次尝试在线加载数据集，
因 Hugging Face 网络不可达而退出。这些都按基础设施失败记录，未计为
`unresolved`。最终结果使用固定数据和离线官方 v4.1.0 harness 产生。

这是一题 smoke，只能证明端到端实验可运行，不能据此声称 Mini Coder 整体优于
Codex。正式结论必须来自预注册 Phase 1 的成对结果。
