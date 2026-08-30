# MiniCoderBench

MiniCoderBench compares Mini Coder and Codex with the same `aicode007` provider, `gpt-5.6-sol` model, reasoning effort, initial workspace, task text, timeout, and hidden validation.

The agents intentionally keep their own prompts, tools, context management, and verification loops: those are the systems being compared. Every run receives a fresh workspace copy with its own Git baseline, so Git-aware tools cannot discover the parent repository. Hidden tests are copied in only after the agent exits.

List tasks without spending quota:

```powershell
mini-coder-bench --list
```

Run one paired live comparison explicitly:

```powershell
mini-coder-bench --live --agent both --task boundary-fix --config agent.toml
```

The JSON report records provider usage when available. Reconcile `actual_cost` against the aicode007 panel; do not substitute OpenAI list prices. Separate API keys in the same aicode007 billing group are recommended when the provider supports them.

The first same-provider smoke test and its interpretation limits are recorded in [`docs/runs/cache-stable-benchmark-smoke.md`](../docs/runs/cache-stable-benchmark-smoke.md).
