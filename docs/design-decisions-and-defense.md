# Mini Coder：设计决策与答辩手册

## 1. 一句话定位

Mini Coder 是一个面向本地、中等复杂度开发任务的轻量 Code Agent。它不与 Codex 等通用商业 Agent 比“能力上限”，而是研究：在任务边界较清晰时，能否通过稳定上下文、受控并行和证据门控，减少无效模型调用与多 Agent 开销，同时保留可解释、可复现的执行过程。

“轻量”不是模型更小，也不是永远更便宜；它指 harness 主动限制上下文增长、并发规模、工具范围和收尾条件。是否节省成本必须以同模型、同接口、同任务、顺序运行的 A/B 数据验证。

## 2. Harness 是什么，为什么自己实现

模型只负责提出下一步；harness 负责把它变成可运行的 Agent：收集上下文、暴露工具、执行循环、管理权限与预算、记录状态、处理失败并决定何时允许交付。OpenAI 对 Codex harness 的公开描述同样覆盖上下文、工具、进度、失败、审批与结果呈现，而不只是一个提示词（[Codex as a platform](https://developers.openai.com/blog/codex-as-a-platform)）。

Mini Coder 自己实现 harness 的目的不是重造一个更强的 Codex，而是让以下机制可观察、可替换、可做消融实验：意图识别、Memory V2、子 Agent 调度、并行读工具、修订绑定验证和推测收尾。

## 3. 六阶段是不是固定流水线

不是。Mini Coder 是 **phase-aware ReAct-style loop**：

`Discover → Frame → Locate → Implement → Verify → Finish`

阶段提供状态、预算和 UI 语义；每一轮仍由模型根据工具观察决定下一动作。发现新证据时可以回到定位或实现，验证失败时必须回到修复。因此它不是六次固定模型调用，也不声称严格复现某篇 ReAct 论文的原始实现。

- **Discover**：有界查看工作区与项目约定。
- **Frame**：形成目标、假设、验收项和澄清需求。
- **Locate**：只读取与当前假设相关的文件。
- **Implement**：由主 Agent 修改，或委派独立分支后统一合并。
- **Verify**：执行真实本地检查，并记录对应代码修订。
- **Finish**：检查证据、未解决项与预算后才交付。

## 4. 意图识别为什么放在本地

Build、Fix、Feature、Improve、Explain 由确定性规则先分类，并生成安全假设和验收提示。优点是零额外网络调用、延迟稳定、容易测试，也能让“一句话需求”进入不同执行策略。

意图还影响软收敛目标：Explain 约 3 次、Fix/Feature/Improve 约 6 次、Build 约 8 次模型调用。它们不是强行截断任务的硬上限；当进度存在且接近目标，系统才进入 completion reserve，提醒模型停止扩散探索，把剩余预算留给必要修改、验证和交付。全局步数、时间、模型调用、工具调用和 Token 限额仍是最后保护。

“先展示方案，确认后再做”会被识别为 plan-first 约束；Agent 先返回方案，用户确认后才进入实现。这里应诚实说明：当前主要由运行时提示、会话状态和测试约束保证，不是操作系统级写入隔离。

局限：规则分类可被模糊或混合表达误导。改进方向是保留零调用快速路径，只在低置信度时调用一个轻量分类器。

## 5. Memory V2 有什么不同

普通做法常在每轮重写一份滚动摘要；摘要字节频繁变化会破坏稳定前缀，也可能在多次改写中漂移。Memory V2 采用“代际检查点 + 热尾部”：

1. 系统规则、工具定义和已冻结检查点保持稳定；
2. 最近 3 个工具批次作为热上下文原样保留；
3. 上下文未到 90% 高水位时保持追加，不频繁压缩；
4. 达到阈值且累计足够证据后，将较旧内容确定性投影为结构化检查点，目标回落至约 68%；
5. 检查点一旦生成，在下一次必要换代前字节不变，并记录 generation 与 hash。

检查点保存目标、要求、决定、相关/修改文件、验证证据和未解决项，而不是生成一段自由文本“故事”。这样既降低摘要漂移，也更利于前缀缓存。OpenAI 官方提示缓存文档说明：缓存依赖精确共享前缀，压缩可能降低复用，因此稳定内容应尽量前置且少改（[Prompt caching](https://developers.openai.com/api/docs/guides/prompt-caching)）。官方 Responses API 的 compaction 则可生成不透明的压缩项继续下一轮（[Compaction](https://developers.openai.com/api/docs/guides/compaction)）；Mini Coder 的取舍是本地、结构化和可审计，而不是声称信息保真度一定更高。

高缓存命中率不等于总成本最低。应同时报告未缓存输入、缓存输入、输出/推理 Token、模型调用数、墙钟时间和任务是否通过。

## 6. 子 Agent 为什么是“按需并行”

子 Agent 只适用于边界清晰、可以隔离、能够并行的工作。Anthropic 的官方提示工程指南同样提醒：不要因为能创建子 Agent 就过度使用；应优先委派独立方向，避免重复工作（[Claude prompting best practices](https://platform.claude.com/docs/en/build-with-claude/prompt-engineering/claude-prompting-best-practices)）。

Mini Coder 默认最多并行 2 个子 Agent、最多 2 批；每个分支还有独立步数、时间、调用、Token、文件数和工作区大小预算。Scout 只读，Implementer 在临时隔离副本中写入被分配的文件；子 Agent 不直接污染主工作区，只返回补丁包和证据。主 Agent 检查路径边界、原子应用补丁，并承担最终集成验证。

前后端分离 Build 在用户确认方案后有一条窄而确定的双分支路由，用于稳定演示；其他任务仍由模型根据可分性决定是否委派。简单 Fix 不开子 Agent，因为启动、重复上下文和合并成本通常大于收益。

## 7. 为什么不是所有工具都与模型调用重叠

同一批只读、互不依赖的工具最多并行 2 个。写文件、执行命令、审批和依赖前序结果的读取仍是屏障，因为下一次模型输入必须包含真实工具结果；提前发起模型调用会让它基于过时世界状态推理，并可能产生重复或冲突操作。

因此 Mini Coder 选择“可证明独立时并行”，不追求表面并发率。真正安全的通用工具—模型 overlap 需要可取消推测、状态快照和结果失效机制，复杂度与额外 Token 可能抵消轻量目标。详细取舍见 [工具—模型重叠](architecture/tool-model-overlap.md)。

## 8. 推测收尾是什么，不是什么

它是 **speculative finishing**，不是“跳过验证”，也不是“推测验证结果”。当最后一项标准验证超过 800 ms、已有有效修改、没有阻塞项且预算允许时：

- 一条线程继续执行真实本地验证；
- 另一条线程用低推理强度、无工具的模型调用，提前整理“做了什么、改了哪里、有哪些已知限制”；
- 草稿被禁止宣称测试通过；
- 只有验证成功、代码 revision 未变化、无未解决项、草稿没有工具请求或越权结论时，Finish Gate 才接纳草稿并附加真实命令证据；否则丢弃，回到正常循环。

验证在 800 ms 内结束时不启动额外模型调用，避免为了省很少时间反而增加成本。同一代码 revision 最多推测一次。该能力默认关闭，适合验证稳定且延迟明显的场景；演示或实验中应明确是否开启。

## 9. 为什么验证要绑定代码修订

“之前测试通过”不能证明“修改后仍然通过”。每次写入或应用子 Agent 补丁都会推进 change revision；验证记录保存执行命令、退出码、时间和对应 revision。Finish Gate 只接受当前 revision 的有效证据。任何后续写入都会使旧证据过期，避免模型误用历史成功结果。

## 10. 与现有 harness 的可辩护对比

| 系统 | 主要定位 | Mini Coder 借鉴/差异 |
|---|---|---|
| Codex | 通用、高能力的软件工程 Agent 与平台化 harness | 同样重视上下文、工具、权限、失败和结果；Mini Coder 范围更窄、实现更小，重点展示本地可观察的缓存、预算与 Finish Gate，不主张能力更强。 |
| Claude Code | 通用终端编码 Agent，支持模型自行组织子 Agent | 都允许委派；Mini Coder 额外限制并发/批次/文件边界，并让补丁回到主 Agent 集成，牺牲自由度换取可控性。 |
| OpenHands | 可扩展 SDK/平台，强调组件化与运行时隔离 | OpenHands 的 Docker runtime 提供安全、资源控制与可复现性（[Runtime](https://docs.openhands.dev/openhands/usage/architecture/runtime)）；Mini Coder 默认直接在受信任工作区运行，更轻但安全边界更弱。OpenHands V1 的设计也强调可组合层次与可选隔离（[SDK design](https://docs.openhands.dev/sdk/arch/design)）。 |
| SWE-agent / mini-SWE-agent | 面向软件工程任务和轨迹研究的精简 Agent | 都强调轨迹可检查；SWE-agent inspector 将 trajectory 视为核心输出（[Inspector](https://swe-agent.com/latest/usage/inspector/)）。Mini Coder 额外面向多轮本地 GUI、会话管理与修改撤销。 |

这张表比较的是设计取舍，不是排行榜。除非有预注册、同条件、重复运行的公开评测，否则不能宣称“比 Codex 更便宜”或“成功率更高”。

## 11. 常见追问与回答

### 创新是不是只是把已有概念拼起来？

ReAct、缓存、子 Agent 和并发都不是新概念。项目价值在于对轻量目标做了互相约束的系统组合：意图给出收敛目标；代际记忆稳定前缀；子 Agent 只在收益可能覆盖开销时启动；推测收尾只重叠不依赖验证结论的工作；revision-aware Finish Gate 阻止草稿越权。应把它表述为 harness engineering contribution，而不是算法原创。

### 为什么不直接使用 Codex？

实际开发当然可以使用 Codex。本项目是可读、可改、可测的研究原型，用于验证窄场景中的上下文和调度策略；它不需要复制商业 Agent 的全部能力，也不需要假装替代品。

### 既然叫轻量，为什么总 Token 上限很高？

上限是灾难保护，不是目标用量。真正的轻量来自软收敛、上下文选择和按需委派。演示前将硬上限调高是为了避免复杂 Build 因供应商输出差异意外中止，但实验报告仍应记录实际用量。

### 为什么只允许两个子 Agent？

典型前后端拆分恰好有两个独立边界；更多分支会复制上下文、增加冲突和合并验证成本。两个是保守默认值，不是理论最优，可在更大仓库中通过消融实验调整。

### 为什么不用模型生成 Memory 摘要？

模型摘要表达能力更强，但额外花费一次调用、输出不稳定且可能遗漏证据。确定性结构化投影更适合缓存和审计；未来可在超长语义内容上增加可选模型摘要层，但不能替代事实账本。

### 为什么推测收尾默认关闭？

它只在“验证时间足以覆盖额外模型调用”时有收益，并增加供应商费用。默认关闭更符合轻量和可预测原则；需要以具体项目的验证耗时与模型价格决定是否开启。

### GUI 显示完成是否等于测试通过？

不等于。状态区区分“完成”“完成（未验证）”“验证失败”等结果。只有当前 revision 的真实检查通过，才能显示已验证证据。

### 安全边界在哪里？

工具做工作区路径限制、危险命令确认、输出截断与敏感信息脱敏，但这不是恶意代码沙箱。只应在受信任仓库使用；处理不可信代码时应放入 VM/容器。OpenHands 的 Docker runtime 是更强隔离方向的参考。

### 最大弱点是什么？

意图分类仍以规则为主；本地命令不是 OS 沙箱；确定性前后端路由覆盖面窄；供应商缓存语义不可控；推测收尾可能增加 Token；尚无足够公开 benchmark 证据证明总体成本优势。主动承认这些边界，比用单个演示任务外推更可信。

## 12. 如何做可信实验

1. 固定模型、推理强度、API 供应商、任务版本与验收脚本；Mini Coder 与对照组顺序运行，避免账单混淆。
2. 同一任务至少重复多次，报告成功率分布而非最佳一次。
3. 同时报 input、cached input、output、reasoning、调用次数、工具次数、墙钟时间和费用。
4. 分别消融 Memory V2、子 Agent、并行读工具和推测收尾，验证每个机制的边际收益。
5. 公开失败与 infrastructure error；不能把接口故障算成 Agent 失败，也不能把自设计任务当作通用能力证明。

当前可公开陈述的工程证据：本版本 231 项单元测试通过，敏感信息扫描通过；最终演示视频约 119.96 秒。它们证明发布构建与演示流程可用，不证明对其他 Agent 的统计优势。

## 13. 不超过一分钟的英文介绍

> Mini Coder is a lightweight Code Agent for local, moderately complex development tasks. Instead of competing with general-purpose commercial agents on maximum capability, it focuses on bounded work with less unnecessary context, model usage, and multi-agent overhead. Its phase-aware ReAct loop moves through Discover, Frame, Locate, Implement, Verify, and Finish, while tool feedback can send the agent back whenever new evidence appears. Four mechanisms shape the runtime: local intent recognition, cache-stable generational memory, bounded parallel subagents, and speculative finishing. Subagents are used only for independent work, and speculative finishing overlaps slow verification with a tool-free delivery draft. Code changes never become “verified” merely because the model says so: the final gate requires real local checks tied to the current code revision. Mini Coder therefore aims to make everyday coding assistance lightweight, inspectable, and evidence-driven.

