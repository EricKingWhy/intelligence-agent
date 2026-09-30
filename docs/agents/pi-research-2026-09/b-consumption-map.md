# Pi 精读批次 B —— 消费面普查（已有实现 vs 待深读问题）

- **调查范围**：仅本仓库（D:\intelligence-agent）。上游浅克隆 `D:\reference\pi`（HEAD `1b34779`）与笔记均未读取；行号全部指向本仓库文件。
- **决策权威**：`goal/Lightweight_Observable_Agent_Harness_Spec/docs/spec/13_OPEN_SOURCE_REUSE_MATRIX.md` §2（下称 13§2）。
- **判定口径**：已实现 = 核心路径有代码 + 有测试；部分实现 = 有代码但边界/V1 裁剪明确；未开工 = 无实现痕迹。

---

## a) 对照表：Pi 能力 × 13 矩阵决策 × 我们现状 × 深读提问

### 1. Minimal Agent philosophy —— PORT DESIGN（13§2:30）｜**部分实现**

Core 为 Python 单包（`src/agent_harness`，213 个 .py）；装配收敛为单一深 factory。

- 证据：`src/agent_harness/assembly.py:185`（`build_runtime`，全文件 536 行，web 与 CLI 两个 adapter 共享）；`src/agent_harness/agent/runtime.py:862`（`AgentRuntime`）。
- 边界现状：无条件注册的本地工具 9+1 个（`assembly.py:85` `BUILTIN_LOCAL_TOOLS` + `assembly.py:303` `UpdatePlanTool`）；optional capability 7 个（`capability/wiring.py:590-596` `_WIRERS`：memory/skills/ticker/mcp/knowledge/websearch/multiagent）。`AgentRuntime` 构造参数约 30 个，装配面明显重于 Pi 的 minimal agent。
- **深读提问**：笔记/上游第 2 章——Pi 的 agent loop 核心到底多大（单文件多少行、依赖几个类型）？它与「extension 全部外置」的分界线画在哪里？对照我们：AgentRuntime 30 个构造参数里，哪些在 Pi 是 extension 注入而非 core 参数？

### 2. Session Tree / Fork —— PORT DESIGN（13§2:31）｜**已实现**

file-per-lineage 方案（ADR-0017 决策 1 **明确否决** pi 的 tree-in-file：单文件 id/parentId + active leaf 会破坏 Session 线性 append-only 不变量）。

- 证据：`src/agent_harness/session/fork.py:162`（`fork_session`：锚点=第 N 条用户消息、seed=事件前缀逐字复制重编 seq、tail 摘要、cwd/permission 显式继承）；`src/agent_harness/session/lineage.py:51,109`（lineage 索引建树）。
- 消费面：`src/agent_harness/web/lineage.py:57`（GET lineage 只读树）、`:113`（POST forks 创建）、`src/agent_harness/cli.py:860`（`fork` 命令）。测试 `tests/session/test_fork.py`（632 行）、`tests/session/test_lineage.py`（157 行）。
- **深读提问**：笔记/上游第 10 章——pi tree-in-file 里「active leaf 切换」如何做崩溃恢复与并发写保护？我们在 UI 上用只读 lineage 视图近似「原地探索」，Pi 的交互层（切 leaf 即切上下文）有没有**不依赖其存储方案**、可单独 PORT 的 UX 设计？

### 3. Resume / JSONL —— PORT DESIGN（13§2:32）｜**已实现**

事件流 = append-only JSONL（每会话一文件）；恢复 = 从事件重建 + Operation Ledger reconcile。

- 证据：`src/agent_harness/session/store.py:94`（`JsonlSessionStore`，布局 `<root>/<session_id>/events.jsonl`，:166 `append_event` 带锁追加）；`src/agent_harness/recovery/coordinator.py:244`（`RecoveryCoordinator.recover`：事件重建 Session + Ledger 对账）。
- 消费面：`src/agent_harness/web/app.py:1507`（POST `/api/sessions/{id}/resume`）；`src/agent_harness/cli.py:866`（`replay` 命令，ADR-0017 决策 4 逻辑回放）。测试 `tests/agent/test_resume_session.py`（150 行）、`tests/recovery/test_recovery_coordinator.py`（693 行）。
- **深读提问**：笔记/上游第 10 章——pi resume 时对「未收口的 tool call / 半截 run」怎么处理？我们走 Ledger reconcile + PendingPolicy（skip/synthesize），Pi 的方案（若有）与我们的 reconcile 语义差异在哪？另：pi 的 JSONL 是否也存完整 tool result 大负载，还是外置 ref？

### 4. Compaction —— PORT DESIGN（13§2:33）｜**已实现**

历史不删除（JSONL append-only）+ runtime context 层做 summary；三档 fallback（ADR-0007）。

- 证据：`src/agent_harness/context/compactor.py:149`（`ContextCompactor`：auto 0.70 / hard guard，摘要拒收重试）；`src/agent_harness/context/builder.py:297`（`ContextBuilder.build` 自动触发 compaction，:443 拒绝落未校验摘要）。
- 事件契约：`src/agent_harness/session/event.py:64`（`context/compacted`）、`:124-125`（`compaction/start`、`compaction/end` bracket——注：bracket 形制来自 DSH 4-event 方案，非 Pi）。历史不删除：`src/agent_harness/context/pruner.py:7`（「SessionEvent 与 Artifact 一字不动」）。
- 测试 `tests/context/test_compactor.py`（518 行）、`test_compaction_bracket.py`。
- **深读提问**：笔记/上游第 9 章——pi compaction 的触发时机（阈值? 手动?）、摘要 prompt 让模型保留什么（决策? 文件路径? 工具结果?）、以及 compaction 边界之后 tool call/result 配对如何不断裂？与我们「完整早期 turn → summary + recent 保留」的语义逐条对账；pi 有没有硬闸兜底（超过 hard guard 时怎么办）。

### 5. Skills / SKILL.md —— PORT DESIGN（13§2:34）｜**已实现（V1 边界）**

渐进披露：目录 → name+description → 按需 load 全文；SKILL.md 格式直接复用（ADR-0011，依据明写 13§2）。

- 证据：`src/agent_harness/skills/discovery.py:78`（`parse_skill_markdown`：frontmatter/body 切分、路径边界 `resolve_within`:115）；`src/agent_harness/skills/tool.py:36`（`LoadSkillTool` 按需读取，body 有上限截断）。
- 目录披露走 context provider：`src/agent_harness/skills/context_provider.py:19`（`SkillCatalogContextProvider`）；接线 `src/agent_harness/capability/wiring.py:263`（`_wire_skills`）。测试 `tests/capability/test_skills_discovery.py`（241 行）、`test_skills_capability.py`（321 行）。
- V1 裁剪（ADR-0011）：不做 Marketplace、不做自动推荐。
- **深读提问**：笔记/上游扩展/skills 部分——pi 的 skill 除了「目录 + 按需 load」还有没有更多披露层级（如 description 触发条件、skill 间引用、skill 携带脚本/资源文件）？我们的 V1 边界之外，哪些披露机制值得补？

### 6. Extensions —— PORT DESIGN（13§2:35）｜**已实现（Capability/Plugin 缝）**

Interface/Provider/Consumer seam + 显式装配 + 三档降级（08 spec → `capability/base.py`）。

- 证据：`src/agent_harness/capability/base.py:45`（`CapabilityDescriptor` + `Degradation` 三档：:15）；`src/agent_harness/capability/wiring.py:632`（`wire_capabilities`：OPTIONAL factory 失败降级跳过并记 warning，:644）。
- 能力清单 7 项见 `wiring.py:590-596`；工具贡献接口 `ContributesTools`（`wiring.py:36`）。测试 `tests/capability/test_capability_seam.py`（105 行）+ 各能力 wiring 测试。
- **深读提问**：笔记/上游第 2 章 + extensions 文档——pi extension 能钩住哪些生命周期点（loop 迭代前后、tool 执行前后、事件流订阅、UI 渲染）？对比我们 `CapabilityWiring` 只在**装配期**注入工具/provider，pi 的**运行期**钩子（尤其 tool 执行拦截、prompt 组装干预）是否值得作为 PORT DESIGN 补进 08 spec 的下一版？

### 7. Pi TypeScript runtime —— DEFER（13§2:36）｜**守住（已实现）**

无任何 TS 进入 Python Core。

- 证据：`src/` 下 .ts/.tsx 计数 = **0**（实测 find）；TS/TSX 221 个全部在 `web/`（React 前端消费面，spec 11 允许，经 SSE/API 消费 session/event，不维护第二套会话真相）。
- **深读提问**：不需要（DEFER 项无需细读）；仅在 A1 批次第 1/2 章核对「Pi 四包分层」时顺带确认无跨语言耦合假设即可。

### 附：Pi 直接 mention 的 ADR（决策面补充证据）

- `docs/adr/0011-skills-progressive-disclosure.md:6,18`（13§2 PORT DESIGN 依据、SKILL.md 格式复用理由）。
- `docs/adr/0017-phase14-resume-replay-fork.md:17-19,21,26-27`（pi tree-in-file 被否、fork boundary 与 pi `/fork` 对齐、seed 逐字复制、lineage 双层建模）。

---

## 契约落码对照（SPEC_ROOT 02/03/04/06/07/11 × 代码）

| 规格 | 相关契约 | 状态 | 证据 file:line |
|---|---|---|---|
| 02_AGENT_RUNTIME | §5.1 local fuse（max_agent_turns 保险丝） | 已落代码 | `src/agent_harness/agent/runtime.py:869`（注释直引「02 §5.1」） |
| 03_SESSION_EVENT_MODEL | §2 typed SessionEvent（seq/type） | 已落代码 | `src/agent_harness/session/event.py:246`（`class SessionEvent`，:253 `seq`、:255 `type`） |
| 03_SESSION_EVENT_MODEL | §5 Resume / §6 Replay / §7 Fork / §8 Compaction | 已落代码 | `web/app.py:1507` / `cli.py:866` / `session/fork.py:162` / `context/builder.py:297`（03:7 明写「Session Tree / Fork / Compaction 思路参考 Pi」） |
| 04_TOOL_RUNTIME | 单一执行路径 + 唯一 Retry 层 | 已落代码 | `src/agent_harness/tooling/executor.py:239`（`class ToolExecutor`）、`:12`（「唯一 Retry Layer」头注释） |
| 06_CONTEXT_ARTIFACT_MEMORY | auto_compact_threshold=0.70、summary+artifact_ref 溢出 | 已落代码 | `src/agent_harness/context/compactor.py:151`（默认 0.70）；`src/agent_harness/assembly.py:310-329`（溢出 handler 与读回工具成对接线） |
| 07_STORAGE_PERSISTENCE_RECOVERY | 恢复 MUST 查 Operation Ledger | 已落代码 | `src/agent_harness/recovery/coordinator.py:244`（recover → Ledger reconcile） |
| 11_STREAMING_API_WEB_UI | SSE 消费（UI 不持第二真相） | 已落代码 | `src/agent_harness/web/app.py:23,953`（`EventSourceResponse`） |

（本仓库该 7 项契约未见「只有 spec」的空白项；未实现的设计草案在 03 §3.3 已被规格自身标注为「勿按此实现」。）

---

## b) 给 A1/A2 批次的重点关注清单

依据 = 我们已实现程度：**已实现且需对账差异的章节逐字读；我们另起炉灶（BUILD）或 DEFER 的章节略读。**

### 逐字细读（已实现，需要逐条语义对账）

| 章节 | 理由 |
|---|---|
| **第 10 章 会话管理（存储/恢复/分叉）** | 我们 fork/lineage/resume/JSONL 全部已实现且 ADR-0017 明确否决了 pi tree-in-file——必须逐字核对 pi 的 tree/leaf/resume 细节，确认被否的理由仍然成立、且 UX 层没有可单独 PORT 的部分。深读提问见上表 2/3。 |
| **第 9 章 上下文压缩** | 我们 compactor 已实现（三档 fallback + bracket 事件），但 pi 的触发时机/摘要内容策略/配对保持细节未知——逐字读以补齐对照表第 4 条的提问。 |
| **第 2 章 三层架构** | Minimal philosophy + extension 分层是我们唯一「部分实现」项（装配面偏重）——逐字读 pi 的 core/extension 分界线，产出「AgentRuntime 哪些参数应下沉为 capability」的对照清单。 |

### 中度阅读（已实现、取差异点即可）

| 章节 | 理由 |
|---|---|
| 第 8 章 上下文工程 | ContextBuilder/provider/pruner 已有；重点看 pi 的 system prompt 分区、prompt caching、消息注入顺序与我们 registry-order 方案的差异。 |
| 第 6 章 消息系统 | `session/derive.py` 已实现 event→message 投影；看 pi 的消息形状（tool call 配对、图片/附件）是否有我们没有的边界情况。 |
| 第 5 章 工具系统 | ToolExecutor/permission/retry 全链路已实现且比 pi 更重（Ledger/审批/溢出）；只对照 pi 的工具错误分类与 retryable 位语义，其余略。 |

### 略读

| 章节 | 理由 |
|---|---|
| 第 1 章 总览 | A1 已核实（29 吻合/15 偏差）；无新增消费点。 |
| 第 3 章 Agent Loop | 我们按 13§5 BUILD 自有 loop（`agent/runtime.py`），不 Port pi 循环；对照确认无遗漏语义即可。 |
| 第 4 章 模型调用 | 我们 model provider/fallback 独立成域（ADR-0014/0032），pi registry 思路仅参考；api 形状差异无移植价值。 |
| 第 7 章 事件驱动 | 我们事件模型来自 spec 03（typed SessionEvent），非 pi 事件总线；只看 pi 面向 UI 的事件流切片方式与 11 spec 的 SSE 消费是否互相印证。 |

### A2 批次（第 6–10 章）优先级排序

1. **第 10 章**（对照表 2/3 的全部提问）→ 2. **第 9 章**（对照表 4）→ 3. 第 8 章 → 4. 第 6 章 → 5. 第 7 章（略）。
A1 批次（第 1–5 章）补读点：仅第 2 章的 extension 生命周期钩子部分（对照表 6 提问），其余维持 A1 已核结论。
