# Verification-aware completion live run

Date: 2026-08-27 (Asia/Shanghai)

This is a redacted record of a real Responses API run against the configured
`aicode007` provider with model `gpt-5.6-sol`. The disposable fixture and its
Session are under the Git-ignored `tmp/` directory. No API key, raw request,
response payload, or credential file is included here.

## Fixture and failing baseline

The fixture contained a five-line `quota.py`, four `unittest` cases, and a short
README defining the acceptance rule: requests must be positive and must not
exceed remaining capacity.

Independent baseline command:

```text
python -m unittest discover -s tmp/live-stage-e-eval-001 -v
```

Baseline result:

```text
Ran 4 tests
FAILED (failures=2)
```

The two failures covered a zero request being accepted and an exact-capacity
request being rejected.

## Real agent run

The agent was asked to inspect the fixture, make the smallest fix, and run the
project's complete test suite. It performed six model turns and seven tool
calls:

1. Listed the project and read the implementation, tests, and README.
2. Ran `python -m unittest` with `purpose=verify`; the real command exited 1.
3. Edited `quota.py` (`+2/-2`).
4. The runtime advanced the change revision and marked the failed pre-change
   verification stale.
5. Ran `python -m unittest` again; the real command exited 0.
6. Returned a final summary.

Persisted Session facts immediately after completion:

```text
schema_version: 3
status: completed_verified
phase: summarize
verification_status: passed
change_revision: 1
changes: 1
verification records: 2 (stale exit 1, current exit 0)
stop_reason: model_completed_verified
model calls: 6
tool calls: 7
retries: 0
total duration: 45.73s
usage: input 40601, output 891, total 41492 tokens
```

This demonstrates that the final state did not come from the model's prose. It
was derived from the latest real verification record for the current file
revision. The final report also included the tracked Diff statistics, both
validation attempts, the stale reason, unresolved items, and run statistics.

## Independent post-run verification

The test suite was then run independently outside the agent loop:

```text
Ran 4 tests in 0.000s
OK
```

The repository's offline suite also passed after the Stage E implementation:

```text
Ran 77 tests
OK
```

## Remaining boundary

Stage E now records local phases, change revisions, validation facts, denial,
and verification-aware completion. Time/model/tool/token budget enforcement,
API retry accounting, stronger command-risk classification, and centralized
redaction remain Stage F work. `run_command` is still a normal host shell
process rather than an operating-system sandbox.
