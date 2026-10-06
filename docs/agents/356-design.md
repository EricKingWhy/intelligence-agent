# W-12 · #356 选项 B：明确退出信号 → 安全暂停（设计稿）

> **阶段**：设计定稿（第二阶段 TDD 施工前的权威设计）。**范围**：只落设计文档；本阶段不写产品代码、不改规格文件。
> **选项**：选项 B（折中，见 `docs/agents/356-research.md` §4.4）——DSH 式软/硬信号拆分：**明确退出信号** → 按 `02 §5.2.1` 走 `run/paused(client_absent)` 收口；**无信号的断线** → 继续跑。不建服务端按 Task 在场登记表。
> **上游依据**：`goal/.../docs/spec/02_AGENT_RUNTIME.md §5.2.1`、`03 §3.4/§5`、`11 §6.2`、ADR-0046；W-22（#366）已合入的 `client_absent` 运行时链（`agent/client_presence.py`、`agent/runtime.py` 准入点与 `_terminal_paused`、`session/runmanager.py` 的 `presence_managed` 接缝）；W-05（#349）`session/progress.py`；W-10 预埋的 `workspace/lease.py::TaskPresenceReader`。
> **本阶段唯一落盘文件**：`docs/agents/356-design.md`。写盘纪律按 `SDD_WORKFLOW_PROTOCOL.md §8.9`。

---

## 0. 12 条 decision → 章节映射（施工权威约束）

下表是本文档的权威索引：每一条 decision 都必须在对应章节逐字落地，施工与审查据此逐条核对。

| # | decision（一句话） | 落点章节 |
| --- | --- | --- |
| 1 | 明确退出信号是**权威硬信号**，只走 `run/paused(reason=client_absent)`，**永不** `run/failed(reason=orphaned)` | §1、§2、§5 |
| 2 | `signal_client_exit` 定为 `async def`（quit-inspection 读账本/队列是协程） | §2 |
| 3 | 三步顺序**不可换**：只读 quit-inspection → 严格 W-05 写 → 立即 `mark_absent`（不等宽限） | §2、§3、§4 |
| 4 | W-05 严格写失败 **fail-closed**：抛 `ClientExitError`，不置缺席、不暂停；**无 cwd 锚视为 N/A 而非失败** | §4 |
| 5 | 退出路径**不注入 `task.cancel()`**：在途 Tool 跑到自然稳定边界，复用既有 `_terminal_paused`/`_raise_reconcile_required` 收口 | §2、§5 |
| 6 | quit-inspection 任读失败 → `uncertain=True` **偏 busy**，但仍执行暂停（信号权威），如实记录不谎报 | §3 |
| 7 | 幂等：已终态/已 paused/已置缺席 → `ignored_already_settled`，绝不双写；未登记 → `ignored_not_managed` 旧语义逐字不变 | §2、§5 |
| 8 | `_reap_if_orphaned` 的 `presence_managed` 分支改为宽限到期**只记日志、继续跑** | §5 |
| 9 | 由此产生对 `02 §5.2.1`「意外断线超宽限须停止接纳」的**规格漂移**，单独声明、**不改规格文件**（修订需用户另批） | §5 |
| 10 | 「durably paused ⇒ 无需服务」是**产品语义决策**，标待用户确认，本阶段只定义、不实现任何关停钩子 | §6 |
| 11 | 新增面**仅三处**（`signal_client_exit`/类型、`inspect_exit_impact`、`_reap_if_orphaned` 分支），其余全部复用既有 W-22 运行时链 | §1、§7 |
| 12 | 反面清单**十条**单列 | §7 |

---

## 1. 目标与非目标

### 1.1 目标

1. **把"客户端明确退出"变成一个权威、可观测、幂等的服务端信号**：客户端主动声明"我要走了" → 服务端在该 Task 的安全边界收口为 `run/paused(reason=client_absent)`，保留同一 `run_id`、预算消耗与 continuation，绝不走旧失败路径。
2. **把"无信号的意外断线"与"明确退出"彻底分开**：没有退出信号时，仅凭"零订阅者"**不得**断定客户端已离开 —— 继续跑，由持久化 + 重连 replay + 显式 resume 自愈。
3. **durability 优先**：暂停前必须先把 W-05 进度写到 durable（严格写），写不成功就不暂停（fail-closed）。
4. **幂等与旧语义隔离**：重复信号不双写；未纳入在场协议的旧 run 行为逐字不变。

### 1.2 非目标（本阶段明确不做）

- **不建**服务端按 Task 的在场登记表（`client_id + task_session_id + presence_kind + last_seen`）；选项 B 明确不做 presence registry（research §4.4 选项 B、§4.2）。
- **不实现**任何服务关停/进程退出钩子（decision 10；§6 只定义语义，标待用户确认）。
- **不改** `02 §5.2.1` 或任何规格文件（decision 9；漂移只声明，修订需用户另批）。
- **不碰** #305 的通用 pause 状态机、#341 relay cleanup、预算 / stuck / CAS 的二次实现（W-12 票面「不做」）。
- **不引入**新依赖；**不新建**第二条 Tool / 暂停执行路径（不变量 #7、#10）。
- **不做**客户端心跳 / 托盘 UI / 前端登记协议（属 W-12 后续前端面与 W-15）。

### 1.3 新增面只有三处（decision 11）

| 面 | 位置（设计归属） | 性质 |
| --- | --- | --- |
| `RunManager.signal_client_exit(...)`（`async def`）+ 新类型 `ClientExitOutcome` / `ClientExitError` | `session/runmanager.py` | 唯一写侧入口 |
| `RunManager.inspect_exit_impact(...)`（`async def`，只读） | `session/runmanager.py` | 唯一读侧查询 |
| `ManagedRun._reap_if_orphaned` 的 `presence_managed` 分支改写 | `session/runmanager.py` | 行为改写（不新增方法） |

除这三处外，**全部复用既有 W-22 运行时链**：`ClientPresenceGate.mark_absent()`、`AgentRuntime` 循环顶的 client-presence 准入点（`runtime.py:1736`）、`_terminal_paused`（`runtime.py:2920`）、`_raise_reconcile_required`（`runtime.py:3124`）、`_closeout_continuation` 的 `REASON_CLIENT_ABSENT` 确定性分支（`runtime.py:3222`）、`write_progress_file` / `ProgressWriteOutcome`（`session/progress.py:658`）、`TaskPresenceReader`（`workspace/lease.py:51`）、RunManager 终态与接力投递管线。

---

## 2. 明确退出信号 contract

### 2.1 语义总则（decision 1、2、3、5、7）

**明确退出信号是一份权威硬信号**：客户端/宿主进程在**主动**退出前，把"这个 Task 的最后一个托管客户端正在离开"这件事显式交给服务端。信令一旦被接受：

- 结局**只能是** `run/paused(reason=client_absent, trigger_dimension=client_presence, closeout_source=deterministic)` 这一条非终态暂停；
- **永不**落在 `run/failed(reason=orphaned)` 上（那是"零订阅者宽限到期"的旧孤儿路径，选项 B 不再把它当作客户端离开的证据）。

**`signal_client_exit` 是 `async def`**（decision 2）：因为它内部的 quit-inspection 需要 `await` 读 Operation Ledger（`ledger.list_for_session` 是协程）、读队列/在途状态（同样是协程）。**禁止**把它实现成同步方法再在内部用 `asyncio.run` / 线程绕行——那会与 Runtime 的单事件循环前提冲突（`client_presence.py` 明示"单事件循环内使用"）。

**三步顺序不可换**（decision 3）：

```
① await inspect_exit_impact(session_id)      # 只读 quit-inspection，副作用为零
② await <strict W-05 write>(session_id)      # 严格写；失败即抛 ClientExitError（§4）
③ runtime.client_presence.mark_absent()      # 立即置缺席——不等 30s 宽限
```

顺序的三条硬理由：

- ① 在 ② 之前：先知道"会打断什么"，才能把 impact 如实写进 outcome；只读查询**绝不**产生写副作用。
- ② 在 ③ 之前：③ 一置位，runtime 会在下一个循环顶收口暂停 —— 若此刻 durable 进度还没落，暂停就发生在"进度文件不新"的窗口里，违反 W-05「写失败不得把旧文件说成最新」与本次 durability 目标。
- ③ 立即、**不等宽限**：明确退出是权威信号，宽限是给"意外断线"用的重连窗口，对一个已经主动告别的客户端再等 30 秒毫无意义，还会延长"暂停前仍在跑"的窗口。③ 之后由既有运行时链在稳定边界收口（decision 5，见 §2.4）。

### 2.2 `signal_client_exit` 签名与返回

```python
async def signal_client_exit(
    self, session_id: str, *, client_id: str,
) -> ClientExitOutcome:
    """客户端明确退出信号的服务端唯一入口（选项 B 写侧）。

    权威硬信号：接受即承诺只走 run/paused(reason=client_absent)，
    永不 run/failed(orphaned)（decision 1）。
    三步顺序固定：只读 inspect → 严格 W-05 写 → 立即 mark_absent
    （decision 3）；禁止在任何一步调 task.cancel()（decision 5）。

    幂等（decision 7）：
      未登记 run        → ignored_not_managed，旧语义逐字不变
      已终态/已 paused/已缺席 → ignored_already_settled，绝不双写
      正常              → paused，恰好一条 run/paused

    fail-closed（decision 4）：严格 W-05 写失败抛 ClientExitError，
    调用方拿到明确错误，run 不被置缺席、不被暂停。
    """
```

> 入参 `client_id` 仅用于**诊断与日志归因**（哪个客户端发的信号），不构成在场登记——选项 B 不建登记表（§1.2）。多客户端同 Task 的"最后客户端"判定属未来的前端/登记协议，本阶段不在服务端建表；本信号按"调用方已判定这是最后一个托管该 Task 的客户端"来处理。

### 2.3 类型形状与字段（`ExitImpact` / `ClientExitOutcome` / `ClientExitError`）

```python
@dataclass(frozen=True)
class ExitImpact:
    """quit-inspection 的只读结果（decision 2/3/5/6）。

    每一维回答"退出会打断什么"。任读失败 ⇒ uncertain=True 偏 busy，
    但不得据此改变暂停决策（decision 6，见 §3.3）。
    """

    session_id: str
    has_inflight_tool: bool          # 有在途工具（Ledger RUNNING / 执行域在跑）
    has_inflight_child: bool         # 有活动或待恢复的子 Agent（不变量 #18/#19）
    has_pending_operation: bool      # Ledger 有未结清的行（非 settled）
    needs_reconcile: bool            # 含 RUNNING（复用 needs_reconcile() 的未证即待对账语义）
    has_queued_input: bool           # 会话队列里有未投递输入
    uncertain: bool                  # 任一维读取失败 ⇒ 偏 busy
    detail: tuple[str, ...]          # 逐维如实记录（含失败原因），不谎报

    @property
    def busy(self) -> bool:
        # uncertain 偏 busy：判不准一律按"有活"处理（DSH fail-safe，
        # research §2.4 / §4.3 #4）。busy 只用于如实报告与未来关停钩子判断，
        # MUST NOT 用来跳过暂停（decision 6）。
        return (
            self.uncertain
            or self.has_inflight_tool
            or self.has_inflight_child
            or self.has_pending_operation
            or self.needs_reconcile
            or self.has_queued_input
        )
```

```python
# ClientExitOutcome.status 的三个取值（穷尽，无第四态）
CLIENT_EXIT_PAUSED = "paused"
CLIENT_EXIT_IGNORED_ALREADY_SETTLED = "ignored_already_settled"
CLIENT_EXIT_IGNORED_NOT_MANAGED = "ignored_not_managed"


@dataclass(frozen=True)
class ClientExitOutcome:
    """信号处理结果（decision 1/3/4/6/7）。"""

    session_id: str
    status: str                          # 上表三值之一
    run_id: str | None                   # 收口的 run_id（同 run，不新开）
    uncertain: bool                      # 是否带读不确定（decision 6）
    impact: ExitImpact | None            # quit-inspection 结果（未跑时为 None）
    progress: ProgressWriteOutcome | None  # W-05 严格写结果（N/A 时为 None）
    paused_event_seq: int | None         # 那条 run/paused 的 seq——**None-by-design**，见下方注
    detail: str                          # 人类可读收口说明（含 N/A / 幂等原因）
```

> **`paused_event_seq` 的 None-by-design（A-P2-2）**：信号路径**不等待**暂停落盘
> （decision 3：置缺席后由既有运行时链在稳定边界收口），故正常路径下 `paused_event_seq`
> **恒为 `None`**——这不是缺陷。design 依赖的"恰好一条 `run/paused`"由两件事保证：
> ① 闸门 `client_presence` 单向（`mark_absent` 只置真、不反转）；② `_terminal_paused`
> 的**单次**收口语义（同一执行段只落一条暂停）。§8 T5 的相应验收点据此改口径。

```python
class ClientExitError(Exception):
    """严格 W-05 写失败时的 fail-closed 中止（decision 4）。

    抛出即代表：信号路径**中止**，run 维持原状继续跑。
    抛出后 MUST NOT 置缺席、MUST NOT 暂停（否则就是在进度未 durable 时
    假装安全收口）。无 cwd 锚不抛本错——那是 N/A，不是失败（decision 4）。
    """

    def __init__(
        self, *, session_id: str, run_id: str | None,
        progress: ProgressWriteOutcome, detail: str,
    ) -> None:
        super().__init__(detail)
        self.session_id = session_id
        self.run_id = run_id
        self.progress = progress   # ok=False 的写结果，含 error_kind/reason
        self.detail = detail
```

### 2.4 接受信号后发生什么（decision 5：不取消，复用既有链）

`mark_absent()` 只置位 `ClientPresenceGate`，**不**对 run task 调 `task.cancel()`。收口完全交给既有 W-22 链：

1. `mark_absent()` 使 `runtime.client_presence.absent` 变真；
2. 在途 Tool **跑到自然稳定边界**（当前工具调用按其既有 Ledger 收口，不盲中止、不重复注入取消）；
3. runtime 在下一个循环顶准入点（`runtime.py:1736`）看到 `absent` → 走 `_terminal_paused(trigger_dimension=TRIGGER_CLIENT_PRESENCE)`；
4. `_terminal_paused` 先过 `_raise_reconcile_required`（`runtime.py:3124`）：有未证副作用先升 `NEED_RECONCILE`（`03 §5`：对账优先于恢复）；
5. `_closeout_continuation` 命中 `reason == REASON_CLIENT_ABSENT`（`runtime.py:3222`）→ **确定性** continuation，**不发**任何"礼貌性"模型请求；
6. 落**恰好一条** `run/paused(reason=client_absent)`（非终态）。

**为什么绝不 `task.cancel()`**：取消会把 `CancelledError` 注进在途 Tool，盲中止副作用（可能产生 UNKNOWN 且无从 reconcile）；同时取消臂会把 reason 翻成 `orphaned`，正是 decision 1 要禁止的失败路径。这一点与 W-22 `_reap_if_orphaned` 现有注释（`runmanager.py:225-241`）同向。

### 2.5 幂等与旧语义隔离（decision 7）

| 进入时状态 | `status` | 行为 |
| --- | --- | --- |
| `presence_managed is False`（未登记） | `ignored_not_managed` | **不改**任何闸门、**不**暂停；旧 `run/failed(reason=orphaned)` 孤儿语义逐字不变（`11 §6.2`） |
| run 已终态（`run/completed` / `run/failed` 已落，`run.terminal`） | `ignored_already_settled` | 不双写、不置缺席 |
| run 已 `paused`（`run.paused` 已置） | `ignored_already_settled` | 不双写（"已 paused 时又收到离开事件"不得双写，W-22 明文） |
| runtime 闸门已 `absent`（重复信号） | `ignored_already_settled` | 不双写、不重复暂停 |
| 正常在途且已登记 | `paused` | 走上表 §2.4 全链 |

判据只读**持久化事实**（terminal 旗标 / paused 旗标 / gate.absent），不靠连接层推断（Codex #50118/#43182 的反例：run 终态必须由持久化状态机决定）。

---

## 3. quit-inspection 查询与 busy 偏置

### 3.1 定位

`inspect_exit_impact` 是**只读**查询（decision 3 第①步），对应 DSH 的 `quit-inspection` IPC（exit 前问 Host"会打断什么"，research §2.4）。它**只读、无副作用**，不得 append 任何事件、不得改 Ledger、不得改闸门。

```python
async def inspect_exit_impact(self, session_id: str) -> ExitImpact:
    """只读 quit-inspection（decision 2/3/6）。绝不写盘、绝不再现副作用。"""
```

### 3.2 读取维度与数据源（全部复用既有只读面）

| 维度 | 数据源（复用，不新增账本） |
| --- | --- |
| `has_inflight_tool` | `executor.operation_ledger.list_for_session(...)` 中 state 为 `RUNNING` 的行 |
| `has_pending_operation` | 同账本中非 `settled` 的行 |
| `needs_reconcile` | 同账本中 `UNKNOWN` / `NEED_RECONCILE` 的行（复用 `needs_reconcile(operation)`） |
| `has_inflight_child` | 既有子 Agent / 委派状态（复用 runtime 既有多 Agent 只读面） |
| `has_queued_input` | 会话队列只读面（复用既有 queue 读取） |
| `has_present_client` | 可选复用 `workspace/lease.py::TaskPresenceReader.has_present_client`（W-10 预埋只读 seam；**只读**，不反向登记，避免 W-10 ↔ W-12 依赖环） |

**账本缺席**（未注入 OperationLedger / 纯对话 session 无账本行）：按"无该维度信息"处理，不伪造行；此时该维为 `False`，若连账本句柄都没有则不因此置 `uncertain`（缺信息 ≠ 读失败，与 `_raise_reconcile_required` 的"没有账本就不伪造对账要求"同向）。

### 3.3 读失败 → `uncertain=True` 偏 busy，但仍暂停（decision 6）

**规则**：`inspect_exit_impact` 内**任一维读取抛异常**，都不得让查询失败、更不得让整个信号失败：

- 该维按**偏 busy** 处理（视为"可能有活"）；
- 置 `uncertain = True`；
- 把失败原因**如实**追加进 `detail`（例：`"operation_ledger 读取失败：<exc>"`）；
- 查询照常返回。

**关键边界**：`uncertain=True` **不改变暂停决策**——因为退出信号是权威硬信号（decision 1）。busy/uncertain 只用于：① 在 `ClientExitOutcome` 里如实报告"这次收口带着读不确定"；② 为 decision 10 的未来关停钩子提供"是否真的空闲"的事实。**MUST NOT** 因为 `uncertain` 就跳过暂停，也 **MUST NOT** 把 `uncertain=True` 的收口记录成"干净、确定"。

> 与 research §4.3 #4「要区分时不确定必须偏向继续跑」的关系：那条针对的是**无信号断线**的判定方向（选项 B 下无信号一律继续跑，见 §5）。对**明确退出信号**，方向相反且确定：信号权威 ⇒ 仍要暂停；`uncertain` 只影响"如实程度"，不影响"要不要暂停"。

---

## 4. W-05 严格写与 fail-closed

### 4.1 为什么是"严格写"而非 best-effort

W-05 契约要求「写失败在任务状态可见，不得把旧文件说成最新」。暂停是一个**产品可见的状态转移**：若在进度文件陈旧/写失败时暂停，就等于向用户宣告"任务在安全边界冻结了"，而冻结点的 durable 进度却不新——这是谎报。因此退出路径上的 W-05 写是**严格**的：成功才继续，失败即中止（decision 4）。

> 对比：常规触发点（创建 / 交付 / cancel / run 终态）的 W-05 仍是 best-effort（`write_progress_file` 的既有契约）。严格只加在**退出信号**这一条路径上，不改变其它调用方。

### 4.2 无 cwd 锚 ⇒ N/A，不是失败（decision 4）

W-05 进度文件写在本 session 的**项目根锚**（`<项目根>/agent-progress/<session-id>/progress.md`，见 W-05 票面；`progress_paths(root, session_id)`）。

- 若本 session **没有 cwd / 项目根锚**（例如纯对话 session、未绑定项目目录的运行）：**没有可写位置**，判为 **N/A（不适用）**——跳过写、**不**失败、**不**抛错，照常进入 `mark_absent`。
- 若本 session **有 cwd 锚，但该 cwd 目录已被外部删除**（A-P3-1，行为不变，补声明）：同样**没有可写位置**，判为 **N/A（不适用）**——跳过写、不失败、不抛错，照常进入 `mark_absent`（沿用 `service.refresh_progress_file` 的同一守卫，**不复活已删目录**）。
- 记录：`ClientExitOutcome.progress = None`，`detail` 分别注明 `"无 cwd 锚，进度写 N/A"` 或 `"cwd 目录不存在，进度写 N/A"`。

**MUST NOT** 把"无锚点"当成"写失败"：那会把一个正常可暂停的 run 错误地 fail-closed 掉。N/A 与失败是两种事实，必须分开记录。

### 4.3 严格写判定

对**有锚点**的 session，退出路径调用既有 `write_progress_file(root, session_id, events)`：

| 结果 | 判定 |
| --- | --- |
| `ok=True, skipped=False` | 成功（新写或更新） |
| `ok=True, skipped=True` | 成功（幂等跳过：磁盘正文已是当前投影，durable 已最新） |
| `ok=False`（任意 `error_kind`：`locked` / `env` / `external_edit` …） | **失败** → `raise ClientExitError(...)` |

`error_kind` 明细直接来自 `ProgressWriteOutcome`（`progress.py:536`），原样进 `ClientExitError.progress`，不吞、不降级。

### 4.4 fail-closed 的边界（decision 4）

严格写失败时：

- **抛 `ClientExitError`**；
- **不**调 `mark_absent()`；
- **不**暂停、**不**落任何 `run/paused`；
- run **维持原状继续跑**（就像这次信号没被接受一样）；
- 调用方拿到明确错误，可向用户展示"进度写失败，暂停未执行"。**MUST NOT** 静默吞掉或"先暂停再补写"。

严格写的成功/失败是 decision 4 与 decision 6 的分水岭：**只读查询失败仍暂停（信号权威）**，**durable 写失败不暂停（fail-closed）**。

---

## 5. 无信号断线：继续跑

### 5.1 行为改写：宽限到期只记日志、继续跑（decision 8）

`ManagedRun._reap_if_orphaned`（`runmanager.py:216`）的 `presence_managed` 分支**从**"宽限到期 → `mark_absent()` 交准入点暂停"**改为**"宽限到期 → **只记日志、继续跑**"：

- 零订阅者超过 `disconnect_grace_seconds` 时，**不再** `mark_absent()`；
- 只落一条日志（如实：客户端已断线，但无退出信号，run 继续）；
- run 照常推进，直到自然完成、显式 cancel、或后续收到**明确退出信号**。

**理由**：选项 B 下，只有"明确退出信号"是权威的离开证据（research §4.4）。一次网络抖动、一次前端重连中的短暂断流，与"用户关掉了最后一个客户端"在服务端看来无法区分；把宽限到期当离开证据会误暂停仍在被观察的 run。DSH 的"关窗=隐藏、任务继续"与 Codespaces 的"用户活动重置 idle"都支持"没有明确离开就不停"（research §2.4；W-22 §方案依据）。

### 5.2 触发口收敛（decision 5、8）

W-22 已经落地的准入闸门、`_terminal_paused`、`_raise_reconcile_required` **全部保留**，但它们的 `client_absent` 触发口**收敛为唯一一处**：`signal_client_exit` 里的 `mark_absent()`。宽限计时器不再是触发口。这满足不变量 #7（Tool 只一条统一路径）与"暂停只有一个决定点"。

### 5.3 规格漂移声明（decision 9）

**漂移事实**：`02 §5.2.1` 现行正文（`02_AGENT_RUNTIME.md:115`）写：

> 「最后一个托管该 Task 的客户端明确退出，**或意外断线经过有界重连宽限时**，Run MUST 停止接纳新的 Model/Tool/Child 工作……」

其中"**或意外断线经过有界重连宽限时** ⇒ 停止接纳"这一条，是 W-22 按原票面落地的语义。**选项 B 有意不实现它**：无信号断线在宽限到期后**继续跑**，停止接纳只由明确退出信号触发。

**第二处漂移事实（A-P2-1）**：`11 §6.2` 现行正文（`11_STREAMING_API_WEB_UI.md:169`）同样把"无信号断线"当成停收触发口：

> 「最后一个托管客户端明确退出，**或意外断线超过有界宽限时**，服务先阻止该 Task 新的 Model/Tool/Child 接纳，再按 `02 §5.2.1` 与 `03 §3.4/§5` 持久化 `client_absent` 暂停或进入 NEED_RECONCILE。**宽限期间不发起新的模型步骤**。」

按同一 decision 9，`11 §6.2` 的"意外断线超过有界宽限 ⇒ 停收 / 宽限期间不发新模型步骤"在选项 B 下**不生效**；其余（明确退出停收、`client_return` 显式恢复、重连不自恢复）照常生效。`11 §6.2` 与 `02 §5.2.1` 是**同一处**漂移的两个正文落点，一并登记、一并待用户批准修订。

- 这是一处**真实的规格漂移**，不是实现惰性；
- **本阶段不改** `02 §5.2.1`（也不改 `03` / `11` / ADR-0046 的任何正文）——规格是冻结的，修订需**用户另行批准**；
- 漂移的具体形态：`02 §5.2.1` 的"意外断线+宽限 ⇒ 停收"在选项 B 下**不生效**；"明确退出 ⇒ 停收"**照常生效**；
- **风险登记**：在规格修订获批前，`02 §5.2.1` 正文与实现行为不一致。审查者/后续读者应以本文档 §5.1 为实现事实、以 §5.3 为漂移指针，并知道规格正文尚未同步。
- **未决**：是否要用户批准把 `02 §5.2.1` 收窄为"仅明确退出触发停收"（或新增一条选项 B 的说明）——**待用户决定**，本设计不预设结论。

### 5.4 与孤儿回收旧语义的关系

未登记（`presence_managed=False`）的 run 的孤儿回收**完全不变**：宽限到期仍 `task.cancel()` → `run/failed(reason=orphaned)`（`runmanager.py:253-260`）。decision 8 只改 `presence_managed=True` 分支，旧路径逐字不动（不变量 #22、`11 §6.2`）。

---

## 6. 「无需服务 Task」定义（待用户确认）

> 本节回答 research §5 第 2 条与 §7 第 3 条的悬置问题：**"无需服务 Task"是否含 durably paused？** 这是**产品语义决策**，本阶段**只定义、不实现**任何关停钩子（decision 10）。以下定义**待用户确认**。

### 6.1 定义

**「无需服务 Task」**：一个 Task 处于**它在没有活的服务器进程时也不丢失可恢复性**的状态。

本次设计的判定：**durably paused ⇒ 无需服务**。具体成立条件（缺一不可）：

1. **已落恰好一条** `run/paused`（此处即 `reason=client_absent`）；
2. 该暂停的 **closeout 是确定性的**（`closeout_source=deterministic`，没有 pending 的模型收尾请求）；
3. 恢复所需的一切都已在 durable 介质上：append-only `SessionEvent` 流、Operation Ledger、Checkpoint、W-05 `progress.md`、continuation（同 `run_id`、预算快照、已完成/未完成项）。
4. **不属于**恢复前置未满足的暂停：若暂停时存在 `NEED_RECONCILE`，恢复需要用户显式对账——但该状态**同样是 durable 的**，因此**仍无需服务**（只是"需要人"，不需要"活的进程"）。

**反例（不构成"无需服务"）**：严格 W-05 写失败的 run（此时根本没暂停）、仍在跑（未暂停）的 run、暂停事件尚未落盘的中间态。

### 6.2 推理：暂停是非终态，但全部状态已 durable

- **暂停是非终态**：逻辑 run 未终结，`run/resumed` 可以把它唤回（新执行段、同 `run_id`）。因此"暂停"不等于"结束"。
- **但可恢复性不依赖活着的内存**：`run/paused` 是 **durable fact**；恢复走的是**新执行段**（新 Runtime / 新闸门，`client_presence.py` 语义），它从 durable 事实确定性重建（不变量 #3 append-only SessionEvent、#5 Persistent History ≠ Runtime Context、#13 Ledger 支持 reconcile）。
- **服务退出不丢失可恢复性**：进程退出即释放内存中的 Runtime / 闸门 / 订阅者；这些在暂停后**本就没有恢复语义**（重连不自动续跑，必须显式 resume）。所以"服务退出"对 durably paused 的 Task 只丢失"一个已经不需要的活动进程"。
- **结论**：durably paused 的 Task 不**需要**活的服务进程；它可以安全地随最后一个客户端退出而**不再被服务**。

### 6.3 本阶段边界（decision 10）

- 本节**只定义语义**；
- **不实现**任何服务关停钩子、不实现"无客户端则退出进程"的编排、不改 `aclose()` / Web 生命周期；
- 是否据此实现关停、何时实现、由谁触发——**待用户确认后另开**，且属于新的授权范围（`AGENTS.md §9.1.1`：新证据/新范围需用户决策）。

---

## 7. 反面清单（decision 12：十条，单列）

以下十条是**无论实现细节如何都禁止**的行为，审查据此逐条判 P0 违规。

1. **禁止**在退出信号路径上落 `run/failed(reason=orphaned)`（或任何失败终态）——明确退出是权威硬信号，结局只能是 `run/paused(reason=client_absent)`（decision 1）。
2. **禁止**在退出路径调用 `task.cancel()` 或注入任何取消——在途 Tool 必须跑到自然稳定边界（decision 5）。
3. **禁止**把 `mark_absent()` 放在严格 W-05 写**之前**——三步顺序（只读查询 → 严格写 → 置缺席）不可换（decision 3）。
4. **禁止**在严格 W-05 写失败时继续暂停 / 置缺席——必须抛 `ClientExitError` 并让 run 继续跑（fail-closed，decision 4）。
5. **禁止**把"无 cwd 锚"当作写失败——那是 N/A，不是失败，不得因此中止信号（decision 4）。
6. **禁止**重复写 `run/paused` / 重复置缺席——已终态/已 paused/已缺席一律 `ignored_already_settled`，绝不双写（decision 7）。
7. **禁止**改变未登记（`presence_managed=False`）run 的旧语义——`ignored_not_managed` 下不置闸门、不暂停，旧 `orphaned` 路径逐字不变（decision 7）。
8. **禁止**为"客户端明确退出"等待 30 秒宽限——置缺席必须立即，宽限只服务于无信号断线（decision 3）。
9. **禁止**保留 `_reap_if_orphaned` 在 `presence_managed` 分支上的"宽限到期 `mark_absent`"旧行为——宽限到期只记日志、继续跑（decision 8）。
10. **禁止**在本阶段修改任何规格文件（`02` / `03` / `11` / ADR-0046）来"对齐"漂移，也**禁止**实现任何服务关停钩子——前者漂移只声明、修订需用户另批；后者只定义、待用户确认（decision 9、10）。

> 附加互斥项（不占十条配额）：**禁止**把 `inspect_exit_impact` 实现成会写盘 / 会推进序号 / 会改 Ledger 的操作——它必须是纯只读查询（decision 3 第①步）。

---

## 8. 测试计划（TDD：红 → 绿）

**方法**：每一例先写**会失败**的测试（红），确认它因"新面未实现"或"旧行为存在"而失败，再写最小实现使其转绿（`AGENTS.md §9.4`；SDD §9 TDD）。逐票落 commit，先红后绿可自证（`principle-sequence-verifiable-units`）。

**测试分层**：单测（RunManager / fake runtime）/ 集成（FakeModel 准入计数 / 真实进度目录）/ 端到端（真实子进程 kill / reconnect）。行号指现状实现，供施工者定位。

| # | 用例 | 红（先失败） | 绿（实现后通过） | 依据 |
| --- | --- | --- | --- | --- |
| **T1** | 未登记 run：`signal_client_exit` → `ignored_not_managed` | 方法不存在（AttributeError）/ 或误改旧行为 | 返回 `ignored_not_managed`；`gate.managed is False`、`gate.absent is False`；无 `run/paused`；断线后仍走旧 `run/failed(orphaned)` | decision 7；`11 §6.2` |
| **T2** | 已终态（completed/failed 已落）：→ `ignored_already_settled`，不双写 | 新方法未实现 | 返回 `ignored_already_settled`；事件流中 `run/paused` 数不变（0） | decision 7 |
| **T3** | 已 paused：→ `ignored_already_settled`，不双写 paused | 未实现 | 返回 `ignored_already_settled`；`run/paused` 恰好仍 1 条（不新增） | decision 7；W-22「已 paused 再收离开事件不双写」 |
| **T4** | 已置缺席（重复信号）：→ `ignored_already_settled` | 未实现 | 第二次调用不重复置位、不新增事件 | decision 7 |
| **T5** | 正常信号：三步顺序 → 恰好一条 `run/paused(reason=client_absent)` | 未实现 | `status=paused`；`run/paused` 恰好 1 条且 `reason=client_absent`、`trigger_dimension=client_presence`、`closeout_source=deterministic`；同 `run_id`；`paused_event_seq` 设计已收敛为 `None`（信号不等暂停落盘，见 §2.3 注 A-P2-2） | decision 1、3、5；`02 §5.2.1` |
| **T6** | 宽限到期（无信号）：presence_managed run **继续跑**，零 `paused`、零 `failed(orphaned)` | 旧 `_reap_if_orphaned` 会在到期 `mark_absent` → 出现 paused（红） | 到期后 run 未暂停、未失败；`gate.absent is False`；有"继续跑"日志 | decision 8 |
| **T7** | 在途 Tool 不被取消：signal 后 `task.cancel` 未被调用，工具跑到稳定边界 | 若实现误用 cancel 会中断工具（红） | `task.cancel` 调用数为 0；工具按其 Ledger 收口；产出恰好 1 条 paused | decision 5 |
| **T8** | 严格 W-05 写失败（只读目录 / 锁被占 / 外部编辑）→ 抛 `ClientExitError` | 未实现 fail-closed | 抛 `ClientExitError`；`gate.absent is False`；无 `run/paused`；run 继续跑 | decision 4；W-05 |
| **T9** | 无 cwd 锚：W-05 判 **N/A**，不失败、照常暂停 | 若把无锚当失败会抛错（红） | `status=paused`；`progress is None`；`detail` 含 `"N/A"`；不抛 `ClientExitError` | decision 4 |
| **T10** | quit-inspection 读失败（账本/队列抛异常）→ `uncertain=True` 偏 busy，**仍暂停**且如实记录 | 若吞错或误中止（红） | `uncertain is True`、`impact.busy is True`；`status=paused`；`detail` 含失败原因；不谎报 clean | decision 6 |
| **T11** | NEED_RECONCILE 优先：信号时存在 UNKNOWN op → 先 `operation/reconcile-required` | 若先落 paused 会漏对账（红） | 事件序：`operation/reconcile-required` 先于 `run/paused`；paused 的 continuation 不暗示可安全续跑（`blocked_by` 非空） | decision 5；`03 §5`；`runtime.py:2991` |
| **T12** | 投影可区分：`paused(client_absent)` 与 `needs_reconcile` 是两种投影 | 若投影混同（红） | `run/paused` 投影 `run_status='paused'`（`projection.ts:874-884`）；带 reconcile 时投影为 `needs_reconcile`（`03 §5`）——两者互斥可辨 | decision 5；`03 §5` |
| **T13** | 幂等重复信号：并发/连续两次 → 一次生效、第二次 `ignored_already_settled` | 未实现 | 合计恰好 1 条 `run/paused`；第二次返回 `ignored_already_settled` | decision 7 |
| **T14** | 停止接纳：信号后新 model/tool/child 接纳数 = 0 | 未实现准入（红） | FakeModel 计数：信号后新增 model 请求 0；新 tool / 新 child 接纳 0 | decision 1；`02 §5.2.1`；W-22 验收「离开后新请求数 0」 |
| **T15** | 真实子进程 kill/reconnect：durable pause 后重连不自恢复、显式 resume 可续 | 无（回归护栏） | kill 后重启：暂停事件与 run_id 一致、不伪造失败；重连仅展示；显式 `resume_basis=client_return` + `expected_version` 后才 `run/resumed` | W-22 既有链；本票「不回归」 |

**红的判定要求**：T1–T14 必须能在**未实现新面前真的失败**（用断言证明是"缺能力"而非"测试写错"）。T15 是回归护栏，红态指"既有行为被新改动破坏时失败"。

**测试不改语义**：不得为了好写而删除 Failure / Recovery 语义（`AGENTS.md §8` 红线）。NEED_RECONCILE 与 kill/resume 必须有真实用例。

---

## 9. 方案依据（成熟产品映射表，SDD §1.3）

> 来源与机制摘要的完整调研见 `docs/agents/356-research.md` §2（已核实，含 License）。本表只做"机制 → 本设计决策 → 判定"的映射。逐行判定遵循 Reuse First（`AGENTS.md §6/§9.5`）。

| 来源（成熟产品/标准） | 机制摘要 | 契合的本设计决策 | 判定 |
| --- | --- | --- | --- |
| DeepSeek Harness `quit-confirmation.ts`（MIT）+ `quit-inspection.ts`（research §2.4） | 硬退出前经私有 IPC 查询"会打断什么"（在跑 agent / 子代理 / 待审批 / 排队消息 / 定时任务）；关窗=隐藏、任务继续 | §3 只读 quit-inspection；§1 区分"软离开(继续跑) vs 硬退出(暂停)"；§2 退出前查 impact | **ADAPT**（借语义，不移植 TS；不引 Node Host 入 Core） |
| DSH quit-confirmation 的"检查失败/超 2s → **按有任务处理**"（research §2.4） | 不确定性 fallback **偏 busy** | decision 6（§3.3）`uncertain=True → busy`；§2.3 `ExitImpact.busy` | **ADAPT** |
| DSH quit-confirmation 的"确认框期间重复退出请求并入**单一待决决定**"（W-22 §方案依据） | 同一离开事件只处理一次，不堆叠 | decision 7（§2.5）幂等 `ignored_already_settled`、不双写 | **ADAPT** |
| Anthropic《Effective harnesses for long-running agents》`claude-progress.txt` + OpenAI Codex 长时程项目文件（W-05 §方案依据） | 把目标/状态/决策外置成 durable 文件，新上下文凭文件+历史恢复 | §4 暂停前**严格** W-05 写；§6 可恢复性依赖 durable 事实 | **PORT DESIGN**（只借"外置 durable 交接"原则） |
| GitHub Codespaces idle-timeout 与"停止后再用需显式动作"（W-22 §方案依据） | 不活动→停；活动重置 idle；停止=可恢复，不等同删除 | §6「durably paused ⇒ 无需服务」；§5 无信号不擅自停、"需要人"而非"需要进程" | **PORT DESIGN**（借生命周期语义） |
| Python `asyncio.shield` / 取消语义（356-research §2.1，PSF） | 收尾只产生一次终态；不把取消注进在途工作 | decision 5（§2.4）不 `task.cancel()`、在途跑到稳定边界 | **REUSE**（语言级原语，无代码移植） |
| Pi durable / terminal-once / append-only record log（356-research §2.2，MIT） | run 自己负责 abort 收尾、只产生一次终态；进程死从 checkpoint 续跑 | decision 1（单一非终态收口）；§6 durable 可恢复 | **ADAPT**（语义参照；**不**采用其"自动续跑"） |
| Cline `abortTask` 的原因分类 `user_cancelled` / `streaming_failed`（356-research §2.5，Apache-2.0） | 区分"用户主动取消"与"流式失败" | §1 明确退出 ≠ 断线失败；decision 1 | **ADAPT** |
| Codex #50118 / #43182 的 run 状态歧义 bug（356-research §2.5） | "run 到底 paused/orphaned/仍在跑"由连接层决定 ⇒ 歧义 | §2.5/§5.2 终态只由持久化状态机决定；NEED_RECONCILE 优先 | **PORT DESIGN**（作为反例的证据） |
| 本仓 W-22 `ClientPresenceGate` + `AgentRuntime` 准入点 + `_terminal_paused` + `_raise_reconcile_required`（`02 §5.2.1`，已合入） | 缺席置位 → 循环顶停收 → 稳定边界 `run/paused`，对账优先 | §2.4 全部收口机制；decision 11 的"复用既有链" | **REUSE** |
| 本仓 W-05 `write_progress_file` / `ProgressWriteOutcome`（`progress.py:658`） | 原子写、同目录锁、external-edit 守卫、失败明确报错 | §4 严格写 + fail-closed | **REUSE** |
| 本仓 W-10 `TaskPresenceReader` 只读 seam（`lease.py:51`） | Task 是否在场客户端；只读，不反向登记（避免依赖环） | §3.2 `has_present_client` 维度 | **REUSE** |
| `signal_client_exit` / `inspect_exit_impact` / `_reap_if_orphaned` 分支（本设计） | 无成熟先例的 per-Task presence 语义（research §3 覆盖矩阵：无一家同时具备） | decision 11 的三处新面 | **BUILD**（本仓最小新面） |

**License 汇总**：DSH=MIT、Pi=MIT、Cline/Codex=Apache-2.0、asyncio=PSF；本设计**不实质复制**任何上游代码（ADAPT/PORT DESIGN 只借语义），来源与判定均已记录备查。本阶段**零新依赖**。

---

## 附：待用户确认与未决项

1. **decision 10 / §6**：「durably paused ⇒ 无需服务」作为产品语义——**待用户确认**；确认前不实现任何关停钩子。
2. **decision 9 / §5.3**：`02 §5.2.1`「意外断线超宽限须停收」的漂移——是否批准修订规格正文，**待用户决定**；本阶段不改规格文件。
3. 选项 B 已定（research §4.4）；本设计不重新解释产品方向。
