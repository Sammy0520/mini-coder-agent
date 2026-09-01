# Cache-Stable Context Compression V2

Status: implemented behind a default-off feature flag; provider validation pending

Last updated: 2026-09-01

## 1. Purpose

Mini Coder needs bounded context without turning every late model request into a new cold prompt. The current implementation preserves append-only history while it is below a soft limit, then derives a compacted view on every later request. That bounds tokens, but recent real runs show that ordinary seven-to-eleven-call coding tasks spend roughly the final 43–45% of their main-agent requests in the compacted path. Cache reuse becomes unstable in the same region.

This document specifies and records the second-generation compression design. The local engine, session persistence, main-agent integration, subagent limits, and content-free diagnostics are implemented behind `context_compression_v2_enabled = false`. The existing ContextManager path remains the default rollback until controlled provider probes justify enabling V2.

### 1.1 Implementation snapshot

- Session schema v10 persists an exact frozen checkpoint and content-free request fingerprints.
- Tool-schema tokens participate in high/low-watermark capacity accounting.
- Main and child agents have separate watermark, hot-tail, and checkpoint-interval settings.
- New conversation turns explicitly discard the prior turn's checkpoint and rebuild from compact durable turn state.
- Checkpoint creation emits `context_checkpoint_committed`; every model-call record includes generation, checkpoint hash, compaction reason, and local prefix diagnostics.
- Deterministic unit and integration tests pass; live provider cost/cache probes remain intentionally separate from correctness evidence.

## 2. Evidence motivating the change

The formal DKCodex A/B run at `D:\MiniCoder-E2E\formal-ab-20260901-192219` used `gpt-5.6-terra`, Responses streaming, and reasoning effort `high`.

### 2.1 Main-agent compaction frequency

| Run | Main model calls | Requests using a compacted view | Share |
| --- | ---: | ---: | ---: |
| With subagents | 11 | 5 (steps 7–11) | 45.5% |
| Without subagents | 7 | 3 (steps 5–7) | 42.9% |

The current main-agent limits are 20,000 estimated message tokens with a soft limit ratio of 0.8, so the append-only region ends near 16,000 estimated message tokens. The current subagent limit is 8,000 tokens, giving a soft boundary near 6,400 tokens.

### 2.2 Cache behavior around the boundary

Before the compacted region, individual requests reached 58–83% reported cache reuse. Once the request reached or crossed the current boundary, several requests reported zero cached tokens. This correlation does not prove that every miss was caused locally: a compatible provider can also miss because of routing, eviction, TTL, model changes, or cache-key handling. It does establish that Mini Coder currently changes the model-visible history during exactly the part of a run where cache reuse becomes unstable.

### 2.3 Cold and warm runs must not be confused

An earlier aicode007 two-turn run reported 31.3% aggregate reuse on its cold first turn and 85.5% on its warm second turn. The formal DKCodex A/B used new, separate cache keys, so both conditions were cold. The new design must therefore expose enough local evidence to distinguish a local prefix rewrite from a provider-side cache miss; cached-token percentages alone are insufficient.

## 3. Goals

1. Let typical five-to-eight-call coding tasks finish without context compression when safely possible.
2. When compression is necessary, rewrite the old prefix once, freeze it, and return to append-only growth.
3. Move the compression boundary infrequently and observably.
4. Preserve current function-call protocol validity and enough hot evidence for the next edit or verification decision.
5. Keep exact raw events, tool outputs, changes, and verification records in Session storage even when the model-visible representation is compact.
6. Make cache behavior diagnosable without recording prompt contents or secrets.
7. Apply smaller but not prematurely lossy limits to short-lived implementer agents.
8. Bound worst-case context and cost; this is not a proposal for unlimited replay.

## 4. Non-goals

- Cross-session or cross-conversation memory.
- Provider-managed conversation state as the only source of truth.
- Model-generated summaries that add another required model call.
- Hiding verification failures, uncertain writes, unresolved requirements, or safety decisions.
- Treating a high cache percentage as proof of low total cost or task correctness.
- Changing the current runtime before a separately approved implementation task.

## 5. Proposed context layers

Every model request should be assembled from three explicit layers.

### 5.1 Stable prefix

The stable prefix contains data that should remain byte-for-byte identical throughout a run whenever configuration has not changed:

- system instructions;
- runtime facts;
- stable tool definitions in deterministic order;
- selected skill instructions for the current turn;
- response-language instruction;
- provider cache key.

Dynamic timestamps, counters, absolute temporary paths, run IDs, and current progress must never be inserted before or inside this layer.

### 5.2 Frozen checkpoint

A checkpoint is a deterministic, locally generated summary of completed history through a specific execution boundary. Once created, its serialized content is immutable until a new checkpoint generation is committed.

It contains only durable facts needed for future decisions:

- current goal and acceptance requirements;
- conservative assumptions and architecture decisions;
- relevant and changed paths;
- applied change revision;
- completed tool-operation summaries;
- applied subagent bundle IDs and owned paths;
- current verification records with revision, mode, exit code, and environment-error status;
- unresolved issues and uncertain executions;
- the last authoritative user-visible outcome when continuing the same conversation.

The raw transcript remains in Session storage and is not destructively replaced.

### 5.3 Hot tail

The hot tail remains append-only and protocol-exact. It contains the messages after the checkpoint boundary, including the most recent two or three completed tool batches. It must preserve assistant tool calls and matching tool results as valid pairs.

The hot tail should preferentially retain:

- the latest relevant source excerpts;
- the latest failed edit excerpt and recovery information;
- the latest verification output;
- unapplied subagent bundle summaries;
- the current unresolved error;
- the current user turn.

## 6. High/low watermark algorithm

Compression should use hysteresis instead of continuously rebuilding a derived view near one threshold.

Proposed initial main-agent values for evaluation:

- hard message-context budget: 32,000 estimated tokens;
- high watermark: 90% (28,800 tokens);
- post-checkpoint target: 65–70% (20,800–22,400 tokens);
- hot completed tool batches: 3;
- minimum completed tool batches between checkpoint generations: 6.

Proposed initial implementer values for evaluation:

- hard message-context budget: 12,000 estimated tokens;
- high watermark: 90% (10,800 tokens);
- post-checkpoint target: about 70% (8,400 tokens);
- hot completed tool batches: 2;
- minimum completed tool batches between checkpoint generations: 4.

The values are hypotheses, not final defaults. They require deterministic and real-provider measurements.

### 6.1 Request preparation

1. Build `stable_prefix + frozen_checkpoint + hot_tail` without rewriting any existing component.
2. Include a stable estimate of tool-schema tokens in capacity accounting, even though tool schemas are sent outside the message array.
3. If the request is below the high watermark, send it unchanged.
4. If it reaches the high watermark and enough new completed history exists, construct a candidate next checkpoint.
5. Validate that the candidate preserves tool-call protocol boundaries, current revision evidence, unresolved issues, and the latest hot batches.
6. Commit the checkpoint atomically, assign the next generation number, and freeze its exact serialization.
7. Continue append-only growth from the new checkpoint until the next high-watermark event.
8. If a request approaches the provider/model hard context limit before normal checkpoint eligibility, perform an explicit emergency checkpoint and record the reason. Emergency compaction must be distinguishable in telemetry.

## 7. Deterministic checkpoint format

Checkpoint serialization should use a versioned JSON-compatible structure rendered deterministically with sorted keys and stable list ordering. Example:

```json
{
  "version": 2,
  "generation": 1,
  "through_execution_id": "...",
  "change_revision": 5,
  "goal": "...",
  "requirements": [],
  "decisions": [],
  "relevant_files": [],
  "changed_files": [],
  "applied_subagent_bundles": [],
  "verification": [],
  "unresolved": []
}
```

The checkpoint must not include wall-clock timestamps, random IDs that are not semantically required, raw secrets, full source files, or raw command logs. Lists should be deduplicated and ordered by their first durable occurrence unless a documented field requires another order.

## 8. Tool-result retirement rules

Compression should be based on whether a result has been durably recorded, not only on the tool name.

### 8.1 Read and search results

After the model has consumed a completed read/search batch and the relevant paths/ranges are recorded, retire full old output into a summary containing:

- operation and target;
- covered range or match count;
- observation revision;
- truncation/continuation state;
- success or failure.

Keep the newest source excerpts in the hot tail.

### 8.2 Writes and edits

After an atomic change is recorded by ChangeTracker, retire old full write arguments into:

- path;
- change ID and revision;
- additions/deletions;
- resulting hash;
- active or undone state.

Do not duplicate full file contents in the checkpoint.

### 8.3 Commands and verification

Once a command result has been parsed into a durable execution and verification record, retire old raw output into:

- command identity or safe normalized display;
- purpose and verification mode;
- exit code and expected exit codes;
- passed/conclusive/environment-error flags;
- duration and truncation flags;
- associated paths/domains and change revision.

Keep the most recent failing output in the hot tail until it is resolved or superseded on the current revision.

### 8.4 Subagent results

Before patch application, retain bundle IDs, summaries, paths, and conflict evidence in the hot tail. After an atomic successful application, retire the child transcript and patch detail into:

- agent ID and role;
- owned paths;
- bundle ID;
- applied revision;
- local child verification summary;
- scope/conflict status.

The parent still performs one integrated verification in the real workspace.

## 9. Cache observability

Every model-call record should add content-free diagnostics:

- hash of the stable system/developer prefix;
- hash of deterministic tool definitions;
- hash of the cache key, never the key itself;
- checkpoint generation and checkpoint hash;
- estimated stable-prefix tokens;
- estimated longest common prefix with the previous request;
- index/type of the first changed input item;
- hot-tail tokens;
- compaction reason (`none`, `high_watermark`, `emergency`, `new_turn`);
- provider-reported input, cached, output, and reasoning tokens;
- provider/model identity.

This permits the following diagnosis without storing prompt content:

- local common prefix is small and provider cache is zero: likely local instability;
- local common prefix is large and provider cache is zero: likely provider routing/TTL/eviction or incompatible accounting;
- system/tool hash changes inside one run: deterministic-prefix regression;
- misses coincide with checkpoint-generation changes: expected cold checkpoint boundary;
- repeated misses with an unchanged checkpoint: investigate provider or request conversion.

## 10. Interaction with multi-turn conversation

Mini Coder remains session-scoped only. A new user turn should start from:

- the same stable runtime prefix;
- one compact previous-turn state;
- the latest user-visible assistant outcome when useful;
- a fresh task brief and selected skill for the new request;
- the new user message.

Old raw tool transcripts should not be replayed into the new turn. A new-turn rebuild is an explicit cache boundary and must be recorded as such.

## 11. Interaction with parallel execution

Parallel read tools must append their results in original tool-call order, not completion order, so nondeterministic finish timing cannot perturb the next prompt prefix.

Speculative finish candidates must not mutate the canonical message history. Only an accepted candidate becomes the final result after real verification passes and the change revision remains unchanged.

Subagent cache accounting must be separated by actor (`main`, `scout`, or implementer ID). Aggregate cached tokens alone obscure cold child sessions and cannot diagnose main-agent prefix stability.

## 12. Validation plan

Implementation must not be accepted solely on unit tests or a higher cache percentage.

### 12.1 Deterministic tests

- no compression below the high watermark;
- one checkpoint at the high watermark;
- no checkpoint rewrite on the next several requests;
- a second checkpoint only after the minimum batch interval and next high watermark;
- deterministic checkpoint bytes for identical durable state;
- valid function-call pairs after checkpointing;
- newest failed verification output remains hot;
- superseded old revision evidence is retired correctly;
- parallel tool completion order cannot change serialized history;
- speculative candidates cannot alter checkpoint state;
- emergency checkpoint is explicit and bounded.

### 12.2 Provider probes

1. Run two identical short requests with the same provider, model, cache key, stable prefix, and workspace fixture.
2. Treat the first as cache warm-up and compare the second.
3. Run a bounded multi-tool loop that stays below the high watermark and confirm the local common-prefix estimate grows monotonically.
4. Run a loop that crosses exactly one checkpoint boundary and expect at most one local cache reset attributable to that generation.
5. Repeat with one implementer using its stable role cache key.

### 12.3 Product acceptance

- task correctness and independent verification do not regress;
- no increase in repeated reads or repeated verification;
- no increase in protocol errors;
- fewer checkpoint generations than current compaction events;
- lower uncached input tokens or lower total monetary cost on a warm repeated task;
- no material increase in completion failures caused by missing context.

## 13. Rollout and rollback

The first implementation should remain behind a configuration flag, with the existing ContextManager path available as a rollback. Suggested rollout:

1. add diagnostics only;
2. validate local prefix stability on recorded/deterministic runs;
3. add frozen checkpoints behind a disabled flag;
4. run cold/warm provider probes;
5. run a small fixed task pair;
6. enable for subagents only if child correctness and cost improve;
7. enable for the main agent only after verification-state fixes are complete;
8. keep rollback for at least one release cycle.

## 14. Open questions

- What exact context window and cached-input pricing does each compatible provider expose for each model alias?
- Should stable tool definitions be split by task phase, or would changing the tool set create more cache boundaries than it saves?
- Can accepted verification output be retired immediately, or should the last successful command remain hot until FINISH?
- Should implementers share a role cache key or use per-scope keys to avoid high-traffic prefix collisions?
- Is `previous_response_id` sufficiently compatible across supported providers to complement, but not replace, local replay?
- What high/low watermarks minimize monetary cost rather than raw token count for the selected provider?

Until the provider probes and product-acceptance checks are addressed, the runtime should keep V2 disabled by default and retain the current compression behavior as rollback.
