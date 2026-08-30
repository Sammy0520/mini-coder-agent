# Cache-stable context and paired benchmark smoke test

Date: 2026-08-30 (Asia/Shanghai)

This record contains no API key, raw prompt transcript, provider payload, or
generated workspace. It validates cache reporting and the same-provider runner;
it is not a formal Mini Coder versus Codex result.

## Locked comparison

Both agents were configured with the same local credential and the following
comparison manifest:

```text
Provider       aicode007
Base URL       https://api.aicode007.com
Wire API       Responses
Model          gpt-5.6-sol
Reasoning      xhigh
Verbosity      high
Task           boundary-fix
```

The runner copies the fixture into a separate workspace for each agent, creates
an independent Git baseline inside that workspace, and injects hidden validation
only after the agent process exits. Actual cost remains empty until the run is
matched to the aicode007 billing panel.

## Mini Coder smoke result

The Mini Coder run completed the task, changed only `pricing.py`, and passed the
hidden validation.

```text
Duration                    36.83s
User turns                  1
Provider calls              4
Tool calls                  4
Input tokens                33,038
Cached input tokens         27,648
Uncached input tokens        5,390
Cache reuse                 83.69%
Output tokens                  628
Reasoning tokens                151
Provider model              gpt-5.6-sol
```

This is the first real-provider evidence after moving changing progress state to
the end of each request and keeping the working history append-only until the
context approaches its budget. It demonstrates that the provider returns usable
cache metadata and that Mini Coder can preserve a highly reusable prefix on this
task. One task is not enough to establish an average cache rate.

## Codex authentication smoke result

Codex used the same project-local credential, completed the task, changed only
`pricing.py`, and passed the same hidden validation. Its reported cache reuse was
70.53%. The JSON stream contained eight unique tool item IDs; the first report
counted both start and completion events and displayed 16, which the runner now
deduplicates.

This Codex process started before the runner added a local Git repository to each
workspace. It could therefore discover the parent repository while inspecting
Git state. The result proves that the aicode007 Responses/authentication path and
Codex JSON ingestion work, but its time and token totals are contaminated and
must not be compared with the Mini Coder numbers above.

## Fixes made after the smoke run

- both child processes receive the same ignored project-local API key through
  their environment, never through command arguments or reports;
- each workspace now receives its own Git repository and baseline commit before
  either agent starts;
- Codex tool events are counted once per stable item ID;
- underlying Codex provider call count remains `unknown` because the CLI JSON
  reports aggregate turn usage rather than every provider request;
- hidden files remain unavailable until the agent has exited.

## Next valid comparison

After reconciling the smoke timestamps with the aicode007 panel, run a fresh
paired task with the corrected harness:

```powershell
python -m mini_coder.benchmarks.cli --live --agent both --task boundary-fix --config agent.toml --output benchmark-results\paired-boundary-fix
```

The full five-task comparison should only be run after this one pair confirms
that local usage fields and aicode007 billing rows can be matched reliably.
