# ADR-0057: TUI 终端界面作为流式消费面的记录性承接

- **Status**: Accepted（记录性承接，2026-10-25，#1607；spec 11 是否收录 TUI 为待用户裁决项）
- **Date**: 2026-10-25
- **Scope**: Streaming surfaces 承接记录（非新增能力决策——TUI 已由 #1498/PR #1556 合入 main）
- **Related**: Engineering Spec 11（CLI/SSE/Web UI）；ADR-0016、ADR-0017、ADR-0046；#1498、PR #1556、#1607；GLOSSARY「AgentEvent」「Event Projection」

## Context

`tui/` 子树（61 个 tracked 文件）已随 PR #1556（issue #1498，2026-10-23 merge `fe6c8fb7b`）合入 main，`dev.sh` 提供 `tui` 启动入口。但两个承接缺口随之出现（#1607 整仓 review 的 F5/F6）：

1. 冻结 spec 11 的流式面清单是 CLI / FastAPI SSE / Web Session Inspector，不含 TUI；grep "tui/终端/terminal" 零命中。按 `AGENTS.md` §1.1，实现与冻结规格冲突应报告 Gap，而不是回写规格。
2. TUI 在其他 ADR 中只以**既存客户端**身份被引用（ADR-0016 的 detached-run 常驻附着客户端与 `SeqCursor` seq 语义；ADR-0046 的 client-presence 暂停触发方；ADR-0017 把「步进式 TUI replay」列为 DEFER），从未有一份记录回答「TUI 在本项目架构里是什么」。
3. #1498 的 Tracker 收尾登记（`fc00bac7a`）在 #1504 收尾（`6b0d22a79`）中被删除且月档无承接，进度链断裂（由 #1607 在月档补录修复，与本 ADR 互为指针）。

本 ADR 不新增任何能力、不改运行时行为，只把**已经存在的事实**钉进架构记录，并上报 spec Gap。

## Decisions

### D1. TUI 是第四个流式消费面，消费同一 durable 事件流

TUI（`tui/`，终端交互界面）与 CLI Renderer、SSE Surface、Web Session Inspector 并列，是 `AgentEvent` / durable SessionEvent 的又一个**消费面**（GLOSSARY「AgentEvent：供 CLI / SSE / Web UI / Test / Trace 多面消费」的自然延伸）。它遵守与 Web 相同的投影纪律（GLOSSARY「Event Projection」）：终端侧视图是事件的纯投影，**不持有第二套会话真相**（不变量 #22 同向）；刷新/重连按 durable seq 重建（ADR-0016 的 `after_seq` 语义对 TUI 客户端同样成立）。

### D2. 跨端事件语义单源，TUI 不维护私有词汇

#1498 已把 TUI 侧 19 条手抄事件名接入 `scripts/gen_event_types.py` 生成式单源（两产物各 75 条），并把 `RUN_TERMINAL_TYPES` / `PlanItem` / `planItemsFromData` / `supersedeRanges` / `isShadowed` 五个符号收敛到 `tui/src/shared/event-semantics.ts` 单定义。后续 TUI 侧新增事件语义必须继续走该单源；`web/e2e/fixtures.ts` 的独立终态集合是 #1498 审查裁定的刻意反耦合，不合并。

### D3. TUI 是独立可选面，不是 Core 依赖

`tui/` 是自带 `package-lock.json` 的独立 npm 工程（engines `node >= 22.1`），不进 Python Core 的依赖闭包；`dev.sh tui` 只是本地启动器。TUI 缺席（未安装/未构建）不影响 CLI / Web / 后端任何面——与「Optional subsystem failure 不得拖垮 Core」的不变量精神一致。ADR-0017 对「步进式 TUI replay」的 DEFER 保持原义：当前 TUI 是交互消费面，不是重新执行式回放器。

### D4. spec 11 Gap 上报，冻结规格不擅改

spec 11 处于冻结规格族。是否把 TUI 收入其流式面清单（或以其它章节形式承接）是规格修订决策，**留 spec owner（用户）裁决**；本 ADR 只承担「架构事实有单点记录」的义务。在裁决落地前，TUI 的规格依据以本 ADR 为准。

## Consequences

- TUI 的架构定位、单源纪律、可选性自此有单点记录；后续 TUI 相关票直接引用本 ADR，不必重新论证。
- spec 11 与实现之间存在一个**已上报的、待裁决的 Gap**；用户裁决前不得宣称「spec 11 覆盖 TUI」。
- #1498 的进度事实由月档 `docs/phase_status/2026-10.md` 补录条目承载（#1607 F6）；本 ADR 与该条目互为指针，不重复叙述。
