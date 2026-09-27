# ADR-0047 — 完成判定：Runtime Quiescence 强制 + CompletionPolicy 可插拔

- **Status**: Accepted（实现随 T8/#316 落地）
- **Date**: 2026-09-26
- **Deciders**: 用户（#305 的产品与架构裁决，2026-09-23 冻结）+ 本 Agent（票面实现口径）
- **Related**:
  - 父 PRD **#305**、本票 **#316**（T8/12：「Require quiescence and CompletionPolicy before completion」）、
    上游 **#308**（T3：预算维度接入 loop）、**#312**（T4：非终态 `run/paused`）、
    **#313**（T5：四维 run ceiling）、**#315**（T7：绝对 deadline 与对账耦合）
  - **本 ADR 展开的正式规格**：`02_AGENT_RUNTIME.md` **§5.4**（六条 quiescence 谓词、唯一
    `CompletionPolicy` seam、不静止时的处置）、`03_SESSION_EVENT_MODEL.md` **§5**（run 状态集合
    `active | paused | completed | failed | interrupted | needs_reconcile`；`paused` 只由
    预算 / deadline / stuck 三类原因产生）、`02 §9` 的 MUST NOT（完成规则不得硬编码进 Core）
  - **ADR-0046 §2 D4/D5** —— "副作用未证"的 durable 载体与「对账优先于恢复」；本 ADR 复用同一份
    落盘事实（`storage.needs_reconcile` / `storage.UNPROVEN_SIDE_EFFECT_KEY`），**不**另立判据
  - **ADR-0044 D4/D6**、**ADR-0033**（失败归因面）、**ADR-0014**（同错熔断护栏——本票不动它）
  - 代码落点：`agent/completion.py`（新）、`agent/runtime.py`、`agent/types.py`、
    `session/approval.py`、`storage/operation.py`（只读）

---

## 1. Context

### 1.1 票面要解决的问题

此前"本轮模型没有再请求工具"等同于完成（`runtime.py` 第 5 步 `if not tool_calls:` 直接走
`_terminal_completed`）。这条判据只看了**最新一条模型消息**，而系统里还有一批跨组件在途状态：
已接纳的 tool_call、`ApprovalRequest`、子 Agent、Operation Ledger 记录、reconcile 欠账。
任何一条未结清时落 `run/completed`，都是把"缺失结果 / 未知副作用"当成完成
（`02 §5.4` 明文禁止）。

### 1.2 现状实测（本 ADR 写作时的读数）

| 事实 | 证据 |
| --- | --- |
| 六条谓词的 durable 事实源**都已存在** | 悬空：`session/derive.detect_dangling`（底层 `collect_dangling` 是同一份判定）；审批：`tool/approval-requested` / `permission/resolved`；子代理：`agent/delegation-started` / `-finished`；账本：`OperationLedger.list_for_session` + `storage.needs_reconcile` |
| 完成边界只有一个，且已收口在一条臂上 | `runtime.py` 第 5 步 → `_terminal_completed`（`#263`/`#264` 的 golden 基线冻结了顺序与 append 次数） |
| 非终态收口已有先例（不落终态、run 逻辑上仍开着） | `_terminal_paused`（`#312`）；恢复扫描给悬空 run 补 `run/interrupted`（`03 §5`） |
| `detect_dangling` 只认"有请求无结果" | 恢复期合成的 `tool/result`（`Session.resume`，`_mark_dangling=True`）就是 `02 §5.4` 第 1 条里的**恢复分类**，故它不再算悬空 |
| 被准入闸门拒掉的调用**不**留未结清行 | `tooling/executor.py`：拒因返回带 `budget_delta` 的失败 `ToolExecution`（有 `tool/result`），Ledger 行只在全部闸门通过后 `create`（PENDING），串行分支的剩余调用落 `CANCELLED` |

### 1.3 本 ADR 的范围

**在**：六条谓词的唯一判据与聚合；seam 的位置与调用顺序；不静止时的**表示**（不落任何事件、
保持未解 owner 活动）；`CompletionPolicy` 的形状与默认策略；重入与单终态的落点。

**不在**：stuck（`#317`，`run/paused` 的第三个原因）；委派树共享预算与树内记账（`#318`）；
blocked 状态在 SSE/WS/CLI/Web 上的**渲染**（客户端票）；暂停 / 恢复的 UX；reconcile 裁决的提交面。

---

## 2. Decision

### D1 — 六条谓词各有一个既有 durable 事实源，聚合是**纯函数**

`agent/completion.py::collect_quiescence_report(*, events, operations, new_tool_calls)` 只吃
三份**已经落盘**的输入（SessionEvent 列表 / 账本行 / 最新模型决策是否请求工具的布尔），返回
`QuiescenceReport`。它不碰存储、不认识 Runtime——所以每条谓词都能用"造事件 / 造账本行"的
确定性用例单独驱动。

| # | 谓词（`02 §5.4`） | 判据 | blocker kind |
| --- | --- | --- | --- |
| 1 | 没有已接纳但缺结果或恢复分类的工具调用 | `derive.detect_dangling(events)` 非空 | `dangling_tool` |
| 2 | 没有未决的 `ApprovalRequest` | `session/approval.unresolved_approval_ids(events)` 非空（`approval_id` 维度配对：requested 减 resolved） | `unresolved_approval` |
| 3 | 没有活动或待恢复的子 Agent | `agent/delegation-started` 减 `agent/delegation-finished`（`child_session_id` 维度）非空 | `active_child` |
| 4 | 没有 pending / unknown 的 Ledger 记录 | 账本行 `state ∈ {PENDING, RUNNING, UNKNOWN}`（`RUNNING` 也算：它同样不是"已结清"） | `unsettled_operation` |
| 5 | 没有 pending 的 reconcile 操作 | 账本行 `state is NEED_RECONCILE` **或** 带"副作用未证"标记（`storage.has_unproven_side_effect`） | `pending_reconcile` |
| 6 | 最新被接纳的模型决策不再请求新工具调用 | 调用方传入的 `new_tool_calls` 为真 | `new_tool_calls` |

**作用域是 session，不是 run。** 谓词 1–5 的对象（事件与账本行）都是会话级 durable 事实：
它们描述的是"这个会话里还有没有未结清的世界状态"。按 run 过滤会把上一个执行留下的欠账
放行成"本次 run 完成"——正是 `03 §5`「对账优先于恢复」要挡住的事。谓词 4 与 5 的分界不是
重复实现：4 = 未定 reconcile 状态的在途 / 未知行（`07 §6`：`PENDING` 可证明"没开始"，
但"没开始"≠"已结清"，完成闸门照样挡住它），5 = 已进对账流程的欠账。两者的**并集严格覆盖**
服务层恢复闸门用的 `storage.needs_reconcile`（`#315`）——它**故意不含 `PENDING`**（"还没开始"
不欠对账、可重执行），而完成闸门连 `PENDING` 一起挡；三个读者读同一份落盘事实，判据各自
如实收窄，不是同一把尺子。

**没有 Operation Ledger 的部署**（`executor.operation_ledger is None`）：谓词 4/5 空真（没有行
可查），报告照常给出。这是"证明账本里没有欠账"的诚实读数，不是豁免。

### D2 — seam 位置与顺序：先 Quiescence，后 CompletionPolicy

唯一落点是**既有的最高完成边界**（`runtime.py` 第 5 步 `if not tool_calls:` 那一支，紧挨
`_terminal_completed` 之前）。顺序即契约：

```text
最终响应（无 tool_calls）
  → collect_quiescence_report(...)
      ├─ 有 blocker ⇒ **不调用** policy，走 D3 的 blocked 收口
      └─ 无 blocker ⇒ 调 completion_policy.decide(...)
            ├─ accepted      ⇒ _terminal_completed（既有臂，一字不改）
            └─ not accepted  ⇒ 走 D3 的 blocked 收口（reason = policy 给的理由）
```

`02 §5.4` 的原话是"调用它**之前** MUST 先证明六条"——所以非静止时 policy **根本不被调用**，
"域策略绕过 quiescence / 权限 / 账本检查"在实现上不可能发生（不是靠约定，是靠调用图）。

### D3 — 不静止的表示：**blocked 臂零写入**，保持未解 owner 活动

`02 §5.4` 给的三条出口里，本票选第一条（"保持未解 owner 活动"），因为另外两条在该时点都
没有准确的载体：

- `run/paused` 被 `03 §5` 冻结成只由**预算 / deadline / stuck** 三类原因产生（重复原因
  ⇒ 要么伪造一个不存在的触发维度，要么与 `#317` 的 stuck 语义撞车）；
- 这些 blocker 的"暂停"也不该暗示"抬高 ceiling 就能续跑"（ADR-0044 D4 禁止的正是这种
  误导性暂停）；而 reconcile 类欠账的真实载体**已经存在**——`operation/reconcile-required`
  事件 + `NEED_RECONCILE` 账本行 + 恢复闸门的 409（`#315` / ADR-0046）。
- `run/interrupted` 是**崩溃恢复态**（`03 §5` 点名的唯一产生者是启动扫描），运行期自己写
  它等于伪造一次崩溃。

所以 blocked 收口是：**本次执行正常结束，blocked 臂自己不写任何 SessionEvent、不落终态**。
逻辑 run 仍开着，未解 owner（悬空调用 / 未决审批 / 子会话 / 账本行）**原样留在 durable 状态里**，
这就是它准确的表示。

写清边界（两轴审查订正）：走到闸门之前，本轮**已经**按既有的稳定边界落了这一步的
`model/completed`，并按同一稳定边界**调用** checkpoint 保存（`OnStableBoundary`；
未接 `checkpoint_store` 或策略是 `NoCheckpoint` 时那次调用是空操作）。关掉
`defer_model_event` 的那一支恰好就是"模型不再请求工具"这一步。"零写入"说的是
**闸门这个臂**——它不追加终态、不推 reconcile、不改账本、不写 Checkpoint、不做记忆形成；
不是"这次执行在 durable 面上没有痕迹"。

可观察面三处：

1. `AgentRunResult.status = STATUS_QUIESCENCE_BLOCKED` + `reason`（进程内调用方，如子 run 的
   `provider.py`、CLI）；
2. Diagnostic Log 一条 `agent_decision`（`decision="blocked"`，`blockers` 只含 kind 与 id，
   **不含任何凭证或参数值**）；
3. durable 状态本身：会话账本与事件流里那条未结清的事实，客户端读它，与 replay 同一份。

**wire 层（SSE/WS/CLI/Web）对这个状态的专门渲染不在本票**（见 §4 残余 2）。

### D4 — 拒绝是**无副作用**的

blocked 路径不写 Ledger、不 append 事件、不写 Checkpoint、不做记忆形成、不推 reconcile。
理由有两条，都是契约级的：

- AC「A rejecting custom CompletionPolicy prevents run/completed and reports a stable reason
  **without altering quiescence state**」——"不改变 quiescence 状态"在实现上就是"这条路径没有任何写"；
- 完成闸门的职责是**拒绝**，不是**分类**。分类归暂停收口（`_raise_reconcile_required`，T7：
  那一步的理由是"暂停暗示可恢复，所以必须先把未证行点成欠账"）与崩溃恢复（`RecoveryCoordinator`）。
  拒绝路径顺手改账本会让"拒绝 / 重试"变成非幂等操作，而它恰恰会被重复执行（每次重入闸门都会跑）。

### D5 — `CompletionPolicy`：一个 ABC、一个默认实现、一个决定对象

```python
class CompletionPolicy(ABC):
    @abstractmethod
    async def decide(
        self, *, report: QuiescenceReport, final_text: str, run_id: str,
    ) -> CompletionDecision: ...

@dataclass(frozen=True)
class CompletionDecision:
    accepted: bool
    reason: str | None = None      # 拒绝时必填（稳定串；进结果与诊断日志）
```

- 默认策略 `DefaultCompletionPolicy` = `02 §5.4` 的"静止后接受最终模型响应"（`accepted=True`
  且不读 `final_text` 的内容——"答案看起来不好"不是完成判据，同 `02 §7` 对 fallback 的禁令）；
- `async`：域策略可能要跑一次真实校验（读 artifact / 跑测试），这个 seam 现在就该容得下它；
- 注入点是 `AgentRuntime(completion_policy=None)`（默认实例）。**不**进配置 / API：域策略属于
  嵌入方，Core 不替部署方决定（`02 §9`：完成规则不得硬编码进 Core）。
- policy 拒绝时 `reason` 是**策略自己的**字符串（原样进结果与日志），quiescence 拒绝时是
  `quiescence_blocked:<kind,...>`（kind 排序后拼接，同输入同输出）。

### D6 — 重入与单终态

闸门**没有缓存**：每次走到完成边界都重新聚合一次（本轮执行内的一次调用点）。所以"未解工作结清后
重入同一完成闸门"是自然结果——恢复后的执行、续聊的下一轮、重试的执行都会各自再算一遍，
静止了才落 `run/completed`。**终态恰好一个**由既有的两个 owner 保证：本路径**不写**终态，
`_RunFinalizer`（单终态旗）+ `Session.end_run` 仍是唯一的终态写入面；崩溃未收口的 run 由启动
扫描补 `run/interrupted`（`03 §5`），也不会与 blocked 收口叠加成两条。

---

## 3. 验证口径

- **确定性用例**（`tests/agent/test_completion_quiescence.py`）：六条谓词各自独立阻断 +
  组合阻断 + 全部静止后默认策略接受且**恰一条** `run/completed` + 自定义拒绝策略（不改
  quiescence 状态：账本与事件流既有的那一段逐字节不变，本次执行只追加自己的 loop 事件）
  + 结清后重入通过 + 单终态。另有取消审批的回归钉（P1：取消不许留下配不上
  `permission/resolved` 的请求，否则会话被永久锁住）。
- **纯函数用例**（同文件）：`collect_quiescence_report` 的每条 kind 与 kind 组合、`reason` 的
  确定性、空真（无账本）分支。
- **既有契约不回退**：`tests/agent/test_event_sequence_golden.py`（冻结的顺序与 append 次数）、
  finalizer / 取消 / 异常臂用例、`tests/recovery/test_reconcile.py`。
- **Live Gate**（`scripts/live_gate.py` 的 `completion-quiescence-gate` v1）：真模型 + 生产工具
  + 生产账本，**同一会话三条腿**——
  1. 静止 ⇒ 收口（并断言 `run/completed` 之前该次调用的 `tool/result` 已 durable、账本行已终态）；
  2. 真实执行域产出一条未结清 owner（生产 `BashTool` 跑一个先落副作用、再睡过工具超时的脚本
     ⇒ `UNKNOWN` + 未证标记；脚本**追加**写哨兵 ⇒ "副作用只发生一次"是可数的，不是形状判据），
     该腿在事件流里**零痕迹**（无任何事件带它的 call id），且整场景不产生任何
     `tool/output_delta`（两条各自成断言、逐条核，不靠注释自证），
     同会话下一次真实执行被拒收口，且 owner 行逐字段不变；
  3. 按 classify（`UNKNOWN`→`NEED_RECONCILE`）→ adjudicate（→`SUCCEEDED`，裁决内容覆盖未证
     标记）两步结清 ⇒ 重入通过。
  断言面另外要求**独立重算**（不经过 Runtime 的纯函数对同一份 durable 事实复读）与"摘掉账本
  后仅事件可见的 kinds 为空"（归因干净，不是"账本缺席所以谓词空真"）。3/3 通过才算证据（票面 AC）。

  负控的设计理由：让**真实模型**恰好发一条会超时的命令不是结构保证，所以第 2 条腿的 owner 由
  执行域直接产出（真实执行 + 真超时 + 真副作用落地，且只留账本行不留 `tool/call` 事件 ⇒ 也不会
  伪造出一个活过结清的悬空调用）。离线端到端用例（`tests/live_gate/test_completion_scenario.py`）
  只允许替换**模型客户端**这一个 seam，因此它的运行**永远不是** Live Gate 证据（`seams` 保持为空）。

## 4. 边界与残余（诚实清单）

1. **谓词 3 在当前串联委派下不会独立命中**：`multiagent/tools.py` 把 `agent/delegation-started`
   与 `-finished` 作为同一批 `pending_events` 一起落盘（阻塞串行模型下两者之间没有可观察窗口），
   所以"started 减 finished"在生产链路上恒为空。本谓词守望的是 durable 事实的**形状**：
   委派一旦改成分阶段落盘（子会话独立生命周期 / 异步委派，`#318` 与后续票的责任域）即刻生效。
   本票**不**改委派链路（Scope Lock）。确定性用例用直接落事件的方式驱动它。
2. **blocked 状态在 wire 层没有专属帧**：SSE/WS 消费者看到的是"流结束、最后一条事件不是终态"
   （与 `run/paused` 同样的形状），CLI 打印空结果。状态在 durable 面是准确的（run 未终结），
   但客户端目前只能靠"没有终态"推断。给这个状态一个客户端可见的表示属于客户端票的责任域。
3. **同一会话里上一次执行留下的悬空调用会挡住后续 run 的完成**：这是 `02 §5.4` 的作用域
   结论（D1），不是缺陷——`Session.resume` 的恢复分类或 reconcile 裁决会把它结清；
   本票只保证"未结清就不完成"，不替它选择结清方式。
4. **`PENDING` 行也算 blocker**（D1 表第 4 条）：`07 §6` 说它"能证明尚未启动、可按策略重执行"，
   但那句话的宾语是**重执行**，不是**完成**。一个还没开始的调用同样说明"这次 run 没有把
   它该做的事做完"。
5. **审批类 blocker 的结清面只有一半**（两轴审查 P1 的残余）：闸门是会话级的，而一条配不上
   `permission/resolved` 的 `tool/approval-requested` 会让这段会话此后每次 run 都 blocked。
   两条成因分开处置——
   - **run 被取消 / 异常退出**：已修。交互式 callback 在非正常退出时按 fail-closed 补一条
     `permission/resolved(deny)` 并清掉 pending（`session/approval.py`），
     `test_cancelled_approval_does_not_wedge_the_session` 钉住"取消后闸门不再有 phantom blocker"。
   - **进程重启**：**仍未修**。审批队列是纯内存的，重启后没有任何写入方能让那条陈旧请求
     变成 resolved（`/approve` 对不存在的 id 404，后端行为符合冻结契约）。⇒ 需要恢复层
     （启动扫描，`03 §5` 里 `run/interrupted` 的同一责任域）决定"陈旧请求按 fail-closed 落决议"
     还是别的语义词。**不在本票范围**：改它要动恢复契约，属恢复票。
   - **结清写入本身失败**（存储故障，`Session.append` 抛错）：上面那条 fail-closed 补齐在
     取消 / 退出的栈上，所以它**吞掉**自己的写入异常并记 ERROR，只为不顶掉栈上的原异常
     （换掉它会让 run 被记成 `failed` 而不是 `cancelled`，`02 §17`）；
     代价是这条请求的配对仍然缺失——与重启同病，同样等恢复票。
     `test_cancel_survives_a_failing_resolution_write` 钉住"上抛的仍是取消 + 留 ERROR 记录"。
6. **委派树里 blocked 子 run 映射成 `failed`**：`multiagent/provider.py` 只区分
   `completed` / 其余 → 父看到的是子代理失败，而不是"子会话有未结清工作"。语义上没说错
   （子确实没完成），但它改变了域流程的父可见行为，而委派侧的正式语义归 `#318`。
7. **blocked run 在同一进程里不可续跑**：续跑的前提是 `run/paused`（`session/service.py`
   的 409 判据），而本票刻意不落暂停 ⇒ 同 run 只能等重启扫描补 `run/interrupted`（逻辑上
   仍在途的 run 被标成崩溃恢复态，是"不落暂停"的代价）。客户端目前只能靠"没有终态"推断。
8. **deadline 过期与闸门同轮相遇时，闸门赢**：deadline 除循环顶判定外还卡工具准入
   （`tooling/executor.py` 的阶段 2.35：到点的调用**不被接纳**）与 closeout 容量
   （`runtime.py` 的 `closeout_capacity`），但"这一轮要不要收口"只在循环里的闸门那一步判。若模型
   恰好在这一轮不再请求工具且账本有欠账，收口是 `quiescence_blocked` 而不是
   `run/paused(deadline)` + `operation/reconcile-required`。两条路都 fail-closed，但**优先级
   没有被任何规格写明**；先按"闸门在完成边界上、deadline 在循环顶上"实现，写在这里备查
   （若将来要反过来，改的是两个判据的先后，不是闸门本身）。
