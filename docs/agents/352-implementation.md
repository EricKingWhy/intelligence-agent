# #352 [W-08] 验收项与真实证据的服务端投影 —— 实现文档

> 工作区 `~/workspace/intelligence-agent-wt-352`，分支 `codebuddy/352-w08-evidence-projection`。
> 研究结论：票面成立，按票面施工（`docs/agents/352-research.md`）。
> 本文件记录实现阶段的启动检查表、设计决策与逐项残余。

## 0. 启动检查表（AGENTS.md §3，如实）

| 前置项 | 实际读取依据 | 状态 |
| --- | --- | --- |
| Vision 相关原则 | `goal/Lightweight_Observable_Agent_Harness_Spec/docs/spec/00_PROJECT_VISION.md` §2.2 Observable / §2.5 Reuse First / §3 冻结原则（本任务研究阶段 2026-10-06 实读，记录于 `docs/agents/352-research.md` §0） | READY |
| 当前任务规格 | `goal/.../06_CONTEXT_ARTIFACT_MEMORY.md` §3 Artifact Store / §8 Failure Semantics；`docs/PRD_RECOVERABLE_REVIEWABLE_AGENT_WORKBENCH.md` §4.2；`docs/tickets/workbench-2026-09-27/W-08-evidence-projection.md` 全文；`gh issue view 352` 全文（含 2026-10-03 审计节） | READY |
| Reuse 相关判定 | `goal/.../13_OPEN_SOURCE_REUSE_MATRIX.md` §2 Pi / §3 DeepSeek Harness / §5 BUILD / §7（研究阶段实读，`docs/agents/352-research.md` §0/§3/§4） | READY |
| Phase 依据 | issue #352（父 #344，P0，in-progress；blocked-by #350/#351 均已关）；W-08 票。注：W-* 票系不在 `14_IMPLEMENTATION_ROADMAP.md` 内，Phase 依据以 workbench 票系 + 父 issue 为准（研究阶段已如实记录） | READY |
| 本任务触发细则 | `docs/SDD_WORKFLOW_PROTOCOL.md` §1.3 / §8.8.1 / §9 全文；`AGENTS.md` §3/§6/§7/§9（含 §9.1.1）/§16.2 全文；`docs/agents/implementation-discipline.md` 全文；`docs/agents/verification.md` §1–§2 全文；`docs/agents/skills/PROVENANCE.md` §4.2；Matt `tdd` skill（`~/.codebuddy/skills/tdd/SKILL.md`）全文 | READY |

## 1. 可验证目标（AGENTS.md §9.4）

为验收项 ↔ 真实证据建立服务端关联投影：
1. 写侧：`evidence/recorded` append-only typed 事件，payload = 票面 14 字段 DTO；形状校验在 session 层 handler（坏形状→拒绝、零事件）；重跑追加不覆盖。
2. 读侧：`derive_evidence_state(events)` 纯投影，按 `criterion_id` 聚合；幂等、可 replay；不新增状态机。
3. 陈旧层：读取时求值（manifest 对比 + base_head 对比），产出明确过期原因（列出变动文件）；绝不沿用旧"通过"。
4. 传输：`POST`/`GET /api/sessions/{id}/evidence`，只回服务端投影；422/409/404 口径沿用。
5. 票面 8 个验收场景全部有测试覆盖；`VerificationEntry.evidence` 维持 `str|None`；`#524 completion_evidence` 严格区分。

## 2. 测试接缝（Matt tdd：seams）

| # | 接缝 | 测试文件 |
| --- | --- | --- |
| 1 | `event.py`：`EVIDENCE_RECORDED` 常量 ∈ `EVENT_TYPES` + 生成物（`docs/EVENT_VOCABULARY.md`、`web/src/generated/event-types.ts`）同步 | `tests/session/test_evidence.py`（词汇断言）+ 既有生成物守卫 |
| 2 | `session/evidence.py`：`apply_evidence_recorded`（写）、`derive_evidence_state`（读）、`compute_evidence_manifest`、`evaluate_evidence_freshness`（纯） | `tests/session/test_evidence.py` |
| 3 | `service.py`：`record_evidence` / `evidence_state`（薄封装，走 `_apply_task_handler` 同款写入路径） | `tests/web/test_evidence_api.py`（端到端覆盖 service） |
| 4 | `web/evidence_delivery.py`：`POST`/`GET /api/sessions/{id}/evidence` | `tests/web/test_evidence_api.py` |

## 3. 设计决策（逐项记录）

1. **事件名**：`evidence/recorded`（现有词汇风格：`task/plan_updated`、`verification/updated` 同族）。
2. **端点**：新增 `GET /evidence` + `POST /evidence`（独立 `web/evidence_delivery.py`，镜像 `web/task_delivery.py` 结构），而非扩展 `GET /task`——证据投影含陈旧求值（需读工作区+git），与 task 快照的"纯事件投影"成本模型不同；且 `GET /task` 的契约已冻结（W-07）。
3. **`ui` 类证据**：无 Chrome/MCP 时以 `result="blocked"` 记录（票面工作指令 2），绝不落 pass。
4. **`VerificationEntry.evidence`**：维持 `str|None`（人类摘要/指针），不动 W-07 契约。
5. **progress.md**：永不进入 manifest 的被覆盖文件集；其 hash 独立单列（审计红线 1）。`record_evidence` 不触发 progress 刷新（progress 文档的 evidence 节只消费 artifact 事件，本票不改它——Scope lock）。
6. **artifact 归属校验**：证据层显式做（可读回 + `tool_call_id` 交叉核对），不改 artifact 共享路由（加 `require_trusted_origin` 属共享端点契约变更，另案决策——见 §5 未决）。
7. **脱敏**：`command_or_action`/`exit_code_or_observation` 原样持久化（append-only 真相）；脱敏是记录侧（runtime）的责任，handler docstring 载明，测试钉"已脱敏值原样往返"。
8. **manifest hash 口径**：文件原始字节 sha256（不做换行归一化——字节同一性才是"版本匹配"的判据）；`manifest_hash` = 排序后 `path\tsha256` 行拼接的 sha256。
9. **base_head**：`git rev-parse HEAD`（40 hex）；非 git 目录 → `None`（如实），陈旧求值跳过该维度。

## 4. 实现进度

- [x] 切片 1（事件词汇）：`EVIDENCE_RECORDED = "evidence/recorded"` 进 `event.py` + `EVENT_TYPES`；
  重生成 `docs/EVENT_VOCABULARY.md`（65 持久化 + 2 仅广播）与 `web/src/generated/event-types.ts`；
  生成物守卫绿。commit `dd04a073`（红测 `1c3639c4`）。
- [x] 切片 2（session 层）：新建 `src/agent_harness/session/evidence.py`——`apply_evidence_recorded`
 （14 字段闭合 DTO 校验；坏形状→shape 零事件；evidence_id 重复→conflict）、
  `derive_evidence_state`（纯投影，按 criterion_id 聚合，幂等、可 replay，畸形事件跳过）、
  `compute_evidence_manifest`（显式清单 + 原始字节 sha256；progress.md 禁入覆盖集、
  hash 独立单列）、`evaluate_evidence_freshness`（读取时求值，fail-closed；明确过期原因；
  不改写 result）。`tests/session/test_evidence.py` 48 例全绿。commit `95d53b17` + `0da4e3d2`
 （红测 `91c610dd`）。
- [x] 切片 3（service + web）：`service.record_evidence`（走 `_apply_task_handler` 同款写入路径；
  不刷新 progress——progress 文档 evidence 节不消费本事件）、`service.evidence_state`
 （只读投影 + 陈旧求值：当前 manifest / `git rev-parse HEAD` / artifact 可读回 + 归属校验；
  未定义任务→None）；新建 `web/evidence_delivery.py`（`POST`/`GET /api/sessions/{id}/evidence`；
  422/409/404 口径沿用；`app.py` 一行接入）；`tests/web/test_evidence_api.py` 11 例全绿
  （含真实 git 仓库的 base_head 陈旧、源码改动陈旧、artifact 丢失陈旧、刷新重建一致性、
  敏感值原样往返）。commit `804214b4`（红测 `68d236e1`）。
- [ ] 双轴独立审查（Matt code-review）+ review ledger
- [ ] `python scripts/gate0.py` 全绿 + 全量 pytest

## 5. 双轴审查 adjudication（2026-10-06）

范围 `108ecdd5..39a4bdb0`。Standards 轴：PASS-WITH-FINDINGS（P0=0/P1=2/P2=4/P3=3）；
Spec 轴：NEEDS-FIX（P0=0/P1=4/P2=3/P3=3）。逐条裁决：

**已修复**：
- S-P1-2（每次 GET 无条件 spawn git）：`evidence_state` 仅在有记录带 `base_head` 时求 HEAD。
- S-P2-1（manifest_hash 写而不用）：`evaluate_evidence_freshness` 先比 `manifest_hash`（一致即过），
  不一致才逐文件 diff 产出明确原因。
- S-P3-1（FRESHNESS_STATUSES 未用）：删除。
- S-P3-2（可选字段五连判）：收成 `_opt_str` 统一判据。
- C-P2-1（_git_head 未走 git 围栏）：新增 `tools/git.py::git_head_command()`（逐字固定 argv、
  零插值、无 shell；scope 围栏对 rev-parse HEAD 无 pathspec 可围，cwd 钉工作区即等价纪律）；
  service 改调它。`tests/tools/test_git_tools.py` 加形状测试。
- C-P2-2（拒绝原因笼统）：`_parse_record` 改为返回 `(record, 具体原因)`，写侧拒绝逐字段点名
  （票面"保存失败时显示缺项"）；`test_rejection_reason_names_the_field` 钉住。
- C-P2-3（POST 返回无 freshness）：POST 成功后调 `evidence_state` 取同条 record（含 freshness），
  与 GET 同口径。
- C-P3-1（base_head 未按 40-hex）：改为严格 40 位 hex（与 impl §3.9 对齐）；`test_base_head_must_be_40_hex`。

**驳回（有证据/理由）**：
- S-P1-1（artifact_ref 直传 store.load 有穿越面）：不成立。`ARTIFACT_ID_PATTERN=[0-9a-f]{16}`
  在 `LocalArtifactStore.load` 内先 `fullmatch` 后才拼接路径（`local_artifact.py:138-140`），
  畸形 id 进不了路径层；失败 fail-closed 判 stale。不复制 store 的形态契约。
- S-P2-2（EvidenceState.to_payload 未用）：保留。投影接缝的 canonical 序列化，测试用它断言幂等。
- S-P2-3（证据 I/O 在 service.py 是 Divergent Change）：保留。task.py 纯领域 + service.py I/O
  正是本仓既有分层（task_* 同构），跟随既有范式。
- S-P2-4（EvidenceOutcome 与 TaskOutcome 重复）：保留。泛型化要动 task.py（scope 外）。
- S-P3-3（captured_at 未校 ISO）：保留。记录侧（runtime）经 `_utc_now_iso` 设值；简单优先。
- C-P1-1（source_event_seq/tool_call_id 均可 None）：保留。reviewer 结论类 external 证据本就没有
  工具调用；票面工作指令 1 明确把 reviewer 结论列为证据来源。kind=external 即无来源引用形态。
- C-P1-2（workspace_manifest 非标量偏离票面第 14 字段）：不成立。PRD §4.2 冻结的是语义内容
  （"修改文件清单、基线 HEAD、工作区文件摘要清单"），不是标量形态；2026-10-03 审计红线要求
  "精确文件 manifest + 独立元数据 hash + 明确过期原因"——标量 hash 给不出"列出变动文件"；
  `manifest_hash` 标量仍在 bundle 内；父任务设计方向（§5.4）明确即此形态。
- C-P1-3（"diff 内无 tests/生成物"）：审查者误读——我给的 diff 文件只含了 `src/**`（打包疏漏），
  测试与生成物均在分支内：`tests/session/test_evidence.py`（50 例）、
  `tests/web/test_evidence_api.py`（14 例）、`docs/EVENT_VOCABULARY.md`、
  `web/src/generated/event-types.ts`（commit `dd04a073`）；生成物守卫实测绿。
- C-P3-2（captured_at 未校 ISO）：同 S-P3-3，保留。
- C-P3-3（derive 不过滤已删验收项）：保留。历史证据是 append-only 事实（与
  `derive_task_state` 保留旧验证值同语义）；与当前清单的 join 是消费侧的事。

**残余**：
- C-P1-2（审计红线"权限检查"）：证据层已做会话命名空间可读回 + 归属校验；artifact 路由级
  `require_trusted_origin` 属共享端点契约变更，维持另案决策（impl §3.6 / §5.1）。
- C-P1-3（人工/机器分列）：kind 语义已文档化（test/ui/diff=机器，external=人工/外部结论）；
  接受轴独立承载用户裁决（`test_derive_unaffected_by_acceptance_axis`）。
- C-P3（evidence_id 去重→409）：票面未写但属合理防护（防同一证据双记；重跑用新 id 不受影响），
  记为已决策细节。

## 6. 未决/残余事项

1. artifact 路由是否补 `require_trusted_origin`：本票只做证据层显式归属校验；路由级加固另案（需用户决策）。
2. `ui` 类证据的采集来源（W-18 Chrome MCP 未到）：当前仅支持以 `blocked` 表达"缺工具"。
3. `test_readonly_target_fails_explicitly`（tests/session/test_progress_file.py）在 root 用户下
   确定性失败（chmod S_IREAD 挡不住 root 写）：环境性、与本票无关（diff 未碰 progress.py）。

## 7. 修后重审（2026-10-06，范围 804214b4..39a4bdb0，仅 297 行）

- **Standards 轴 REREVIEW**：既有 7 项修复全部闭合（逐条核对 diff 行）；新发现 P2×1 + P3×3：
  P2（POST `projection is None` 时静默回 `{}`）→ 改为 loud 500（fail-closed；该分支按构造不可达：
  record 成功 ⇒ 任务已定义 ⇒ `evidence_state` 不可能回 None）；
  P3（写后二次 `evidence_state` 重算开销）→ 接受为权衡（POST 非热路径，正确性优先）；
  P3（`_manifest_error` 兜底近不可达）→ `_parse_manifest` 改为返回 `(manifest, 具体原因)` 元组，
  死分支消除；P3（未知键检查双份）→ 只留 `_parse_record` 一处。
  结论 REREVIEW-STANDARDS: NEEDS-FIX (P0×0/P1×0/P2×1/P3×3)，修复已落地（commit `bdbd04ed`）。
- **Spec 轴 REREVIEW**：既有 4 项修复全部闭合（逐条语义核对）；新 diff 有限发现：**无新 Spec findings**；
  票面验收逐项核对未修坏（懒求值/manifest_hash 等价优化/progress.md 独立单列/敏感值不进正文）。
  结论 REREVIEW-SPEC: CLEAN (P0-P3=0)。
- 说明：Spec 审查者抽查时读到了我正在落盘的 Standards 修复中间态（送审 diff 与工作树短暂不一致），
  其闭合判定不受影响（语义一致）；最终工作树即其所见的终态。

## 8. 终验读数（2026-10-06）

- **Gate-0**：`2dadbc8c` 上 6/6 PASS（diff-check / ruff / oxlint / tsc / guards / coverage），
  读数 `docs/gate/2dadbc8c582e8d519a314409b8ea25f780a12119.json`（20.4s）。
  过程中修过两处 gate 暴露的问题：① tsc 要求 `EVENT_SEMANTICS` 穷尽登记 →
  `web/src/lib/projection.ts` 补 `EVIDENCE_RECORDED` no-op（另起 mini 双轴审查，均 CLEAN，
  台账第二行）；② `test_all_event_types_registered` 硬编码期望集缺新类型 → 补 `evidence/recorded`
  （测试枚举机械跟进，台账第三行）。
- **回归**：tests/session 768 passed（仅 `test_readonly_target_fails_explicitly` 在 root 下
  环境假红，AGENTS.md 已记）；tests/web 730 passed；tests/tools 164 passed。
  环境坑：`no_proxy` 含 IPv6 条目会使 httpx 报 `Invalid port: ':1]'`（测试前
  `export no_proxy=localhost,127.0.0.1`）；venv 需补 `langgraph`/`langmem`/`httpx2[ws]`。
- **冻结**：HEAD `c687702c`（tree `a83077d7`），`git diff --check` 干净；未 push/PR/merge/关单。
