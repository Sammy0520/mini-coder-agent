# Stage H Eval and release validation

Date: 2026-08-28 (Asia/Shanghai)

This record contains no API key, raw provider payload, private prompt history, or
generated workspace. Live failures would retain redacted Session/event artifacts;
the successful live run did not need a retained workspace.

## Deterministic Eval suite

Command:

```text
mini-coder-eval --output <ignored-output-directory>
```

Result:

```text
10/10 passed
```

Covered scenarios:

| Scenario | Evidence |
|---|---|
| `boundary_bug` | Inclusive single-file boundary fix and passing verification |
| `multifile_interface` | Search, two-file public helper rename, passing verification |
| `failed_then_fix` | First verification failed, second edit and verification passed |
| `readonly_analysis` | Read-only answer with no file changes |
| `workspace_escape` | Real outside target existed; traversal tool call failed |
| `session_resume` | Step budget interrupted the first process; saved Session resumed and passed |
| `approval_denied` | Proposed write denied; original file remained unchanged |
| `undo_conflict` | External edit after tracked change; Undo refused and preserved it |
| `rate_limit_retry` | Injected retryable 429; exactly one bounded retry recovered |
| `long_output` | Command succeeded; output was truncated with diagnostics retained |

Every scenario starts from an immutable in-memory file declaration copied to a
fresh temporary directory. Scoring checks status, verification, exact changed
paths, expected content, unrelated changes, boundary behavior, tool/model counts,
retries, duration, Diff size, stop reason, and provider usage when available.

## Live provider Eval

Provider configuration: local `aicode007` provider entry, Responses wire protocol.
Credentials came from ignored local configuration and were not copied into the
report.

Scenario: `multifile_interface`

```text
Result                         PASS
Session status                 completed_verified
Stop reason                    model_completed_verified
Changed paths                  catalog.py, service.py
Unrelated changes              0
Workspace boundary violations  0
External validation            passed
Model calls                    4
Tool calls                     9
Retries                        0
Failed tool calls              0
Invalid tool calls             0
Diff                           +3/-3
Duration                       46.81s
Input tokens                   29,506
Output tokens                  1,025
Total tokens                   30,531
```

The real model chose its own tool sequence. The Eval runner independently checked
the two expected files and reran the test command after the Agent completed.

## Live two-minute demonstration

The real provider also completed the resettable `order_service` fixture:

```text
Session status          completed_verified
Changed implementation shop/policy.py, shop/pricing.py, shop/service.py
Tests                   5/5 passed
Tests modified          no (SHA-256 comparison with fixture)
Duration                119.50s
Model calls             8
Tool calls              24
Retries                 0
Invalid tool calls      0
Total tokens            89,541
```

One exploratory empty-query search was rejected locally and two overlapping-read
hints were recorded; neither caused an unsafe operation or incorrect result. An
independent post-run command again passed all five tests. The reset command was
then used to restore the initial failing state.

## Multi-file demonstration fixture

`examples/order_service/fixture` is an immutable small order-service project. A
reset command creates an ignored disposable workspace and an independent Git
baseline. Its initial tests have one
expected import error (`shop.policy` is intentionally absent) and one expected
boundary failure, while unrelated behavior passes. The task requires introducing
a policy dataclass and updating the pricing/service call chain without changing
the public service API or tests.

## CI and release checks

The final local regression suite completed **115/115** standard-library tests,
and a separate CLI invocation completed **10/10** deterministic Evals. Source
compilation, dependency consistency, patch whitespace, and candidate-file secret
checks also passed.

The CI matrix declares Ubuntu and Windows jobs for Python 3.11 and 3.12. Each job:

1. installs the project in editable mode;
2. runs dependency checks and the complete offline unit-test suite;
3. runs all ten deterministic Evals without API credentials;
4. builds a wheel without runtime dependencies in an isolated PEP 517 build; and
5. scans tracked/candidate files for forbidden credential files, long OpenAI-style
   keys, and private-key material.

Live Evals are intentionally absent from CI and require both `--live` and an
explicit `--scenario` locally.

The package also built as `mini_coder_agent-0.1.0-py3-none-any.whl` in an
isolated PEP 517 environment. That wheel was installed with `--no-deps` into a
new virtual environment, where the installed `mini-coder-eval` entry point
listed all scenarios and independently passed `boundary_bug`. This confirms the
verification did not depend on the repository's editable installation.
