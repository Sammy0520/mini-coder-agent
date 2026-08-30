# Claw-SWE-Bench Lite pilot

这里保存 Mini Coder 与原生 Codex 的预注册外部基准实验。它使用官方 Lite-80 的固定版本，抽取 16 题组成两阶段 pilot；不会把题面、测试补丁或标准补丁提交到本仓库。

## 已固定内容

- `manifest.json`：16 题名单、阶段、成对顺序和先运行的 Agent
- `preregister.py`：只根据公开 ID 和语言复现抽样
- `run_experiment.py`：严格串行运行，并校验数据与 runner 的 SHA-256/提交号
- `adapters.py`：把 Mini Coder 和官方 Codex CLI 接入 Claw runner
- 完整实验约束见 `docs/runs/claw-swe-bench-preregistration.md`

## Ubuntu 运行

从项目根目录执行。下面的 dry-run 和 preflight 都不会调用模型：

```bash
PY=/home/sammy/minicoder-eval/runtime-env/bin/python
CLAW=/home/sammy/minicoder-eval/claw-swe-bench
DATA="/mnt/d/Code Agent/tmp/claw-dataset-cache/snapshot/data/lite-test.parquet"

$PY -m benchmarks.claw_swe_bench.run_experiment \
  --claw-root "$CLAW" --parquet "$DATA" \
  --phase phase1 --agent both --dry-run

$PY -m benchmarks.claw_swe_bench.run_experiment \
  --claw-root "$CLAW" --parquet "$DATA" \
  --phase phase1 --agent both --preflight
```

实例镜像较大，可以先查看，再每次只拉一个；已存在的镜像会自动跳过：

```bash
$PY -m benchmarks.claw_swe_bench.pull_images --phase phase1 --dry-run
$PY -m benchmarks.claw_swe_bench.pull_images --phase phase1 --limit 1
```

当前 Windows 代理为本机端口时，WSL 下载与容器访问使用不同地址：

```bash
export HTTP_PROXY=http://172.31.0.1:7897
export HTTPS_PROXY=http://172.31.0.1:7897
export CLAW_AGENT_PROXY=http://host.docker.internal:7897
export SWEBENCH_VENV=/home/sammy/minicoder-eval/swe-bench-env
export SWEBENCH_WORK_DIR=/home/sammy/minicoder-eval/swe-bench-work
```

正式运行前还需要让 `OPENAI_API_KEY` 在 Ubuntu 进程环境中可用，或用 `--auth-file` 指向本地、已被 Git 忽略的 `auth.json`。运行器不会把密钥写入命令参数或结果文件。

正式 Phase 1 命令与 dry-run 相同，只需去掉 `--dry-run`/`--preflight`。默认模型为 `gpt-5.6-sol`，推理强度 `xhigh`，详细度 `high`，单题 1800 秒。不要并行启动另一份 Mini Coder 或 Codex 测试。

全部 Phase 1 补丁生成后，用固定 parquet 通过官方 SWE-bench 评分器打分；下面先检查评分计划：

```bash
$PY -m benchmarks.claw_swe_bench.evaluate_experiment \
  --claw-root "$CLAW" --parquet "$DATA" \
  --phase phase1 --agent both --dry-run
```

确认预测文件齐全后去掉 `--dry-run`。评分脚本会从固定 parquet 生成临时 JSON，分别评分 Multilingual 与 Verified-mini；临时文件和标准补丁只留在 Claw 的未跟踪 artifacts 目录中。
