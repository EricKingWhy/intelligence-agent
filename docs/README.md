# Documentation Map

> 本文件是 `docs/` 的唯一导航入口。它只说明“何时读哪份文档”；规格、流程、进度和决议仍由
> 各自的唯一权威文件定义。

## 当前必读

| 需要回答的问题 | 唯一入口 |
| --- | --- |
| 正式 Engineering Specification 在哪里？ | `goal/Lightweight_Observable_Agent_Harness_Spec/docs/spec/` |
| 当前 Phase 状态和工作焦点是什么？ | `docs/PHASE_STATUS.md` |
| 当前 SDD / review / 集成流程是什么？ | `docs/SDD_WORKFLOW_PROTOCOL.md` |
| Ticket、验证证据、review coverage 和残余是什么？ | `docs/SDD_TICKET_TRACKER.md` |
| 已冻结的机制或设计决议是什么？ | 对应的 `docs/adr/` 文件 |

**路径陷阱**：`docs/spec/` 不是 Engineering Specification。该目录保存流式 UI PRD、
`Observable_Agent_Workspace_SDD/` 和 Web UI 实施资料；其 `README.md` 仅说明此区别。

## 按任务查阅

- Memory V2（`#296`–`#304`）：产品合同读 `docs/PRD_PRODUCTION_LONG_TERM_MEMORY_V2.md`，
  实现票读 `docs/tickets/mem-v2-*.md`，成熟产品调研读
  `docs/research/2026-09-22-production-long-term-memory-systems.md`。
- `docs/adr/`：机制与决议的完整叙述；只读与当前模块相关的 ADR。
- `docs/tickets/`：票面、拆分和历史 ticket 资料；GitHub issue 仍是已批准票面的上游。
- `docs/agents/`：只在对应 Agent 分支触发时读取的 playbook 与仓库约定。
- `docs/phase_status/<年-月>.md`：批次、集成和审查历史明细；先从
  `docs/PHASE_STATUS.md` 的按日索引定位。
- `docs/research/`、`docs/design/`、`docs/troubleshooting/`：对应主题的按需资料。
- `docs/PHASE*_GATE.md`、`docs/CODE_REVIEW_*.md` 等：历史验收证据，不定义当前流程。

## 历史归档

- `docs/archive/integration-prompts/`：原先散落在 `docs/` 顶层的一次性集成提示词。
- `docs/archive/handoffs/`：原先散落在 `docs/` 顶层的交接资料。
- `docs/integration/`：历史集成资料与少量专项材料的混合目录；本阶段未重排。它不是当前流程
  权威，执行其中任何步骤前必须回到 `docs/SDD_WORKFLOW_PROTOCOL.md` 和 `AGENTS.md` 核验。

归档内容保留历史语境，不应被当成当前指令。旧路径不保留 stub；完整旧路径映射见两个归档目录
各自的 `README.md`，仓库外保存的旧 URL 需通过映射或 Git 历史定位。
