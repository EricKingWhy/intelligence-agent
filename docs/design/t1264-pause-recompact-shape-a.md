# #1264 设计票 · Shape A：循环内**有界**再压（不暂停、不引入新状态）

- 执行器：第八任协调员 B2（2026-10-17）。状态：**用户 2026-10-17 06:0x 裁决「批准 Shape A 落地」**，停线解除。
- 票面：issue #1264 `[context] 压缩暂停真功能：暂停→后端再压→成功自动恢复/失败诚实框`（AC1–AC5）。
- 本文件是**唯一**设计结论落点（【上下文节食令】第 3 条）。票面**不修改**。

## 0. 一句话结论

票面原状态机（暂停 → 后端再压 → 成功自动 `run/resumed`）已被 E1–E9 一手证据证伪（三重交叉验证），**结构上不可行**；
落地方案 = **在 run 主循环内、终态写入之前，对"可重试的压缩失败"做至多一次再压**：
成功 ⇒ **零操作续跑**（不是"恢复"：不写 `resume_basis`、不发 `run/resumed`）；失败 ⇒ 走**既有**暂停 + 诚实 `ContextFullDialog`。

## 1. Shape B（暂停后自动恢复）为什么被证伪 —— 一手证据

- `src/agent_harness/agent/runtime.py:3435`、`:3749`：暂停/终态臂调用 `arms.terminal.mark_terminal_written()`。
  该方法一旦执行，**之后所有终态写入被拒**。暂停事件落盘后，run 在状态机里已经终结，
  "自动恢复"没有任何合法写路径 —— 只能新开 run（那就不是 AC2 要的"用户无操作"）。
- 结论：Shape B 不是"难做"，是**与不变量冲突**。ADR-0044 的暂停/恢复语义保持不动（Shape A 不碰 ADR-0044）。

## 2. 插入点（最小改动面，只碰一处）

`src/agent_harness/agent/runtime.py:1861-1882`，主循环 `while True:`（:1749）内、第 0 步 steer（:1817）之后：

```python
try:
    messages = await self._context_builder.build(session)
except ContextWindowExceededError as error:
    # ← Shape A 在此插入：有界再压一次
    ...  # 失败才落到既有的 _terminal_context_exceeded(...)
```

此刻 `steps` / `arms` / `context_event_start` / `session_step_reserved` 全部存活，因此：

- 同一次迭代内可以合法地再压一次（`await self._context_builder.compact_now(...)`）；
- 再压成功 ⇒ **不 return**，直接落到 :1896 起的既有镜像段
  （`new_events = list(session.since(context_event_start))` → `for event in new_events: yield to_agent_event(event)`），
  然后正常发起模型调用 —— 这就是 **AC2「零操作续跑」** 的机制：run 从未终结，没有恢复动作可做。
- 再压失败 ⇒ 原样走既有 `_terminal_context_exceeded`（:3605）→ 非 `ProtectedFactBudgetExceededError`
  → `_terminal_paused(trigger_dimension=TRIGGER_MAX_CONTEXT_TOKENS)`（:3640-3645）。

## 3. 重试判定：可重试 vs 结构性

`build()` 抛出的 `ContextWindowExceededError` 有 7 条来源，**只有一条**值得再压：

| 来源 | 位置 | 判定 |
|---|---|---|
| 摘要双失败 + 无截断可能 `"Summary validation failed and original context exceeds hard guard"` | `compactor.py:1031` | **按 failures 分类**（唯一候选） |
| 压缩后仍超硬护栏 `"Compaction cannot fit context: ..."` | `compactor.py:1023-1030` | 结构性，不再压 |
| 落 bracket 后重投影仍超硬护栏 `"Re-projected compaction still exceeds hard guard"` | `builder.py:915-919` | 结构性 |
| 抖动闸门 `_THRASHING_GUARD_MESSAGE` | `builder.py:1139`（阈值 3，`builder.py:309`） | 结构性（已连续 3 次预检拒绝） |
| `"Refusing to persist an unvalidated compaction summary"` | `builder.py:1154` | 结构性 |
| `"Refusing to persist compaction without source event range"` | `builder.py:1161` | 结构性 |
| `ProtectedFactBudgetExceededError` | `builder.py:1022` | 结构性 |
| `CompactionPostWriteError` | `builder.py:1218/1238` | **禁止重试**（bracket 已在 JSONL） |

因此判定式 = 异常是 `ContextWindowExceededError`、**不是** `ProtectedFactBudgetExceededError`、**不是** `CompactionPostWriteError`，
且 `error.failures` 非空且 **全部** `error_class` ∈ 瞬时集：

- 瞬时（模型侧抖动，重试有意义）：`timeout`、`transport_error`、`empty_summary`、`tool_calls_in_response`、
  `non_text`、`heading_mismatch`、`empty_section`、`programmatic_mismatch`、`plan_section_mismatch`
- 结构性（重试无意义）：`summary_not_smaller`、`target_not_reached`、`source_range_unavailable`、
  `preflight_request_exceeds_hard_limit`、`deterministic_truncation_fallback`、`chunk_bound_released`、`no_compactable_early_turn`

该划分是对 `_SUMMARY_ERROR_CLASSES`（`compactor.py:86-120`，16 项有界词表）的**穷尽二分**，不新增词条。

## 4. 与「compactor 内部已重试两次」的关系（必须显式交代）

- `compactor.py:875`：`for attempt in (1, 2):` —— 单次 `compact_now` 内**已经**最多两次摘要尝试
  （注释：「W-04 (#348)：摘要生成至多两次尝试」）。
- 因此 Shape A 的运行时再压是**第三层**尝试。奥卡姆剃刀的质疑成立，故施加硬约束：
  - **至多一次**（见 §5 一次性闸门），总请求上限 = 2（首轮内部）+ 2（再压内部）= 4，
    与 oh-my-pi 明确警告的"10 外层 × 3 内层 = 30 请求"（`oneshotRetry` docstring）不同量级；
  - 再压**只在"首轮失败且可归类为瞬时"时**发生 —— 结构性失败一次都不再压。
- 为什么不直接依赖内部两次：内部两次的失败面**不进 runtime**，runtime 无从区分"压过了但失败"与"根本没压"，
  也就无法在 AC2/AC3 之间做出选择。**运行时拥有这个决策**才是 Shape A 的实质，而不是"多试一次"。
- 既有降级链不变、不重复：`_deterministic_truncation`（`compactor.py:246-291`）在 `token_estimate > hard_limit`
  时已经先试确定性截断；截断成功则 `result.fallback_used=True` → `builder.py:1149` 返回，**根本不会抛到 runtime**。

## 5. 一次性闸门（AC4 幂等 / 不重复触发）

照搬 Pi 的 `_overflowRecoveryAttempted`（`agent-harness/reference` 侧 `pi`：
`packages/coding-agent/src/core/agent-session.ts:395` 声明、`:1107`/`:1158` 重置、`:2995-3040` 使用）：

- 在**单次 run** 作用域内持有一个 bool（随 `arms` 走，run 结束即消失 —— 不引入新状态、不进 SessionEvent）；
- 首次命中可重试失败 ⇒ 置位并再压一次；
- 已置位 ⇒ **直接走既有暂停**，不再压。因此并发/重复到达同一失败点时不会重复触发（AC4）。

超时兜底（AC4 后半）：再压复用既有 `compact_now`，其超时/传输失败已经是**有界**的
（内部两次尝试 + `_SUMMARY_ERROR_CLASSES` 词表），不新增等待面；再压自身不设新超时。

## 6. AC1「前端显示"正在压缩"，当且仅当后端真有压缩在途」的事件链（真实事件驱动）

一手链路（无需新订阅机制）：

1. `ContextBuilder.compact_now` 校验全过后才铸 `bracket_id`（`builder.py:1176`，注释引 Pi `session-manager.ts:1262-1288`）
   并 `session.append(COMPACTION_START/CONTEXT_COMPACTED/COMPACTION_END)`（`builder.py:1181/1186/1205`）。
2. `Session.append` **落盘成功后**才回调监听器（`session.py:500-520`；注释「写盘成功后才推进计数器——失败不消耗 seq」）。
3. `RunManager._drive` 注册 `run.session.add_listener(run._on_session_event)`（`runmanager.py:467`，`finally` 摘除）。
4. `enqueue_session_event` 把 SessionEvent 镜像成 AgentEvent（`runmanager.py:170-176`），`_fanout` 推给 run 订阅者
   与 session 级跟随者（`runmanager.py:230-245`；「监听器先入、镜像跳过」，seq 幂等）。
5. Web 走 `GET /api/sessions/{id}/stream?after_seq=N`（`app.py:3015` `stream_session`；客户端 `api.ts:760` `streamSession`）。
6. 前端投影：`projection.ts:369-375` `projectCompactionInProgress` / `projectCompactionEnded`，
   注册在 `:1901-1902`；`types.ts:752` `compaction_in_progress`。

⇒ 压缩在途时 `compaction/start` 已到、`compaction/end` 未到，`compaction_in_progress === true`；
`finalizeRun`（`projection.ts:2159`，#1197 项 5A）保证 bracket 中途夭折时 flag 跟着收口。
**事件对真实配对**由 `derive.py:1345-1400` 的 bracket 配对（要求 `start[2] < summary[3] < end_position`）机械保证；
失败尝试**零 bracket 写入**（`bracket_id` 在校验之后才铸），故不存在半截 bracket 污染 —— 重试安全。

**缺口**：`compaction_in_progress` 目前**没有任何 .tsx 消费者**（grep 全仓仅 `types.ts` 声明 + 测试）。
Composer 里那句「正在压缩上下文…」（`Composer.tsx:1090-1095`，`data-testid="slash-compacting"`）
由**手动** slash 路径的 `useCompactSession().compacting`（`useCompactSession.ts:27`）驱动。
⇒ AC1 的落地 = 让该状态行**也**由投影 flag 驱动（自动路径同样可见），文案沿用 #1197 项 5 一手文案。

**不做**：不新增事件类型（复用既有 bracket 事件对），因此**不触碰**
`web/src/generated/event-types.ts`、`docs/EVENT_VOCABULARY.md`、`projection.ts` 的 `Record` 登记表
（`scripts/gen_event_types.py` / `gen_event_vocabulary.py` 无需重跑；gate0 `guards` 车道仍绿）。

## 7. AC3 诚实框：`ContextFullDialog` **不动**（复核后推翻初判）

初判曾认为 `ContextFullDialog.tsx:52` 的「前面没有可压缩的内容，换个窗口更大的模型继续吧。」
在瞬时耗尽路径上是假话，拟改文案。**复核后推翻**，三条一手依据：

1. 任务书原文把这句话**指定为**诚实文案：「再压失败必须诚实显示**"前面没有可压缩的内容"**
   一类真话」；
2. AC3 原文「文案与 #1197 项 2 / 项 3 一致」，而 `gh issue view 1197` 项 2 的一手裁决文案是
   「上下文满了，**前面没得再压**，换个窗口更大的模型继续吧」—— 同一句话的两种措辞；
3. `docs/ux-mockups/2026-10-16-context-compact/mockup.html:714`（#1197 的**已批准制品**）
   逐字就是这句。

⇒ **本票零文案改动、零新 prop**。票面、任务书、已批准 mockup 三处同源，改它才是扩权。

**语义上它也站得住**：走到这条臂的前提是「摘要两次没压成 + 确定性截断也放不下」
（`compactor.py:1020`）——"**可压的**内容"（能被摘要压进硬护栏的内容）确实不存在，
用户能做的确实只有换更大的窗口或结束。§3 的"结构性失败不再压"正是同一事实的另一面。

## 7bis. 请求数上界（可观测，非估算）

再压走 `compact_now`，其内部 `for attempt in (1, 2)`（`compactor.py:855`）不变 ⇒
**单次 run 的摘要请求上界 = 首轮 2 + 再压 2 = 4**（原先 2）。这正是 oh-my-pi
`oneshotRetry` docstring 警告的"预算相乘"的**有界**形态：层数 2、每层默认 1，
不是 10×3。

## 8. AC1 命名冲突（票面不改，做映射）

AC1 票面写 `run 进入 PAUSED_COMPACTING（新）`。Shape A **明令不引入新状态**
（「不引入 PAUSED_COMPACTING 等新状态，不碰 ADR-0044，不碰 resume_basis 冻结集合」）。
⇒ 票面**不修改**，映射如下：

| AC1 票面措辞 | Shape A 实现 |
|---|---|
| 「不直接弹框」 | 压缩在途时 run 不终结、不写暂停事件（§2 插入点在终态写入之前） |
| 「前端显示"正在压缩上下文…"」 | `compaction_in_progress` 驱动状态行（§6） |
| 「后端确在压（测试断言状态机，非推断）」 | 断言 = 会话里存在 `compaction/start` 且尚无 `compaction/end`，同时 run 未终结 |
| 「PAUSED_COMPACTING（新）」 | **不实现该状态**：续跑路径无暂停（§2）；失败路径落既有 `run/paused`（`trigger_dimension=max_context_tokens`，`reason` 走既有映射，不新增 reason 值） |

## 9. 方案依据（AGENTS §6.1 / 协议 §1.3）

**结论：PORT DESIGN（只移植设计，不抄代码）。** 两来源均 MIT。

- **来源 1 —— Pi**（`earendil-works/pi @28dcce2ba45ce4a9efeb0f5b686f0be830fd89b9`，MIT，
  `packages/coding-agent/src/core/agent-session.ts:2995-3040`）：
  溢出时 `_overflowRecoveryAttempted` 一次性标志（`:395` 声明，`:1107`/`:1158` 重置）；
  首次溢出 ⇒ 压一次并重试；已试过 ⇒ 不再重试，返回**诚实错误**
  `"Context overflow recovery failed after one compact-and-retry attempt. Try reducing context or switching to a larger-context model."`
  —— 与本方案的「至多一次 + 失败诚实报错」逐条同构（差异：Pi 失败即报错，本仓失败落**既有暂停 + 诚实框**，
  因为本仓有暂停语义而 Pi 没有）。
- **来源 2 —— oh-my-pi**（`@579da1d661c5cb8d43bc2ddd429ab72e67165adc`，MIT，
  `packages/agent/src/compaction/compaction.ts:715-745`）`oneshotRetry` docstring：
  「Pass `false` when the CALLER already owns a retry loop around the whole compaction attempt —
  otherwise the two budgets multiply (10 outer attempts x 3 inner = 30 requests)」；
  以及本仓已存档调研 `docs/RESEARCH_OHMY_PI_DSH_COMPACTION.md:227,263`：
  `compactionRetries`（默认 1，摘要调用层）+ `maxOverflowRetries`（默认 1，溢出后 condense-and-retry 路径）
  —— 即成熟产品**同样分层**且**每层默认 1**，与 §4 的硬约束一致。
- **与不变量契合**：不变量 #3（append-only 类型化 SessionEvent）——不新增类型、复用既有 bracket；
  #4（Event ≠ Diagnostic Log）——失败诊断走既有 `context/compaction_failed` 有界通道；
  #22（Web UI 不得维护第二套不可对账真相）——前端只读投影 flag，不推断；
  Vision L105「Recovery before Retry」（**不是** `AGENTS.md` §7 的 #8 —— 那条是
  "Tool Retry 只有 ToolExecutor 一个责任域"）——再压不是盲重试，先按 `error_class` 分类。
- **许可**：两来源均为 MIT；本仓仅移植设计（一次性闸门 + 分层预算），不复制代码，
  来源与差异记录于本文件与 commit message。

## 10. 落地清单（改动面）

后端：
1. `src/agent_harness/agent/runtime.py` —— 插入点（§2）+ 可重试判定（§3）+ 一次性闸门（§5）。
2. 复用 `ContextBuilder.compact_now`（`builder.py:952`），**不改** compactor 的失败词表与降级链。

前端：
3. `web/src/components/Composer.tsx` —— 状态行条件并入投影 flag（§6）。
4. （无）`ContextFullDialog.tsx` **不动** —— §7 复核后推翻初判。

**不碰**：ADR-0044、`resume_basis` 冻结集合、`_SUMMARY_ERROR_CLASSES` 词表（只**增**一份
与之同 owner 的 `_TRANSIENT_ERROR_CLASSES` 二分）、`ContextFullDialog.tsx`、
`web/src/generated/*`、`docs/EVENT_VOCABULARY.md`、`projection.ts` 的 `Record`。

## 10bis. 步/预留记账（#318 契约不变）

再压成功 ⇒ 本轮**继续**：`session_step_reserved` 置回 False 但**不退款** —— 这一步真的
用掉了（模型即将被调用），退款才是记假账。再压失败 ⇒ 走既有退款臂（预留整步退回）。
两分支互斥，由 `continue_after_recompaction` 单点判定。

**已知口径偏差（双轴审查 P2 修回，如实记账）**：本票把"预留成为真账"说全了，
但**再压自身那 2 次摘要请求（`compactor.py:899` 的 `ainvoke`）从不落 `model/request`
事件** —— 该事件的唯一生产点是 runtime（`runtime.py:2935`），压缩链不过它；
而 session 的 `model_requests` 计数只认 `MODEL_REQUEST` 事件
（`runtime.py:_record_active_session_model_requests`）。因此走了再压的 run 会
**少记**再压的真实 provider 请求数（最多 2）。
- 这**不是**本票引入的偏差：手动 `/compact` 与首轮压缩的摘要请求同样不落账，
  本票只是把同一事实多发生一次；改动它属于"压缩请求要不要进 session 账"的独立
  议题（牵动 `_SUMMARY_ERROR_CLASSES` 之外的记账契约与 run 作用域镜像），
  超出 Shape A 的最小改动面，**本票不开**（发现即记，不夹带）。
- 方向上偏差**偏松**（少记 ⇒ 用户不会因再压被提前暂停），不制造越权。
- 回归钉：`tests/agent/test_1264_inloop_recompaction.py::test_retry_success_keeps_the_session_step_reserved`
  只钉"预留不退"这一半；再压请求的记账面**明确不在本票断言范围**。

## 10ter. 双轴审查修复（2026-10-17）

- **P1-a（Standards 轴）** `_is_retriable_compaction_failure` 只看 `attempt >= 1` 的失败：
  `attempt=0` 的非摘要诊断（`chunk_bound_released` 等）不得当否决票（§3 判定式已按此收紧；
  与 `builder.py:_backfill_artifact_ref` 同一条口径）。回归钉：
  `tests/agent/test_1264_inloop_recompaction.py::test_benign_attempt_zero_diagnostic_does_not_veto_the_retry`
  与反向的 `::test_failure_surface_with_no_summary_attempt_is_not_retried`。
- **P1-b（Standards 轴）** `App.tsx` 的 `compactionInProgress` 补会话身份闸门（照 `sessionCwd` 写法）：
  切会话时 `conversation` 仍是旧会话，不设闸门会把旧会话的"压缩在途"残影画到新会话上（不变量 #22）。
  回归钉：`web/src/App.compactionInProgressIdentity.test.tsx`（红证已做：摘掉闸门该文件 1 红）。
- **P2** 删掉 `tests/context/test_1264_transient_failure_classes.py` 里恒真的 partition 断言；
  compactor 注释改为如实描述守卫强度（新增词条会静默落结构性侧，测试不报错）。
- **P3** 陈旧行号（post-image）与不变量出处更正；测试走公开 `context_builder=` ctor 形参。
- **P2（Correctness 轴，3 条，均为测试/记账缺口，非交付路径缺陷）**
  1. 闸门用例 `test_the_gate_allows_at_most_one_retry_per_run` 原本**恒绿**：两条失败
     都写在同一次迭代的脚本里，第一次失败时 `compact_now` 自身就抛，走不到第二次
     build（删掉闸门该用例照样过）。已改为跨**两轮迭代**（第一轮带工具调用），
     并做变异验证：删掉 `not arms.compaction_retry_attempted and` ⇒ 该用例转红。
  2. 续跑臂 `session_step_reserved = False`（不退预留）原先**无覆盖**：本文件所有
     用例都不传 `session_budget`，`#318` 整段是死代码。已加
     `test_retry_success_keeps_the_session_step_reserved`（模型失败收尾 ⇒
     `consumed.agent_turns` 保持 1），变异验证：删掉那行 ⇒ 该用例转红。
  3. §10bis 的记账口径偏差（再压的摘要请求不落 `model/request`）—— 见上，如实记账、本票不开。

## 11. 其他取证备注

- 任务书五项开工清单里的 `web/PRODUCT.md` **在本工作树不存在**（`sed` 报 No such file or directory）。
  前端口径的实际来源 = `docs/ux-mockups/2026-10-16-context-compact/mockup.html`
  （:701「正在压缩上下文…」、:714「前面没有可压缩的内容…」，即 #1197 项 5 的两处一手文案）。
- `docs/spec/*` 一律以 `goal/Lightweight_Observable_Agent_Harness_Spec/docs/spec/` 为准
  （AGENTS §1：根 `docs/spec/` 是流式 UI/Workspace/Web UI 规格族，不是 Engineering Specification）。
