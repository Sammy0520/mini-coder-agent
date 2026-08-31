# SWE-bench Verified Easy pilot

这里保存 Mini Coder 与原生 Codex 的预注册外部基准实验。当前主实验只使用官方
SWE-bench Verified 中难度标注为 `<15 min fix` 的任务；固定总体共有 194 题，按公开
SHA-256 规则选出 16 题，Phase 1 和 Phase 2 各 8 题。

`manifest.json` 固定任务、顺序和先运行的 Agent；`preregister.py` 复现抽样；
`run_experiment.py` 严格串行运行；`evaluate_experiment.py` 使用官方 SWE-bench harness
评分。Agent 容器没有公网出口，只能连接共同的模型接口。

## Ubuntu 运行

```bash
PY=/home/sammy/minicoder-eval/runtime-env/bin/python
CLAW=/home/sammy/minicoder-eval/claw-swe-bench
DATA="/mnt/d/Code Agent/tmp/swe-bench-verified-easy/test.parquet"

$PY -m benchmarks.claw_swe_bench.preregister \
  --source "$DATA" --output benchmarks/claw_swe_bench/manifest.json --check

$PY -m benchmarks.claw_swe_bench.run_experiment \
  --claw-root "$CLAW" --parquet "$DATA" \
  --phase phase1 --agent both --dry-run

$PY -m benchmarks.claw_swe_bench.run_experiment \
  --claw-root "$CLAW" --parquet "$DATA" \
  --phase phase1 --agent both --preflight
```

正式运行时去掉 `--dry-run`/`--preflight`，并用 `--auth-file` 指向已被 Git 忽略的
`auth.json`。也可以用 `run_overnight.sh` 顺序执行 Phase 1。默认 run prefix 是
`minicoder-swe-verified-easy-v1`；每个 pair 开始前会检查余额和认证，失败不会计为
Agent 解题失败。

实例镜像可以提前按顺序下载：

```bash
$PY -m benchmarks.claw_swe_bench.pull_images --phase phase1 --dry-run
$PY -m benchmarks.claw_swe_bench.pull_images --phase phase1 --limit 1
```

补丁生成完成后，用固定数据和官方 SWE-bench v4.1.0 评分：

```bash
$PY -m benchmarks.claw_swe_bench.evaluate_experiment \
  --claw-root "$CLAW" --parquet "$DATA" \
  --phase phase1 --agent both --dry-run
```

Mini Coder 的 Easy pilot 默认上限为 12 次模型调用、60 次工具调用和 120,000 个供应商
报告 token；可以用 `CLAW_MINI_MAX_*` 调整，但必须随结果公开。最终是否完成只以官方
harness 的 `resolved` 为准。完整规则见
`docs/runs/claw-swe-bench-preregistration.md`。

旧 Claw-SWE-Bench Lite smoke 和第一次无效 Phase 1 仅作为历史诊断，不进入本实验。
