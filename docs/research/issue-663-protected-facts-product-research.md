# #663 受保护事实：成熟产品机制调研

日期：2026-10-05。范围：约束保留、来源、可编辑性、作用域、预算、压缩与恢复。ZCode 指 Z.ai 的 ZCode，不是 Tencent CodeBuddy。以下依据一手文档和固定 commit 的上游源码。

## 结论

在核对的 Claude Code、Codex、DeepSeek Harness、Pi、oh-my-pi、Z.ai ZCode 机制中，未发现同时满足“当次调用、明确用户原文来源、受保护事实登记”的现成 API。结论仅限下列机制和源码范围，不证明产品所有入口都不存在类似功能。

最接近的是 oh-my-pi 的 context_notes：显式工具、当前会话分支、写入审批类别、预先限额、追加日志、压缩后继续注入。但它写的是模型维护的整本可替换笔记，不是逐条受来源验证的 protected fact。Pi 的约束摘要、Codex Goals、静态指令文件和自动记忆都是相邻设计，不应等同于事实注册。

## 产品证据

| 产品 | 实际机制 | 限制与对 #663 的适配 |
|---|---|---|
| Claude Code | CLAUDE.md 是人工维护的指令；auto memory 是 Claude 按项目保存、可通过 /memory 编辑的 Markdown 记忆，可明确要求“记住”。[官方文档](https://code.claude.com/docs/en/memory) | 每轮自动加载 MEMORY.md 前 200 行或 25 KB，其余按需读。自动提炼和项目级生命周期不保证逐字保留当轮用户原文，也不是受来源约束的注册工具。 |
| OpenAI Codex | AGENTS.md 是分层指令文件；Goals 是用户维护的线程目标，含 outcome/verification/constraints，可暂停、恢复、清除；Memories 是后台异步的 rollout 提取与整合。[AGENTS](https://developers.openai.com/codex/guides/agents-md/)；[Goals](https://developers.openai.com/cookbook/examples/codex/using_goals_in_codex)；[Memories 源码说明](https://github.com/openai/codex/blob/7f892275e31002f0422477c6219189284560e689/codex-rs/memories/README.md#L29-L120) | AGENTS 有默认 32 KiB 上限；Memories 的提示要求区分用户与助手、保留来源/范围/授权边界，但不保证逐字原文，且为跨会话异步记忆。Goals 是线程目标，不是 protected-fact API。 |
| Z.ai ZCode | 每个成功轮次后，后台决定是否保存偏好、纠正、目标、约束或引用；可要求 remember/forget。memory 与 AGENTS 指令分开，是项目本地 Markdown，可编辑，默认关闭，子代理不读写。[Memory](https://zcode.z.ai/en/docs/memory)；[Agents](https://zcode.z.ai/en/docs/agents) | 后台写入供后续项目会话使用，索引有容量约束并产生额外调用/token。灵活但依赖模型抽取，写入不在当前调用同步受控。 |
| Pi | 压缩提示生成含 Goal、Constraints & Preferences 的模型摘要，/compact 可指定保留主题；原始 session entries 保留，摘要另存。[压缩提示](https://github.com/earendil-works/pi/blob/1b347794e2a630e4359f2584f4eea388145d0ddf/packages/coding-agent/src/core/compaction/compaction.ts#L507-L575)；[Sessions](https://github.com/earendil-works/pi/blob/1b347794e2a630e4359f2584f4eea388145d0ddf/packages/coding-agent/docs/sessions.md#L36-L46) | 约束仍是模型摘要，不是独立事实记录；压缩失败可重试，原始记录不删。适合作为“历史保留、上下文可压缩”的类比。 |
| DeepSeek Harness | Session 是 append-only typed SessionEvent 日志，模型历史从日志派生；表面事件可记录来源事件序号。压缩会记录摘要、被遮蔽范围与模型调用。[Session](https://github.com/deepseek-ai/deepseek-harness/blob/5badb15009ae1756c3afe0ae0cef1faafc290ccc/docs/subsystems/session.md#L5-L11)；[来源与压缩](https://github.com/deepseek-ai/deepseek-harness/blob/5badb15009ae1756c3afe0ae0cef1faafc290ccc/docs/subsystems/session.md#L294-L300)；[Compaction](https://github.com/deepseek-ai/deepseek-harness/blob/5badb15009ae1756c3afe0ae0cef1faafc290ccc/docs/subsystems/compaction.md#L9-L21) | 未闭合的恢复状态可识别并补合成结束事件；副作用结果未知时不盲目重试。[repair.ts](https://github.com/deepseek-ai/deepseek-harness/blob/5badb15009ae1756c3afe0ae0cef1faafc290ccc/packages/core/session/src/repair.ts#L21-L40)；[恢复入口](https://github.com/deepseek-ai/deepseek-harness/blob/5badb15009ae1756c3afe0ae0cef1faafc290ccc/packages/core/session/src/repair.ts#L201-L210) | 强项是来源追溯、压缩范围和恢复；不是模型可调用的 constraint 注册工具，也不决定用户约束优先级。 |
| oh-my-pi | 可选 context_notes 工具对当前会话分支读/替换/清空整本 notebook；每次替换以 custom entry 追加到日志，活动分支的最新版本注入；tool writes 使用 write 审批类别。[Tool 文档](https://github.com/can1357/oh-my-pi/blob/1c0993c3d12e70042169a951663bb2702e2c0a9e/docs/tools/context-notes.md#L14-L45)；[owner/审批源码](https://github.com/can1357/oh-my-pi/blob/1c0993c3d12e70042169a951663bb2702e2c0a9e/packages/coding-agent/src/tools/context-notes.ts#L44-L88) | 上限 16,384 UTF-8 bytes，超限在追加前拒绝；分支变化时拒绝写入。压缩 rollover 保留笔记并能按稳定 entry ID 查询本分支原始历史；笔记质量和及时性由模型负责。[写入源码](https://github.com/can1357/oh-my-pi/blob/1c0993c3d12e70042169a951663bb2702e2c0a9e/packages/coding-agent/src/tools/context-notes.ts#L103-L145)；[rollover/恢复](https://github.com/can1357/oh-my-pi/blob/1c0993c3d12e70042169a951663bb2702e2c0a9e/docs/compaction.md#L165-L198) | 最接近当前任务状态写入，但整本替换和清空与 ADD-only 不同，也没有每条内容必须源自用户原文的校验。 |

## 对 #663 的启发

**产品事实：** DeepSeek Harness 的事件序号、oh-my-pi 的稳定历史 ID 和 Pi 的原始 session 保留，展示了压缩后仍可引用原始来源的办法；Codex memory 提示强调来源/范围，但没有保证逐字引用。oh-my-pi 在超限时预检并拒绝追加。

**对本仓的推论：** 仅允许最新直接投递的 USER_MESSAGE，来源验证较窄；允许当前 input 中任意直接用户原文，则成熟产品类比支持通过事件 ID/序号保留出处，但不能证明本仓压缩后的 active input 仍能定位原始 USER_MESSAGE。若选择较宽范围，必须核实本仓的 source range、active input、USER_MESSAGE 与 compaction 映射；不能以模型摘要充当原始来源。主开发已把“最新直接输入”与“当前 input 中任意直接用户原文”交给用户选择。

人工可编辑 Markdown 或整本替换更灵活，但允许一次编辑影响多项内容；逐条追加和精确去重更窄、更好审计。对不可改写的用户原文事实，超预算前置拒绝与 oh-my-pi 的超限策略相符；自动摘要/截断其他产品的记忆，不构成压缩受保护原文的依据。写入权限与会话范围应由 runtime 检查，不能只依赖 prompt。

## 本仓现状（只读核对）

核对工作树：D:/intelligence-agent-backend-wt-context-663，HEAD d64cda89fc998ae2126f0838cd4146107b2e5c9f，分支 codex/context-663-register-constraint，读取前 clean。

- 已有低层 Session.register_protected_fact(...)：校验来源事件后追加 task/protected_fact；fact 有 source_event_id/source_seq/status，投影只注入 active 项。见 src/agent_harness/session/session.py:363-395、src/agent_harness/session/derive.py:114-147,662-703。
- 现有测试锁住幂等注册和超预算拒绝、不截断：tests/session/test_protected_facts.py:644-661；tests/context/test_protected_facts.py:161-173；同文件 176 起覆盖 build/compaction/restart/fork。
- 对 src 和 tests 检索 register_constraint 后，当前可见的是既有 Session 注册 API 与 protected-fact 投影；没有找到模型可调用的 register_constraint 工具。这是限定搜索结果，不是对潜在接线入口的证明。#663 的调研重点是复用事实链并确保输入来源，不是另造长期 memory。

工程规格背景：model-visible input 可追溯、Persistent History 与 Runtime Context 分离；Memory 是可替换 Capability/Context Provider。见 SPEC_ROOT/00_PROJECT_VISION.md §3、SPEC_ROOT/01_SYSTEM_ARCHITECTURE.md §5/§8、SPEC_ROOT/06_CONTEXT_ARTIFACT_MEMORY.md §1/§6–§8、SPEC_ROOT/13_OPEN_SOURCE_REUSE_MATRIX.md §2–§4、SPEC_ROOT/14_IMPLEMENTATION_ROADMAP.md Phase 5–6。

## 固定版本与本地引用

上游均浅克隆在项目外；下列 permalink 固定 commit，行号由对应本地 checkout 核对。

| 仓库 | Commit（日期） | 本地路径 |
|---|---|---|
| Pi（badlogic/pi-mono 更名为 earendil-works/pi） | 1b347794e2a630e4359f2584f4eea388145d0ddf（2026-09-29） | D:/reference/pi |
| DeepSeek Harness | 5badb15009ae1756c3afe0ae0cef1faafc290ccc（2026-10-03） | D:/reference/deepseek-harness |
| oh-my-pi | 1c0993c3d12e70042169a951663bb2702e2c0a9e（2026-10-04） | D:/reference/oh-my-pi |
| OpenAI Codex | 7f892275e31002f0422477c6219189284560e689（2026-10-04） | D:/reference/codex |

代码来源的本地 file:line：Pi 的 packages/coding-agent/src/core/compaction/compaction.ts:507-575、packages/coding-agent/src/core/session-manager.ts:118-153；DeepSeek Harness 的 docs/subsystems/session.md:5-11,294-300、docs/subsystems/compaction.md:9-21、packages/core/session/src/repair.ts:21-40,201-210；oh-my-pi 的 docs/tools/context-notes.md:14-45、packages/coding-agent/src/tools/context-notes.ts:44-88,103-145、docs/compaction.md:161-198；Codex 的 codex-rs/memories/README.md:29-120、codex-rs/memories/write/templates/memories/stage_one_system_v2.md:9-49、stage_one_input_v2.md:9-21、codex-rs/config/src/types.rs:356-390。

## 启动检查表

| 前置项 | 实际读取依据 | 状态 |
|---|---|---|
| Vision 相关原则 | SPEC_ROOT/00_PROJECT_VISION.md（完整读取；重点 §2.2、§3、§6） | READY |
| 当前任务规格 | SPEC_ROOT/06_CONTEXT_ARTIFACT_MEMORY.md §1–§8；SPEC_ROOT/04_TOOL_RUNTIME.md §2、§5、§8–§9 | READY |
| Reuse 相关判定 | SPEC_ROOT/13_OPEN_SOURCE_REUSE_MATRIX.md（完整读取；重点 §1–§4） | READY |
| Phase 依据 | SPEC_ROOT/14_IMPLEMENTATION_ROADMAP.md（完整读取；重点 Phase 5–6） | READY |
| 本任务触发细则 | docs/agents/reference-sources.md；docs/SDD_WORKFLOW_PROTOCOL.md §1.3、§8.9；research Skill；不操作 GitHub Issue，不改代码/规格 | READY |

## #663 用户裁决与实现阶段记录（2026-10-05）

用户明确选择“暂停并续跑当前 run”：澄清回答必须以原 `run_id` 恢复，保留现有预算账，不创建新任务 run。用户明确更正 active protected constraint 时，由主模型发起一次交互，让用户选择永久或当前任务范围；对没有明确更正、但可能冲突的新要求，仅在主模型无法可靠判断持久范围时询问。普通消息仍由模型自主筛选，不要求用户逐条确认。具体机制与成熟产品依据见 [ADR-0051](../adr/0051-protected-fact-conflict-user-input-resume.md)。

GitHub #663 正文已按该决定更新，且经完整正文精确回读确认：新增专用 `request_constraint_resolution`、`user/input-requested`、`reason=user_input` 同 run 恢复和 modal AC18–AC21。公开 issue 当前无评论、无关联 PR；施工分支为 `codex/context-663-register-constraint`，基线 `e8e36f60106f3aefb540210693ee34b5c6fb3ed0`。根 checkout 的 `docs/SDD_TICKET_TRACKER.md` 有另一条工作线的未提交追加，本轮未改该文件；本记录用于保存 #663 当前决策与施工状态。

### 本轮启动检查

| 前置项 | 实际读取依据 | 状态 |
| --- | --- | --- |
| Vision 相关原则 | `goal/Lightweight_Observable_Agent_Harness_Spec/docs/spec/00_PROJECT_VISION.md` §3 | READY |
| 当前任务规格 | `03_SESSION_EVENT_MODEL.md`、`04_TOOL_RUNTIME.md`、`06_CONTEXT_ARTIFACT_MEMORY.md`、`07_STORAGE_PERSISTENCE_RECOVERY.md`、`11_STREAMING_API_WEB_UI.md` 的相关章节 | READY |
| Reuse 相关判定 | `goal/Lightweight_Observable_Agent_Harness_Spec/docs/spec/13_OPEN_SOURCE_REUSE_MATRIX.md` §§1–3；`docs/agents/reference-sources.md` 对应 Agent Harness 条目 | READY |
| Phase 依据 | `goal/Lightweight_Observable_Agent_Harness_Spec/docs/spec/14_IMPLEMENTATION_ROADMAP.md` Phase 5–6 | READY |
| 本任务触发细则 | `docs/SDD_WORKFLOW_PROTOCOL.md` 完整正文及 §§1.3、5、7、8.5、8.9；`docs/agents/issue-tracker.md`；`docs/agents/implementation-discipline.md` §§9.5–9.6；TDD 与架构相关指引 | READY |

### 执行状态

- 已完成：确认 #663 原先无实现 PR/评论；核实成熟产品交互机制；用户选择 durable 同 run 暂停与续跑；更新 issue AC 并精确回读。
- 进行中：按 #663 与 ADR-0051 实现 protected constraint 登记、冲突澄清工具、pause/resume API 与 Web modal。
- 未完成：测试、恢复/重启验证、风险审查、完整门禁、Git 提交及后续集成。未验证前不得将票标记完成。
