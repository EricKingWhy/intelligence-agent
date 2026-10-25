"""Session：Agent 交互历史的领域聚合根。

持有 session_id 与已加载事件列表（内存缓存），对外提供：
    - start() / resume()  构造入口
    - append()            追加事件（分配 seq + 同步写 JSONL + 更新内存）
    - add_listener()      追加监听器（事件落盘后实时回调，ADR-0016 §2.1）
    - derive_messages()   从事件投影模型可见 messages
    - begin_run() / end_run()  标记 Run 边界

SessionStore 负责 IO（薄层），Session 负责业务状态（seq 分配、dangling 修复）。
"""

from __future__ import annotations

import asyncio
import bisect
import logging
import threading
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import replace
from decimal import Decimal
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from langchain_core.messages import AnyMessage

from agent_harness.sandbox.base import Sandbox

if TYPE_CHECKING:
    from pathlib import Path

    from agent_harness.sandbox.registry import WorkspaceRegistry

from agent_harness.session.board_projection import get_board_projection_store
from agent_harness.session.cwd import cwd_event_data
from agent_harness.session.derive import (
    DANGLING_NOT_EXECUTED_DENIED,
    DANGLING_TOOL_CONTENT,
    build_protected_fact_data,
    derive_messages,
    detect_dangling,
    protected_fact_id,
    validate_protected_fact_data,
    validate_user_fact_links,
)
from agent_harness.session.errors import SeqConflict, SessionNotFound
from agent_harness.session.event import (
    EVENT_TYPES,
    MESSAGE_QUEUED,
    MODEL_COMPLETED,
    PERMISSION_RESOLVED,
    RUN_COMPLETED,
    RUN_FAILED,
    RUN_STARTED,
    RUN_TERMINAL_TYPES,
    SESSION_RESUMED,
    SESSION_STARTED,
    STEER_REQUESTED,
    STREAM_ONLY_TYPES,
    TASK_ACCEPTANCE_RELEASED,
    TASK_ACCEPTANCE_REVISED,
    TASK_ACCEPTED,
    TASK_DEFINED,
    TASK_FACT_REVOCATION,
    TASK_PROTECTED_FACT,
    TOOL_APPROVAL_REQUESTED,
    TOOL_RESULT,
    USER_INPUT_REQUESTED,
    USER_MESSAGE,
    VERIFICATION_UPDATED,
    SessionEvent,
)
from agent_harness.session.store import JsonlSessionStore

logger = logging.getLogger("agent_harness.session")


def _running_loop() -> asyncio.AbstractEventLoop | None:
    """当前线程的运行中 loop（无则 None）——闸门投递归属判定的共用读法。"""
    try:
        return asyncio.get_running_loop()
    except RuntimeError:
        return None


#: 看板投影触发集：只有这些事件会改变投影字段（产品态 / 注意力 / 最后消息）。
#: 触发集外的事件只推进 sidecar 水位（见 BoardProjectionStore.update_from_events）。
#:
#: `ponytail:` #1308 起把 `USER_MESSAGE` 与 `MODEL_COMPLETED` 纳入——卡片正文要显示
#: 「最后一条 user / agent 消息」，这两类事件才会改那两个字段。**成本变化是有意的**：
#: `MODEL_COMPLETED` 每个模型回合一条（runtime 三处写入），长 run 几十上百回合，
#: T1 的「单 run 约 4–8 次重放」上限被抬高到模型回合频率（消息新鲜度优先）。
#: 升级路径见 `board_projection.py` 的「已知上限」段（增量 fold / 尾部 O(1) 扫描），
#: 不是另写一份投影语义。
_BOARD_PROJECTION_TRIGGER_TYPES = (
    frozenset(
        {
            TASK_DEFINED,
            TASK_ACCEPTANCE_REVISED,
            VERIFICATION_UPDATED,
            TASK_ACCEPTED,
            TASK_ACCEPTANCE_RELEASED,
            RUN_STARTED,
            USER_MESSAGE,
            MODEL_COMPLETED,
            USER_INPUT_REQUESTED,
            TOOL_APPROVAL_REQUESTED,
            PERMISSION_RESOLVED,
        }
    )
    | RUN_TERMINAL_TYPES
)


def _board_projection_needs_recompute(event: SessionEvent) -> bool:
    """触发集判定（#1308 起 ``USER_MESSAGE`` / ``MODEL_COMPLETED`` 已在集合内）。

    T1 只对带 ``input_request_id`` 的 USER_MESSAGE 重算（那是注意力清除点）；
    #1308 把两类消息事件整体纳入，因为它们会改「最后一条消息」字段。保留本函数
    作为唯一判定点：钩子只认 ``recompute`` 布尔，触发集的形状不泄漏到 append 路径。
    """
    return event.type in _BOARD_PROJECTION_TRIGGER_TYPES


def validate_event_seq(session_id: str, events: list[SessionEvent]) -> None:
    """校验 seq 严格递增（不容忍重复或回退）——该校验规则的唯一 owner。

    ``Session.load``（加载即校验）与 ``service.change_model``（已自行读过快照，
    不再重复读盘）共用同一判据。

    违反 → ``SeqConflict``，**不是** ``SessionNotFound``：日志确实存在、只是损坏。
    见 BUG-011——旧实现抛裸 ``ValueError``，被 ``service`` 一刀切翻成 404
    「会话不存在」，于是真机会话 ``dd983104`` 的日志损坏（两条 seq=5）显示成
    ``续聊失败：Send failed: 404``。
    """
    seen_seqs: set[int] = set()
    prev_seq = -1
    for event in events:
        if event.seq in seen_seqs:
            raise SeqConflict(f"Session '{session_id}' 事件 seq 重复: {event.seq}")
        if event.seq <= prev_seq:
            raise SeqConflict(
                f"Session '{session_id}' 事件 seq 回退: {event.seq}（前一条: {prev_seq}）"
            )
        seen_seqs.add(event.seq)
        prev_seq = event.seq


class Session:
    """Agent 会话聚合根——Runtime 与外部世界的单一交互入口。

    **在途聚合 vs 其余聚合**（#1024 / #1055）：`Session.append` 的 seq 是构造时从已
    加载事件算出的乐观缓存。撞号要不要在 store 锁内当场改号重落，取决于这枚聚合
    **是否已被交给一个正在跑它的 runtime**——只有那种聚合的撞号才是「旁路写者刚抢走
    一个空档」，改号即可闭合；其余聚合的号可能是旧快照推出来的，撞号是**结构性**的。

    判据只能在交付点**显式建立**，不能从构造参数推断（#1055：早先按
    ``events is not None`` 推断，把服务层 `load()` / `resume()` 造出、随后交给 runtime
    的那一枚误判为非在途，使 #1024 的修复在真实续聊路径上完全失效——同一份 12 轮
    并发用例换成 load/resume 交付的聚合即丢终态）：

    * `deliver_to_runtime()`——由**交付点**在把聚合交给 runtime 时调用。此后该聚合
      撞号即在**同一把写锁内**改号重落（见 `_persist_event`／`store.append_event`
      的 `allocate`——取号、判定、写盘同临界区，杜绝「核对→写」窗口）。交付点仅三处：
      `RunManager.launch`（服务层创建 / 续聊 / 暂停续跑三条腿共用）、CLI `run` 直接调
      `runtime.run_stream`、多代理子 runtime。⚠ 这是个**封闭集合**，但集合本身会随
      生产入口增加而变——`AgentRuntime.run()`（非流式入口）目前**没有生产调用方**，
      若将来出现，它同样必须在入口处标记 `deliver_to_runtime()`，否则那条路径上的
      撞号会退化成 fail-loud（#1055 的 P0 就是「漏掉一个交付点」）。
    * 未标记的聚合——撞号原样抛出，收口归调用方 owner（服务层 `change_model` 一族按
      `_WRITE_CONFLICT_ATTEMPTS` 有界重试、旁路写者 `SessionEventSink` 自带
      `_SINK_ATTEMPTS` 重读循环）。本层**不**替它们吞冲突，否则那些上界与失败归因
      会变成不可观测（BUG-011）。
    """

    def __init__(
        self,
        session_id: str,
        store: JsonlSessionStore,
        events: list[SessionEvent] | None = None,
        sandbox: Sandbox | None = None,
    ) -> None:
        self.session_id = session_id
        self._store = store
        self._events: list[SessionEvent] = events if events is not None else []
        # 增量 seq 计数器：构造时一次性从已加载事件重算（max+1），append 时 O(1) 分配，
        # 避免每次 append 对全量事件做 O(n) max 扫描（长会话累计 O(n²)）
        self._next_seq: int = max((e.seq for e in self._events), default=-1) + 1
        # 取消/关闭打断过一次在途异步写 ⇒ 计数器可能落后盘面（UNKNOWN）。置位后
        # 下一次构造事件前先向 store 要盘面事实（#1489）。
        self._seq_uncertain: bool = False
        # 本聚合是否已被**交付点**交给在途 runtime（#1055）。默认关：撞号原样抛出，
        # 把结构性冲突交给调用方的有界重试与失败归因（BUG-011）。交付点显式调
        # `deliver_to_runtime()` 打开它——**不从构造参数推断**：服务层 `load()` /
        # `resume()` 造出的聚合同样会被交给 runtime（#1055 的 P0 就是这么漏掉的）。
        self._delivered_to_runtime: bool = False
        self._sandbox: Sandbox | None = sandbox
        # 追加监听器（ADR-0016 §2.1）：事件持久化后同步回调——web 层据此实时
        # 广播 durable 事实（含工具执行期间追加的 output_delta）。回调异常被
        # 吞掉（落日志）：listener 是观察者，绝不能破坏 append 的持久化契约。
        self._listeners: list[Callable[[SessionEvent], None]] = []
        # ── 有序投递闸门（#1547）──
        # #1489 把 fsync 等待移出事件循环后，写者的**完成顺序**不再等于 seq 顺序
        # （`_GroupFlush` cohort 的唤醒顺序 ≠ 取号顺序）。若内存投影与 listener
        # 通知跟随各自写者的完成时刻，`RunManager.enqueue_session_event` 的 seq
        # 幂等门会把迟到帧**永久丢弃**（live SSE 与 after_seq 重放双路都丢）。
        # 闸门把「内存投影 + listener 通知」收口成**按 seq 单调**的投递：
        #   * seq 未定的在途写者（`_gate_inflight` 值为 None）关住游标——它可能
        #     占到更小的号；定号后才放行（等待的是**真实在途的写**，不是定时窗口）；
        #   * 已知 seq 但尚未走到投递点的写者（`_gate_undelivered`）同样挡住
        #     更高 seq 的放行；
        #   * 写失败/等待者取消的 seq 不通知、也不挡路（`_gate_skipped` 透明跳过）。
        # `threading.Lock`：同步 `append` 的调用线程可能与事件循环并发触碰闸门。
        self._gate_guard = threading.Lock()
        self._gate_inflight: dict[int, int | None] = {}
        self._gate_next_token = 0
        self._gate_undelivered: set[int] = set()
        self._gate_ready: dict[int, SessionEvent] = {}
        # skipped 保留事件对象（审查 F1）：取消写者的内存补插必须推迟到 drain
        # 消费该 seq 的时刻（前置 ①③ 保证那时没有更低的在途/未登记 seq）——
        # 在取消臂/完成回调当场 insort 会越过仍在途的更低 seq，把 `_events`
        # 倒插成 [0,2,1]。
        self._gate_skipped: dict[int, SessionEvent] = {}
        self._gate_draining = False
        # 已消费（投递或透明跳过）的最大 seq：投递严格按 seq 单调推进，水位用于
        # 让 `_deliver_ordered` 幂等（同一事件可能已被其它等待者的 drain 放行）。
        self._gate_delivered_through = -1
        # ready 中每个 seq 的**投递归属 loop**（#1547 审查 P0）：异步写者的事件
        # 必须回到它自己的事件循环上通知 listener（#1489 冻结的线程亲和——
        # `web.AskUserTimeoutDriver` 依赖「append_async 的回调发生在 loop 内」）；
        # 同步写者登记 None（本就是任意线程契约，就近投递）。drain 在别的线程上
        # 遇到 loop 亲和事件时只做转发，不在本地通知。
        self._gate_ready_loops: dict[int, asyncio.AbstractEventLoop | None] = {}
        # 已走到投递点、但看板投影还欠一次 sidecar 更新的 seq（同步路径投递后由
        # drain 补看板；异步路径自己经 to_thread 更新，不登记在此）。
        self._gate_board_pending: set[int] = set()
        # 投递水位等待者（#1547）：`(seq, loop, future)`。drain 每次推进水位后
        # 按 seq 序经 `call_soon_threadsafe` 唤醒——drain 可能跑在工作线程，
        # 跨线程 set future 不安全。等待者唤醒序 = seq 序，镜像方（`append_async`
        # 返回 → run_stream yield）因此保持单调。
        self._gate_waiters: list[
            tuple[int, asyncio.AbstractEventLoop, asyncio.Future]
        ] = []
        # 闸门清空等待者（#1547 审查 P1）：`(loop, future)`。`mark()/since()` 的
        # 长度游标以内存投影为界，而同步写者的投递可能被在途写者推迟到返回之后
        # ——快照消费者（runtime 的工具 delta 记账）等闸门清空再取 since()。
        self._gate_settled_waiters: list[
            tuple[asyncio.AbstractEventLoop, asyncio.Future]
        ] = []

    @property
    def sandbox(self) -> Sandbox | None:
        """与 Session 绑定的 Sandbox（通过 WorkspaceRegistry 管理）。不传 registry 时为 None。"""
        return self._sandbox

    @property
    def events(self) -> list[SessionEvent]:
        """已加载的事件列表（内存缓存，只读视图）。"""
        return list(self._events)

    @property
    def next_seq(self) -> int:
        """下一条事件的 seq（增量计数器，构造时从已加载事件取 max+1，空列表从 0 开始）。"""
        return self._next_seq

    def mark(self) -> int:
        """当前追加位置的句柄——配合 since() 取"之后追加的事件"。

        调用方不需要知道 events 的内部表示（列表/游标/页）；这是
        Session 拥有的追加语义，替代调用方自己做 len(events) 算术。
        """
        return len(self._events)

    def since(self, marker: int) -> list[SessionEvent]:
        """返回 mark() 之后追加的事件（副本，不影响内部状态）。"""
        return list(self._events[marker:])

    # ── 追加监听器（ADR-0016 §2.1）──

    def add_listener(self, callback: Callable[[SessionEvent], None]) -> None:
        """注册追加监听器：此后每条事件持久化成功后同步回调（任意线程上下文）。"""
        self._listeners.append(callback)

    def remove_listener(self, callback: Callable[[SessionEvent], None]) -> None:
        """注销监听器；未注册时静默（幂等）。"""
        with suppress(ValueError):
            self._listeners.remove(callback)

    # ── 构造入口 ──

    @classmethod
    def start(
        cls,
        store: JsonlSessionStore,
        *,
        agent_id: str = "default",
        session_id: str | None = None,
        workspace_registry: WorkspaceRegistry | None = None,
        started_data: dict | None = None,
        cwd: str | Path | None = None,
        # #363 / W-19：显式选择的 sandbox 后端（"local" | "docker"）；None ⇒
        # registry 默认。选择记进 session/started（审计事实，抄 DSH `sandbox/mode`
        # log-only 事件）；docker 不可用时 registry.create 抛 SandboxUnavailableError，
        # 会话不创建（fail-fast，零副作用——与 local fuse 同一条"被拒不落事件"纪律）。
        sandbox_backend: str | None = None,
    ) -> Session:
        """新建 Session：生成 id、创建 JSONL、append session/started。

        提供 workspace_registry 时，自动创建/绑定 Sandbox 实例到 session.sandbox。
        session_id 允许调用方预生成（web 层"先组装 runtime 后建 Session"的顺序
        需要：_build_runtime 要以 session_id 装配 S3 artifact 命名空间，组装失败
        时不能留下任何已落盘的孤儿 session——R6-6）。
        started_data（T7 #137，加法字段）：会话级初始配置写进 session/started——
        目前用于记录创建时选定的模型（provider / model_id），使"当前模型"可从
        事件流派生。
        cwd（WS-1 #151，加法字段）：会话侧工作目录锚，**由创建者赋予**（不由
        WorkspaceRegistry 反向灌给会话）。经 `canonical_workspace_path` 规范化后
        写进 session/started，此后不可变；None = 不写该字段（历史遗留语义）。
        `started_data` 里若夹带 `cwd` 键会被丢弃并记一条 warning——写侧只认本参数，
        否则那条路径会绕过 AC5 的"唯一一套规范化"。
        """
        session_id = session_id or str(uuid4())
        sandbox = None
        if workspace_registry is not None:
            sandbox = workspace_registry.create(session_id, backend=sandbox_backend)
        session = cls(session_id, store, sandbox=sandbox)
        data = dict(started_data) if started_data else {}
        # #363 / W-19：sandbox 后端选择记进 session/started（审计事实）。
        # 显式选择才写；None 表示"未显式选择，走部署默认"，不写（避免把"默认"
        # 误记成"用户选了 local"——DSH 生效公式里 explicit 与 default 是两档）。
        if sandbox_backend is not None:
            data["sandbox_backend"] = sandbox_backend
        # cwd 只认显式参数：started_data 里夹带的同名键会**绕过**唯一一套规范化
        # （AC5），静默写出一个未规范化/相对的 cwd。删掉它，再按参数写入。
        if "cwd" in data:
            logger.warning(
                "Session.start 忽略了 started_data['cwd']=%r：cwd 只由 cwd 参数"
                "赋予（WS-1 #151），否则会绕过规范化",
                data["cwd"],
            )
        data.pop("cwd", None)
        data.update(cwd_event_data(cwd))
        session.append(SESSION_STARTED, data, agent_id=agent_id)
        return session

    @classmethod
    def append_event(
        cls,
        store: JsonlSessionStore,
        session_id: str,
        event_type: str,
        data: dict,
        *,
        run_id: str | None = None,
        agent_id: str | None = None,
        step_id: int | None = None,
    ) -> SessionEvent:
        """只追加一条会话事实事件，**不触发 resume 副作用**。

        本入口是**旁路写者**形状（加载一枚新聚合再追加）：它交给调用方自己的有界
        重试，不打 `terminal`（#1061 的终态保底只给在途 runtime 的 `end_run`）。

        ``resume()`` 会修复 dangling tool_call 并 append ``session/resumed``——
        那是「恢复会话」的语义。写一条无关事实（排队消息 / 模型切换 / 取消排队 /
        中断标记）时套用它，会把 run 在途的 tool_call 判成悬空并注入合成
        tool/result，破坏 tool_call/result 配对（不变量 #7）。本入口只做
        「加载 + 追加」；run_id / agent_id / step_id 透传事件信封（T8 #138 的
        ``run/interrupted`` 要挂到被中断的 run 上）。

        加载后同样校验 seq（BUG-011）：损坏日志在这里也要报 ``SeqConflict``，不能
        一边说日志已坏、一边继续往上追加（排队消息 / 中断标记等旁路写者）。
        """
        events = store.read_events(session_id)
        if not events:
            raise SessionNotFound(f"Session '{session_id}' 不存在或事件日志为空")
        validate_event_seq(session_id, events)
        session = cls(session_id, store, events)
        return session.append(
            event_type,
            dict(data),
            run_id=run_id,
            agent_id=agent_id,
            step_id=step_id,
        )

    def deliver_to_runtime(self) -> None:
        """把本聚合标记为「已交给在途 runtime」（#1055）。

        由**交付点**调用，仅三处：`RunManager.launch`（服务层创建 / 续聊 / 暂停续跑
        三条腿共用）、CLI `run` 的 `runtime.run_stream`、多代理子 runtime。

        标记之后 `append` 撞号即在 store **同一把写锁内**改号重落（#1062 裁决 A）：
        此时这枚聚合就是 runtime 正在写的那一枚，撞号只可能是旁路写者抢走了空档。
        未标记的聚合保持原样抛出，收口归调用方——`change_model` 一族 /
        `SessionEventSink` 各有自己的有界重试与失败归因（BUG-011），本层替它们吞掉
        会让那些上界不可观测。
        """
        self._delivered_to_runtime = True

    @classmethod
    def load(
        cls,
        store: JsonlSessionStore,
        session_id: str,
        *,
        workspace_registry: WorkspaceRegistry | None = None,
    ) -> Session:
        """加载并校验 seq 的**无副作用**入口：不修复 dangling、不写 session/resumed。

        用于「恢复语义已由上游完成」的场景——崩溃路径先经 RecoveryCoordinator
        按 Ledger 精确回填（不变量 #13/#14），再 load 出 Session 继续跑，
        避免与恢复标记重复再写一条 `session/resumed`。

        sandbox 是**尽力而为**的（#1235）：映射文件缺失（历史会话 / 映射卷被清空 /
        手工构造）时降级为 ``sandbox=None``，与不传 registry 同义——写路径端点
        只用 load 追加事件，不该因映射缺席 500。需要 sandbox 的调用方自行判空。
        但「映射此刻不可用」仍然上抛 `WorkspaceBindingError`（硬删除进行中 /
        重绑定 / Docker 持久卷缺失），那不是「没有映射」。
        """
        events = store.read_events(session_id)
        if not events:
            raise SessionNotFound(f"Session '{session_id}' 不存在或事件日志为空")

        sandbox = None
        if workspace_registry is not None:
            try:
                sandbox = workspace_registry.get(session_id)
            except KeyError:
                logger.warning(
                    "Session %s 没有 workspace 映射，sandbox 降级为 None（#1235）",
                    session_id,
                )
        validate_event_seq(session_id, events)
        return cls(session_id, store, events, sandbox=sandbox)

    @classmethod
    def resume(
        cls,
        store: JsonlSessionStore,
        session_id: str,
        *,
        workspace_registry: WorkspaceRegistry | None = None,
    ) -> Session:
        """加载已有 Session：校验 seq、修复 dangling、append session/resumed。

        提供 workspace_registry 时，自动查回/恢复 Sandbox 实例到 session.sandbox；
        查不到映射时与 `load` 同口径降级为 None（见 `load` 的 #1235 说明）。
        """
        session = cls.load(store, session_id, workspace_registry=workspace_registry)

        # 修复 dangling tool_call：为每个未解决的 tool_call 追加合成 tool/result。
        # #566：resume 没有 Ledger 读数，只有「审批无决议」这一半可以从事件流证明
        # （审批闸门在接纳点之前，无决议 ⇒ 接纳点未到 ⇒ 未执行）；其余形态
        # （已批准 / 无审批请求）无法排除"执行已开始、结果丢失"，保留保守占位。
        # 方法内导入：session.approval → tooling 包 → executor → session 包，
        # 模块顶层会成环（与 service.py 的 fork 延迟导入同一纪律）。
        from agent_harness.session.approval import (
            ApprovalOutcome,
            approval_outcome_for_call,
        )

        dangling_ids = detect_dangling(session._events)
        for tc_id in dangling_ids:
            if approval_outcome_for_call(session._events, tc_id) is (
                ApprovalOutcome.UNRESOLVED
            ):
                content = DANGLING_NOT_EXECUTED_DENIED
            else:
                content = DANGLING_TOOL_CONTENT
            logger.warning(
                "Resume 修复 dangling tool_call_id=%s，追加合成 tool/result", tc_id
            )
            session.append(
                TOOL_RESULT,
                {"tool_call_id": tc_id, "content": content},
                source_event_ids=[tc_id],
                _mark_dangling=True,
            )

        session.append(SESSION_RESUMED, {})
        return session

    # ── 核心操作 ──

    def append(
        self,
        event_type: str,
        data: dict,
        *,
        run_id: str | None = None,
        agent_id: str | None = None,
        step_id: int | None = None,
        block_id: str | None = None,
        source_event_ids: list[str] | None = None,
        _mark_dangling: bool = False,
        terminal: bool = False,
    ) -> SessionEvent:
        """追加一条事件：分配 seq、同步写 JSONL、更新内存。

        _mark_dangling 仅内部使用——在 data 中写入 dangling=true 标记。
        block_id 是流式块标识（ADR-0016 §3.2，reasoning 块等），透传给 SessionEvent。
        事件类型必须在 EVENT_TYPES 词汇表内；STREAM_ONLY_TYPES（流式专属信号）
        拒绝持久化（invariant #4：Event ≠ Diagnostic Log）。

        #1547：返回即「已落盘」不变；若有更低 seq 的在途写者，内存投影与 listener
        通知经闸门**按 seq 保序**放行（可能晚于本调用返回，但必然发生且不乱序）。

        异步调用链（会阻塞事件循环的路径）走 :meth:`append_async`：#1489 起
        durable 刷盘的等待可以从事件循环移出，写入与确认语义完全不变。
        """
        event = self._build_event(
            event_type,
            data,
            run_id=run_id,
            agent_id=agent_id,
            step_id=step_id,
            block_id=block_id,
            source_event_ids=source_event_ids,
            mark_dangling=_mark_dangling,
        )
        # 返回**实际落盘的那一枚**：已交付 runtime 的聚合撞号会在 store 锁内改号
        # （#1062），调用方（`end_run` 的返回值、流式镜像、测试断言）必须拿到与事件流
        # 一致的 event_id/seq 对，否则「返回的 seq」与「盘上的 seq」静默分叉。未交付的
        # 聚合不换号（撞号原样抛出）。
        return self._persist_event(event, notify=True, terminal=terminal)

    def register_protected_fact(
        self,
        *,
        fact_type: str,
        value: Any,
        source_event_id: str | None,
        supersedes_fact_id: str | None = None,
        evidence_event_id: str | None = None,
    ) -> SessionEvent:
        """Append a fact registration after validating its original event sources."""
        data = build_protected_fact_data(
            self._events,
            session_id=self.session_id,
            fact_type=fact_type,
            value=value,
            source_event_id=source_event_id,
            supersedes_fact_id=supersedes_fact_id,
            evidence_event_id=evidence_event_id,
        )
        existing = next(
            (
                event
                for event in self._events
                if event.type == TASK_PROTECTED_FACT
                and event.data.get("fact_id") == data["fact_id"]
            ),
            None,
        )
        if existing is not None:
            if existing.data != data:
                raise ValueError("protected fact id already exists with different data")
            return existing
        return self.append(TASK_PROTECTED_FACT, data)

    def _persist_event(
        self,
        event: SessionEvent,
        *,
        notify: bool,
        terminal: bool = False,
    ) -> SessionEvent:
        """提交一条已构造事件，并在需要时通知实时观察者；返回**已落盘的那一枚**。

        Store 是 durable 写入 owner，Session 仍拥有内存投影与 seq 计数器。
        顺序不可交换：store 成功后才更新内存/seq，避免失败留下空洞；history
        adoption 通过 ``notify=False`` 保持离线 seed 语义。

        **seq 分配在 store 写锁内（#1062 裁决 A）**：本聚合的计数器只是「盘上最大
        seq」的乐观缓存，任何旁路写者（后台记忆作业的 ``SessionEventSink``、服务层
        的旁路追加）都能在两次 append 之间把号占掉。旧实现（#1024 起）在会话侧
        **自旋重投**（撞号 → 读盘面 → 换号 → 再 append），有两个残余：调用线程是
        runtime 事件循环协程（``_drive``）时，持续占位下一次 append 可纯自旋最长 1.0s、
        期间同 loop 的 SSE 心跳/其它会话全部停摆；对手不收手时事件一次都不前进。

        裁决 A 把**取号、判定、写盘**收进 store 的**同一临界区**（会话写锁 + #1063 的
        跨进程文件锁）：撞号不再是会话侧要处理的异常，而是 store 在锁内直接改号到
        锁内读到的空档再落盘（``store.append_event`` 的 ``allocate``）。自旋与墙钟预算
        随之删除——**没有可插入的窗口**，收敛由构造保证（Kafka 由 broker 在 append 时
        分配 offset、SQLite WAL 单写者锁、EventStoreDB ``WrongExpectedVersion``／
        Marten ``ConcurrencyException`` 的存储层 fail-closed 同型：分配必须在写锁内）。

        触发条件（沿用 #1055 的快照门，A **不改**这条边界）：只有被**交付点**显式交给
        在途 runtime 的聚合（``_delivered_to_runtime``）才在锁内改号；未交付的聚合
        撞号仍原样抛 ``SeqConflict``，收口归调用方 owner（``change_model`` 一族的
        ``_WRITE_CONFLICT_ATTEMPTS``、sink 的 ``_SINK_ATTEMPTS``），本层替它们吞掉会让
        那些上界与失败归因不可观测（BUG-011）。**终态写者**（``terminal=True``，只有
        ``end_run``）即使未交付也改号——它没有「下次 append」可用，放它不落等于 run 在
        盘上永远悬空（#1061 的保底通道，A 是它的推广而非替换）。
        """
        persisted: SessionEvent
        # 闸门登记（#1547）：同步写者与异步写者走同一道闸——`_GroupFlush` cohort
        # 的唤醒顺序同样可能让两个同步写者的完成顺序颠倒（取号 6 先于取号 5 返回），
        # 只有「先占位、完成后再投递」能同时覆盖同步/异步两条入口。adopt_history
        # （notify=False）是离线 seed，无并发在途写者，不经闸门——保持「直接落
        # 内存」的批量语义（fork N 条历史时的 O(N) 而非 O(N) 次闸门往返）。
        token: int | None = None
        if notify:
            with self._gate_guard:
                token = self._gate_next_token
                self._gate_next_token += 1
                self._gate_inflight[token] = None
        try:
            persisted = self._store.append_event(
                self.session_id,
                event,
                # `allocate`：撞号时在 store 锁内改号再落盘。已交付聚合 + 终态写者
                # 都吃这条路径；未交付的非终态写者仍 fail-closed（快照门，BUG-011）。
                allocate=self._delivered_to_runtime or terminal,
            )
        except BaseException:
            # 写失败透明：不占闸位、不投递、不挡路（同异步入口的失败分支）。
            if token is not None:
                with self._gate_guard:
                    self._gate_inflight.pop(token, None)
                self._drain_ordered()
            raise
        return self._commit_persisted(persisted, notify=notify, token=token)

    async def append_async(
        self,
        event_type: str,
        data: dict,
        *,
        run_id: str | None = None,
        agent_id: str | None = None,
        step_id: int | None = None,
        block_id: str | None = None,
        source_event_ids: list[str] | None = None,
        _mark_dangling: bool = False,
        terminal: bool = False,
    ) -> SessionEvent:
        """:meth:`append` 的异步入口（#1489）：durable 落盘的等待不占事件循环。

        语义与 :meth:`append` **逐字相同**（同样的词汇表校验、同样的 seq 分配、
        同样的 ``allocate`` 门、同样的内存投影/监听器顺序），差别只有一处：

        * 临界区内的 write+flush 仍在本线程同步完成（微秒级）；
        * 本轮 ``os.fsync`` 的等待交到工作线程，且与同时到达的写者**共享**一次
          刷盘。

        于是「事件循环在等磁盘」这条阻塞被移除，而「返回即耐久」「不可吞写失败」
        「不确认未耐久事件」三条底线都不动。

        ⚠ **本协程写入的事件，其 listener 通知必然回到调用方的事件循环上**
        （#1547 闸门按 `_gate_ready_loops` 归属转发，别的线程只投递自己的
        同步事件）：``web`` 的 ``AskUserTimeoutDriver`` 显式依赖「append 的
        后续回调发生在 loop 内」，本方法保住这条假设（见该模块 docstring
        的告警）。
        """
        event = self._build_event(
            event_type,
            data,
            run_id=run_id,
            agent_id=agent_id,
            step_id=step_id,
            block_id=block_id,
            source_event_ids=source_event_ids,
            mark_dangling=_mark_dangling,
        )
        # 闸门登记（#1547）：这个写者从此刻起占一个「seq 未定」的闸位——它的
        # 完成回调定号前，任何更高 seq 的事件都不得先行投递（可能被它压号）。
        with self._gate_guard:
            token = self._gate_next_token
            self._gate_next_token += 1
            self._gate_inflight[token] = None
        task = asyncio.ensure_future(
            self._store.append_event_async(
                self.session_id,
                event,
                allocate=self._delivered_to_runtime or terminal,
            )
        )
        # 等待者状态（#1547）：回调与取消臂共享。`seq` 在写完成时由回调回填；
        # `cancelled` 在等待者放弃时置位——两者谁先到都能正确收口。
        waiter: dict[str, int | bool] = {}

        def _commit_memory(done: asyncio.Future) -> None:
            """写完成时把计数器对齐到**实际写入的那一枚**（#1489），并给闸门定号。

            取消/关闭打断的是「等待」，线程里那行可能已在文件里、只有「是否耐久」
            是 UNKNOWN。不对齐的话 `_next_seq` 落后盘面，紧接着的终态写（取消臂的
            `run/failed`）会以同号重投撞 `SeqConflict` —— 那条 run 在盘上就永远
            悬空（#550/#1061 要堵的正是这个）。#1547 起，内存投影不再在这里做，
            而是随闸门按 seq 有序投递；**等待者已放弃**的 seq 登记进 `_gate_skipped`
            （保留事件对象），内存补插由 drain 消费到该 seq 时保序完成（审查 F1：
            当场插入会越过仍在途的更低 seq），通知按 #1489 语义不补发。
            """
            if done.cancelled() or done.exception() is not None:
                with self._gate_guard:
                    self._gate_inflight.pop(token, None)
                self._drain_ordered()
                return
            persisted = done.result()
            with self._gate_guard:
                # `max`：取消后的对齐可能已经把计数器推到更前面，回调晚到时不许倒退。
                self._next_seq = max(self._next_seq, persisted.seq + 1)
                waiter["seq"] = persisted.seq
                if waiter.get("cancelled"):
                    # 等待者已放弃：写本身成功，seq 已定——登记为透明跳过并当场
                    # 撤出闸位（通知按 #1489 语义不补发）。内存补插不在此刻做：
                    # 更低 seq 可能仍在途，drain 消费到该 seq 时再保序插入（F1）。
                    self._gate_inflight.pop(token, None)
                    self._gate_skipped[persisted.seq] = persisted
                else:
                    self._gate_inflight[token] = persisted.seq
                    self._gate_undelivered.add(persisted.seq)
            self._drain_ordered()

        task.add_done_callback(_commit_memory)
        # `shield`：取消只放弃**等待**，不撤销已在跑的那次写；`_commit_memory` 在
        # 本协程被唤醒之前执行（Future 的 done 回调按登记顺序派发，它早于 shield
        # 自己的转发回调），因此下面醒来时计数器对齐一定已经完成。
        try:
            persisted = await asyncio.shield(task)
        except asyncio.CancelledError:
            # 那次写还在跑：本条是否落盘属 UNKNOWN，取号也可能已被它占掉。不猜、
            # 也不在取消路径上等磁盘——只记下「计数器可能落后」，下一次 append 前
            # 向 store 要盘面事实（`last_written_seq` 取写锁读，与在途写者串行）。
            skipped = False
            with self._gate_guard:
                waiter["cancelled"] = True
                late_seq = waiter.get("seq")
                if not isinstance(late_seq, int):
                    late_seq = None  # 回调未到：留给 _commit_memory 的取消分支收口
                elif late_seq <= self._gate_delivered_through:
                    late_seq = None  # 已被其它等待者的 drain 按序投递，无需补插
                elif late_seq in self._gate_undelivered:
                    # 完成回调先到、等待者随后被取消：写已成功只是没等它——透明
                    # 跳过（事件对象随 `_gate_skipped` 保留，内存补插由 drain
                    # 消费到该 seq 时保序完成，审查 F1：当场插入会越过仍在途的
                    # 更低 seq 把 `_events` 倒插成 [0,2,1]）。
                    self._gate_undelivered.discard(late_seq)
                    self._gate_inflight.pop(token, None)
                    if (
                        task.done()
                        and not task.cancelled()
                        and task.exception() is None
                    ):
                        self._gate_skipped[late_seq] = task.result()
                        skipped = True
                    else:
                        late_seq = None  # 防御：undelivered 只由完成回调登记
                else:
                    late_seq = None  # 防御：不在 undelivered 也不该由本臂处理
            if skipped:
                self._drain_ordered()
            self._seq_uncertain = True
            raise
        # 投递（内存投影 + listener）按 seq 有序放行（#1547）。恢复点到投递点之间
        # 没有 await——不可能在这里被取消而在闸门里留下永久占位。
        self._deliver_ordered(persisted, token)
        # 等本条**真正投递**再返回（#1547）：run_stream 依据本返回值 yield durable
        # 镜像（带 seq），镜像先于更低 seq 入队会被 RunManager 的幂等门当重复丢弃。
        # 本条投递 ⟹ 全部更低 seq 已投递（闸门保证），镜像因此永不跳号。若此刻
        # 被取消：写已耐久、事件已在闸内 ready，后续 drain 照常按序投递——等待者
        # 放弃的只是「拿返回值」，不产生需要收口的 UNKNOWN。
        await self.wait_delivered(persisted.seq)
        # 看板投影同样是**逐事件一次 sidecar 重写 + fsync**（`_write_sidecar`），
        # 留在循环上就等于把 #1489 要消除的那次等待原样搬了个位置——它也是派生索引
        # （best-effort、绝不回灌事件流），所以一并移出。放在投递之后：此刻
        # `_events` 必然已含自身与全部更低 seq（闸门保证），快照是前缀完备的。
        # `_events` 快照在**循环线程**上取（与
        # `runmanager._strict_progress_write_for_exit` 同款纪律：跨线程迭代
        # 活列表不安全）。
        await asyncio.to_thread(
            self._update_board_projection,
            persisted,
            list(self._events),
        )
        return persisted

    def _build_event(
        self,
        event_type: str,
        data: dict,
        *,
        run_id: str | None,
        agent_id: str | None,
        step_id: int | None,
        block_id: str | None,
        source_event_ids: list[str] | None,
        mark_dangling: bool,
    ) -> SessionEvent:
        """`append` 的构造段（校验 + 信封 + 取号），同步与异步入口共用的唯一一份。"""
        if self._seq_uncertain:
            # 上一次异步写在取消/关闭中成了 UNKNOWN：那条行可能已在盘上并占掉一个号。
            # 不猜——向 store 要盘面事实再取号。`max` 保证只前进不倒退（在途写者
            # 晚到的 `_commit_memory` 也可能已经把计数器推得更远）。
            self._next_seq = max(
                self._next_seq,
                self._store.last_written_seq(self.session_id) + 1,
            )
            self._seq_uncertain = False
        # 词汇表校验：先拒绝再写盘，杜绝未知/流式事件悄悄污染 durable log
        if event_type in STREAM_ONLY_TYPES:
            raise ValueError(
                f"流式专属事件 '{event_type}' 不得通过 Session.append 持久化"
                "（仅作为 run_stream() 的 AgentEvent 输出）"
            )
        if event_type not in EVENT_TYPES:
            raise ValueError(f"未知事件类型 '{event_type}'：不在 EVENT_TYPES 词汇表中")
        if event_type == USER_MESSAGE:
            refs = validate_user_fact_links(
                self._events, data, session_id=self.session_id
            )
            if refs:
                if source_event_ids is not None and source_event_ids != refs:
                    raise ValueError("user fact links do not match source_event_ids")
                source_event_ids = refs
        elif event_type == TASK_PROTECTED_FACT:
            validate_protected_fact_data(self._events, data, session_id=self.session_id)
            refs = [data["source_event_id"]]
            if data.get("evidence_event_id") is not None:
                refs.append(data["evidence_event_id"])
            if source_event_ids is not None and source_event_ids != refs:
                raise ValueError(
                    "protected fact event references do not match its payload"
                )
            source_event_ids = refs
        return SessionEvent(
            seq=self._next_seq,
            type=event_type,
            session_id=self.session_id,
            run_id=run_id,
            agent_id=agent_id,
            step_id=step_id,
            block_id=block_id,
            data={**data, "dangling": True} if mark_dangling else data,
            source_event_ids=source_event_ids,
        )

    def _commit_persisted(
        self,
        persisted: SessionEvent,
        *,
        notify: bool,
        token: int | None = None,
    ) -> SessionEvent:
        """同步入口的收口（#1547：经闸门按 seq 保序投递；离线 seed 直接落内存）。

        store 返回**实际落盘的那一枚**：锁内分配会改号，不接住它就会「内存/返回值是
        旧号、盘上是新号」——`_events`、`_next_seq` 与调用方（`end_run` 的返回值、
        流式镜像）三处一起分叉。**不回落到本地事件**：`append_event` 的契约是
        `SessionEvent`（docstring 的「:returns: 实际落盘的那一枚」），全仓替换件
        （含测试替身）都返回它；`None` 只可能来自契约外的实现，那种情况下回落到
        旧号反而会静默分叉（#1102 删掉了这条例外分支——它已无调用方可走）。
        """
        event = persisted
        if not notify:
            self._events.append(event)
            # 写盘成功后才推进计数器——失败不消耗 seq。必须按**实际写入的号**前进：
            # 锁内分配会改号，写成 `+= 1` 会留下过期缓存，把下一次 append 直接送进
            # 冲突分支（#1024 实测：改 `+= 1` 后终态缺失率立刻非零）。`max`：离线
            # seed 与既有计数器只前进不倒退。
            self._next_seq = max(self._next_seq, event.seq + 1)
            return event
        # notify=True：撤出闸位、登记投递点，交给闸门按 seq 放行（#1547）。本条
        # 可能因更低 seq 仍在途而**暂存未投**——此刻不能在调用线程上等它（那个
        # 更低 seq 的投递点可能在事件循环上，等 = 死锁）；调用方拿返回值继续即可，
        # 投递由闸门保证最终发生（每个状态迁移的线程都会尝试 drain）。
        with self._gate_guard:
            if token is not None:
                self._gate_inflight.pop(token, None)
            self._next_seq = max(self._next_seq, event.seq + 1)
            self._gate_ready.setdefault(event.seq, event)
            self._gate_board_pending.add(event.seq)
        self._drain_ordered()
        return event

    # ── 有序投递闸门（#1547）──

    def _deliver_ordered(self, persisted: SessionEvent, token: int) -> None:
        """异步写者的投递点：撤闸位、登记事件（含归属 loop），尝试放行投递。

        本方法只在写者自己的协程里执行 ⇒ `get_running_loop()` 即投递归属 loop
        （#1489 冻结的 listener 线程亲和，见 `_gate_ready_loops`）。
        """
        loop = asyncio.get_running_loop()
        with self._gate_guard:
            self._gate_inflight.pop(token, None)
            delivered_early = persisted.seq <= self._gate_delivered_through
            if not delivered_early:
                self._gate_undelivered.discard(persisted.seq)
                self._gate_ready.setdefault(persisted.seq, persisted)
                self._gate_ready_loops.setdefault(persisted.seq, loop)
        # 已被其它等待者的 drain 按序投递（投递唯一一次的保证）时不重复登记，
        # 但撤闸位本身也是状态迁移——照常尝试一次 drain（审查 F6：若在此早退，
        # 闸内又无其它活跃写者，settle 等待者会失去唯一一次唤醒机会）。
        self._drain_ordered()

    def _drain_ordered(self) -> None:
        """闸门放行循环：按 seq 单调地做「内存投影 + 看板 + listener」投递。

        四个前置有一个不满足即返回，等下一个状态迁移再试：
        ① 存在 seq 未定的在途写者（它可能压号，放行更高 seq 会造成倒序）；
        ② 没有任何可消费的 seq（ready/skipped 皆空）；
        ③ 最小可消费 seq 之下还有「已写完、未走到投递点」的 seq——先等它登记；
        ④ 最小可消费 seq 归属**别的 loop**——本地线程不得代投（#1489 冻结的
          listener 线程亲和，`web.AskUserTimeoutDriver` 依赖之），转发一次
          `call_soon_threadsafe` 到归属 loop 再返回；循环侧 drain 会以同一
          前置集合重扫，转发不改变放行顺序（只推迟、绝不禁绕过）。

        `_gate_draining` 防两层重入：listener 同步再 append（同线程递归，外层
        while 重扫即可覆盖）与跨线程并发 drain（串行化，防止两个线程交错投递
        造成通知乱序）。投递动作（append/看板/listener）在锁外执行——看板
        sidecar 写含 fsync，绝不能进临界区；跑在 loop 上时看板欠账转交执行器
        （#1489 的循环不等待磁盘目标），快照在转交前于当前线程取好。
        """
        with self._gate_guard:
            if self._gate_draining:
                return
            self._gate_draining = True
        try:
            while True:
                with self._gate_guard:
                    if any(seq is None for seq in self._gate_inflight.values()):
                        return
                    if not self._gate_ready and not self._gate_skipped:
                        return
                    seq = min(self._gate_ready.keys() | self._gate_skipped)
                    if any(s < seq for s in self._gate_undelivered):
                        return
                    owner_loop = self._gate_ready_loops.get(seq)
                    if (
                        owner_loop is not None
                        and owner_loop is not _running_loop()
                        and not owner_loop.is_closed()
                    ):
                        owner_loop.call_soon_threadsafe(self._drain_ordered)
                        return  # 归属 loop 关闭（进程收尾）才会在本地代投
                    skipped_event = self._gate_skipped.pop(seq, None)
                    event = self._gate_ready.pop(seq, None)
                    self._gate_ready_loops.pop(seq, None)
                    self._gate_delivered_through = seq
                if event is None:
                    if skipped_event is not None:
                        # 透明跳过的内存补插点（审查 F1）：前置 ①③ 已保证没有比它
                        # 更低的在途/未登记 seq，此刻 insort 位置必然正确。
                        self._insert_memory_ordered(skipped_event)
                    continue  # 跳过：推进水位 + 保序补插内存，不投递、不通知
                self._events.append(event)
                if seq in self._gate_board_pending:
                    self._gate_board_pending.discard(seq)
                    self._defer_board_projection(event)
                self._notify_listeners(event)
        finally:
            with self._gate_guard:
                self._gate_draining = False
                resolved = [
                    w
                    for w in self._gate_waiters
                    if w[0] <= self._gate_delivered_through
                ]
                if resolved:
                    resolved.sort(key=lambda w: w[0])  # 唤醒序 = seq 序（镜像序）
                    self._gate_waiters = [
                        w
                        for w in self._gate_waiters
                        if w[0] > self._gate_delivered_through
                    ]
                settled: list[tuple[asyncio.AbstractEventLoop, asyncio.Future]] = []
                if self._gate_is_idle_locked():
                    settled = self._gate_settled_waiters
                    self._gate_settled_waiters = []
            for _seq, loop, fut in resolved:
                if not loop.is_closed():
                    loop.call_soon_threadsafe(self._resolve_waiter, fut)
            for loop, fut in settled:
                if not loop.is_closed():
                    loop.call_soon_threadsafe(self._resolve_waiter, fut)

    def _gate_is_idle_locked(self) -> bool:
        """闸门是否已清空（在 `_gate_guard` 内调用；看板欠账不算——它是缓存）。"""
        return not (
            self._gate_inflight
            or self._gate_undelivered
            or self._gate_ready
            or self._gate_skipped
        )

    def _defer_board_projection(self, event: SessionEvent) -> None:
        """补一次看板欠账：跑在 loop 上就转交执行器，工作线程就地写（#1489）。"""
        snapshot = list(self._events)  # 跨线程纪律：在当前线程取快照
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self._update_board_projection(event, snapshot)
            return
        try:
            loop.run_in_executor(
                None,
                self._update_board_projection,
                event,
                snapshot,
            )
        except RuntimeError:
            # loop 在转交间隙关闭（进程收尾）：看板是 best-effort 派生索引，就地
            # 补一次，异常由 `_update_board_projection` 自己的观察者纪律兜底
            # （审查 F5：绝不让它逃出 drain 破坏 append 契约）。
            self._update_board_projection(event, snapshot)

    def _insert_memory_ordered(self, event: SessionEvent) -> None:
        """把「写成功但等待者已放弃」的事件保序补进内存投影（不通知）。

        只在 drain 的 skip 消费点调用（审查 F1）：前置 ①③ 已保证没有比该 seq
        更低的在途/未登记 seq，bisect 插入位置因此必然正确。**不得**在取消臂/
        完成回调里当场调用——更低 seq 仍可能在途，当场插入会把 `_events` 倒插
        （[0,2,1]，审查 F1 的复现形状）。
        """
        bisect.insort(self._events, event, key=lambda e: e.seq)

    @staticmethod
    def _resolve_waiter(fut: asyncio.Future) -> None:
        """drain（可能在任意线程）经 call_soon_threadsafe 回到 waiter 的 loop。"""
        if not fut.done():
            fut.set_result(None)

    async def wait_delivered(self, seq: int) -> None:
        """等待 seq 已被有序闸门投递（幂等：已投递立即返回）。#1547。

        镜像通道的序化锚点：`run_stream` 的 durable 镜像带 seq 进入
        `RunManager.enqueue_agent_event`，与 listener 通道共用同一把 seq 幂等门；
        镜像先于闸门投递入队会把仍在闸内的更低 seq 判成「重复」永久丢弃
        （本票的 live 丢帧形状）。调用方在入队镜像前等本方法返回，即保证
        「seq 入队 ⟹ 全部更低 seq 已入队」。
        """
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        waiter = (seq, loop, fut)
        with self._gate_guard:
            if seq <= self._gate_delivered_through:
                return
            self._gate_waiters.append(waiter)
        try:
            await fut
        except asyncio.CancelledError:
            # 删除必须在 `_gate_guard` 内：drain 的 finally 会在锁内重建等待者
            # 列表，无锁 remove 与之交错可能把**幸存**等待者一起丢掉（审查 P1）。
            with self._gate_guard, suppress(ValueError):
                self._gate_waiters.remove(waiter)
            raise

    async def wait_gate_settled(self) -> None:
        """等待闸门清空：无在途写者、无待投递/跳过 seq（#1547 审查 P1）。

        `mark()`/`since()` 的长度游标以内存投影为界；同步写者的投递可能被更低
        seq 的在途写者推迟到 `append` 返回**之后**。一次性快照消费者（如 runtime
        的 `_record_session_tool_deltas` 工具预算记账，没有 listener 通道补偿）
        先等本方法再取 `since()`，才不会漏掉刚写入但尚未放行的事件。

        已知残余（审查 F4，登记不扩张）：等待无超时——活性由在途写者的落盘
        收口保证（写失败/取消都即时撤闸，不留永久占位）；本方法只覆盖调用点
        之后的窗口，其它 `mark()/since()` 快照消费者（记忆形成等）仍可能读到
        晚到事件——它们没有预算记账那类「丢一条即账不平」的语义，按需各自接入。
        """
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        waiter = (loop, fut)
        with self._gate_guard:
            if self._gate_is_idle_locked():
                return
            self._gate_settled_waiters.append(waiter)
        try:
            await fut
        except asyncio.CancelledError:
            with self._gate_guard, suppress(ValueError):
                self._gate_settled_waiters.remove(waiter)
            raise

    def _notify_listeners(self, event: SessionEvent) -> SessionEvent:
        """逐条回调会话监听器（观察者，异常不破坏 append 契约）。"""
        for listener in self._listeners:
            try:
                listener(event)
            except Exception:
                logger.exception(
                    "session listener 回调失败（session=%s, event=%s）",
                    self.session_id,
                    event.type,
                )
        return event

    def _update_board_projection(
        self,
        event: SessionEvent,
        events: list[SessionEvent],
    ) -> None:
        """best-effort 更新看板投影（#1303 §3.2）。

        观察者纪律与 listener 相同：任何异常只记日志，绝不破坏 append 契约。
        ``adopt_history`` 走 ``notify=False`` 不逐条触发，完成后一次全量重放
        （避免 fork N 条历史时的 O(N²)）。

        ``events`` 是调用方**在循环线程上取的快照**（#1489）：本方法可能跑在工作
        线程（异步入口把这次 sidecar 重写+fsync 移出了循环），跨线程迭代活列表不安全。
        """
        try:
            get_board_projection_store(self._store).update_from_events(
                self.session_id,
                events,
                recompute=_board_projection_needs_recompute(event),
            )
        except Exception:
            logger.exception(
                "看板投影钩子失败（session=%s, event=%s）",
                self.session_id,
                event.type,
            )

    def adopt_history(
        self,
        events: list[SessionEvent],
        *,
        event_id_remap: Mapping[str, str | None] | None = None,
        fact_id_remap: Mapping[str, str | None] | None = None,
    ) -> list[SessionEvent]:
        """移植既有事件（fork seed 的唯一 owner，ADR-0017 决策 3）。

        重编 seq（本聚合按序分配，child 局部单调），逐字保留原 event_id /
        time / type / data / run_id / agent_id / step_id / source_event_ids，
        session_id 改写为本会话。类型必须在 EVENT_TYPES 词表内（流式专属拒绝）。
        """
        adopted: list[SessionEvent] = []
        adopted_by_id: dict[str, SessionEvent] = {
            event.event_id: event for event in self._events
        }
        event_remap = dict(event_id_remap or {})
        fact_remap = dict(fact_id_remap or {})
        for event in events:
            if event.type in STREAM_ONLY_TYPES:
                raise ValueError(
                    f"流式专属事件 '{event.type}' 不得移植进 durable log（invariant #4）"
                )
            if event.type not in EVENT_TYPES:
                raise ValueError(
                    f"未知事件类型 '{event.type}'：不在 EVENT_TYPES 词汇表中"
                )
            moved_data = event.data
            if event.type in {
                USER_MESSAGE,
                MESSAGE_QUEUED,
                STEER_REQUESTED,
                TASK_PROTECTED_FACT,
                TASK_FACT_REVOCATION,
            }:
                moved_data = dict(event.data)
            if event.type == TASK_FACT_REVOCATION:
                # #1190：撤销事件引用被撤销 fact——fork 时按 fact 重映射改写；
                # 目标 fact 在 fork 边界被丢弃（source 事件不在种子内）⇒ 撤销无
                # 对象，整条丢弃（child 里该 fact 也不存在，无注入可言）。
                revoke_id = moved_data.get("revoke_fact_id")
                if isinstance(revoke_id, str) and revoke_id in fact_remap:
                    remapped_revoke_id = fact_remap[revoke_id]
                    if remapped_revoke_id is None:
                        continue
                    moved_data["revoke_fact_id"] = remapped_revoke_id
            if event.type in {USER_MESSAGE, MESSAGE_QUEUED, STEER_REQUESTED}:
                revoke_id = moved_data.get("revoke_fact_id")
                if isinstance(revoke_id, str) and revoke_id in fact_remap:
                    remapped_revoke_id = fact_remap[revoke_id]
                    if remapped_revoke_id is None:
                        moved_data.pop("revoke_fact_id", None)
                    else:
                        moved_data["revoke_fact_id"] = remapped_revoke_id
                annotations = moved_data.get("protected_facts")
                if isinstance(annotations, list):
                    copied_annotations = []
                    for annotation in annotations:
                        if not isinstance(annotation, dict):
                            copied_annotations.append(annotation)
                            continue
                        copied_annotation = dict(annotation)
                        supersedes_id = annotation.get("supersedes_fact_id")
                        if (
                            isinstance(supersedes_id, str)
                            and supersedes_id in fact_remap
                        ):
                            remapped_supersedes_id = fact_remap[supersedes_id]
                            if remapped_supersedes_id is None:
                                copied_annotation.pop("supersedes_fact_id", None)
                            else:
                                copied_annotation["supersedes_fact_id"] = (
                                    remapped_supersedes_id
                                )
                        copied_annotations.append(copied_annotation)
                    moved_data["protected_facts"] = copied_annotations
            elif event.type == TASK_PROTECTED_FACT:
                source_id = moved_data.get("source_event_id")
                evidence_id = moved_data.get("evidence_event_id")
                supersedes_id = moved_data.get("supersedes_fact_id")
                if isinstance(source_id, str):
                    source_id = event_remap.get(source_id, source_id)
                if isinstance(evidence_id, str):
                    evidence_id = event_remap.get(evidence_id, evidence_id)
                if source_id is None or (
                    moved_data.get("evidence_event_id") is not None
                    and evidence_id is None
                ):
                    # A source event removed at the fork boundary cannot back a child fact.
                    continue
                if isinstance(supersedes_id, str) and supersedes_id in fact_remap:
                    supersedes_id = fact_remap[supersedes_id]
                    if supersedes_id is None:
                        moved_data.pop("supersedes_fact_id", None)
                refs_changed = (
                    source_id != event.data.get("source_event_id")
                    or evidence_id != event.data.get("evidence_event_id")
                    or supersedes_id != event.data.get("supersedes_fact_id")
                )
                if refs_changed:
                    moved_data = build_protected_fact_data(
                        self._events,
                        session_id=self.session_id,
                        fact_type=moved_data["fact_type"],
                        value=moved_data["value"],
                        source_event_id=source_id,
                        supersedes_fact_id=supersedes_id,
                        evidence_event_id=evidence_id,
                    )
                else:
                    for id_key, seq_key in (
                        ("source_event_id", "source_event_seq"),
                        ("evidence_event_id", "evidence_event_seq"),
                    ):
                        ref_id = moved_data.get(id_key)
                        if ref_id is None:
                            continue
                        referenced = adopted_by_id.get(ref_id)
                        if referenced is None:
                            raise ValueError(
                                "forked protected fact is missing its source event"
                            )
                        moved_data[seq_key] = referenced.seq
            moved_sources = (
                [
                    remapped
                    for ref in event.source_event_ids
                    if (remapped := event_remap.get(ref, ref)) is not None
                ]
                if event.source_event_ids is not None
                else None
            )
            if event.type == TASK_PROTECTED_FACT:
                moved_sources = [moved_data["source_event_id"]]
                if moved_data.get("evidence_event_id") is not None:
                    moved_sources.append(moved_data["evidence_event_id"])
            moved = replace(
                event,
                seq=self._next_seq,
                session_id=self.session_id,
                data=moved_data,
                source_event_ids=moved_sources,
            )
            moved = self._persist_event(moved, notify=False)
            adopted.append(moved)
            adopted_by_id[moved.event_id] = moved
            if event.type in {USER_MESSAGE, MESSAGE_QUEUED, STEER_REQUESTED}:
                old_annotations = event.data.get("protected_facts")
                new_annotations = moved.data.get("protected_facts")
                if isinstance(old_annotations, list) and isinstance(
                    new_annotations, list
                ):
                    for old_annotation, new_annotation in zip(
                        old_annotations, new_annotations, strict=False
                    ):
                        if not isinstance(old_annotation, dict) or not isinstance(
                            new_annotation, dict
                        ):
                            continue
                        old_identity = {
                            "fact_type": old_annotation.get("fact_type"),
                            "value": old_annotation.get("value"),
                            "source_event_id": event.event_id,
                        }
                        old_supersedes = old_annotation.get("supersedes_fact_id")
                        if isinstance(old_supersedes, str):
                            old_identity["supersedes_fact_id"] = old_supersedes
                        try:
                            new_fact = build_protected_fact_data(
                                self._events,
                                session_id=self.session_id,
                                fact_type=new_annotation["fact_type"],
                                value=new_annotation["value"],
                                source_event_id=event.event_id,
                                supersedes_fact_id=new_annotation.get(
                                    "supersedes_fact_id"
                                ),
                            )
                        except (KeyError, TypeError, ValueError):
                            continue
                        fact_remap[protected_fact_id(old_identity)] = new_fact[
                            "fact_id"
                        ]
            elif event.type == TASK_PROTECTED_FACT:
                old_fact_id = event.data.get("fact_id")
                new_fact_id = moved.data.get("fact_id")
                if isinstance(old_fact_id, str) and isinstance(new_fact_id, str):
                    fact_remap[old_fact_id] = new_fact_id
        # adopt 期间逐条 _persist_event(notify=False) 不触发投影；这里一次全量重放，
        # 避免 fork N 条历史时的 O(N²)（#1303 已知上限的例外）。
        try:
            get_board_projection_store(self._store).update_from_events(
                self.session_id,
                self._events,
                recompute=True,
            )
        except Exception:
            logger.exception(
                "adopt_history 后的看板投影重放失败（session=%s）",
                self.session_id,
            )
        return adopted

    def derive_messages(self) -> list[AnyMessage]:
        """从已加载事件投影出模型可见 messages（委托纯函数）。"""
        return derive_messages(self._events)

    # ── step_id 基数（前端 turn 定位键的服务端镜像）──

    @property
    def max_step_id(self) -> int:
        """已出现过的最大 step_id（无则 0）。

        事件信封的 step_id 是前端 turn 定位键（`projection.resolveStep` →
        `withTurnAt`），必须 session 级单调；续聊 run 以它为步号基数接续，
        否则第二轮 run 再从 1 编号会与首轮冲突（前端把第二轮模型输出折叠进
        首轮 turn）。计算与消费见 `AgentRuntime._drive` 的 `step_base`。
        """
        return max(
            (e.step_id for e in self._events if e.step_id is not None),
            default=0,
        )

    @property
    def user_turn_count(self) -> int:
        """真实用户发言轮数 = 前端已为「用户消息」开出的 turn 数。

        runtime 注入的纠正消息（`data.injected_by`，同错熔断软触发）不算：
        它带 step_id 落在当前轮内，不新开 turn。
        """
        return sum(
            1
            for e in self._events
            if e.type == USER_MESSAGE and not e.data.get("injected_by")
        )

    # ── Run 生命周期 ──

    def begin_run(
        self,
        *,
        agent_id: str = "default",
        agent_profile: str = "main",
        model: str | None = None,
        reasoning_effort: str | None = None,
        budget: dict[str, Any] | None = None,
    ) -> tuple[str, int]:
        """生成 run_id、append run/started、返回 ``(run_id, turn_index)``。

        ``turn_index`` = 该 session 里第几个 run（1-based），供 Langfuse
        trace metadata 标记「这是第 N 轮」（T9 #139）。

        ``agent_profile``（#198）：生效档位**总是**写进 run/started.data——
        未指定时为 "main"。"缺字段"正是此前不可回溯的根因（真机会话里 13 个
        run 全部查不到档位，只能靠工具集形状反推）；旧数据无该字段时前端按
        「档位未知」降级，不报错。

        ``model``（#226）：本轮**请求侧**的模型标识（= 真正发往 provider 的
        ``model`` 字段，装配层给的就是 `ModelConfig.model_name`）。`None` 时
        **不落该键**（本方法只写调用方给的事实，不替它编默认值；Runtime 路径
        总传真值）。取值口径、与 ``model/completed`` 回显侧的对照、以及为什么
        不能放在 ``model/started``（流式专属、永不持久化），见
        ``docs/adr/0034-request-side-model-identity.md``。

        ``reasoning_effort``（`#1093`）：本轮**请求侧**选中的推理档位名（harness
        语义档位，如 "minimal"/"standard"/"deep"；不是 provider wire 值）。`None`
        时**不落该键**——与 ``model``/``budget`` 同一「不可得 ≠ 默认值」口径
        （`11 §6.1`：账目缺失是 unavailable，不是默认档；web 的「默认」位置正是
        「请求省略该字段」）。机制来源 LangSmith ``ls_invocation_params``（run
        metadata 记录每次调用的 provider 参数供事后按配置过滤/对比），落点抄仓内
        #198 ``agent_profile`` 写进 ``run/started.data`` 的加法模式。

        ``budget``（`#312` T4）：本逻辑 run 的 **run 作用域** 预算快照（ceiling 等）。
        `None` **不落键**——"没有配 ceiling"是"没有这条事实"，不是"ceiling=0"
        （`11 §6.1`：不可得 ≠ 0）。`run/paused` 的 limits 快照由它重建，
        所以配了 ceiling 时必须落（否则重启后投影不出配置值）。
        """
        run_id = str(uuid4())
        turn_index = sum(1 for e in self._events if e.type == RUN_STARTED) + 1
        data: dict[str, Any] = {
            "turn_index": turn_index,
            "agent_profile": agent_profile,
        }
        if model is not None:
            data["model"] = model
        if reasoning_effort is not None:
            data["reasoning_effort"] = reasoning_effort
        if budget is not None:
            data["budget"] = budget
        self.append(RUN_STARTED, data, run_id=run_id, agent_id=agent_id)
        return run_id, turn_index

    def end_run(
        self,
        run_id: str,
        *,
        status: str,
        final_text: str = "",
        usage_total: dict[str, int | None] | None = None,
        cost_usd: Decimal | str | None = None,
        trace_id: str | None = None,
        trace_url: str | None = None,
        reason: str | None = None,
        message: str | None = None,
        primary_error: str | None = None,
        fallback_error: str | None = None,
        http_status: int | None = None,
    ) -> SessionEvent:
        """append run/completed 或 run/failed，返回该事件（Phase 9 让流式层镜像它）。

        usage_total / cost_usd / trace_id / trace_url 是前端 Gap 1/2 契约 +
        trace_url 契约（BACKEND_GAP_PROMPT.md / BACKEND_PROMPT_TRACE_URL.md）：
        只扩展 data，不改既有语义；None 表示"未追踪"，前端据此降级显示
        （trace_url=null → 渲染纯 mono code 的 trace_id），不伪造占位字符串。
        trace_id 与 trace_url 并列保留（前者机器可读，后者人类可点击，不互替）。
        对称终态：completed 与 failed 都下发 trace_id / trace_url——失败 run 在
        Langfuse 也有可见 trace，跳转有排查价值。reason 仅 failed 语义使用
        （如 identical_tool_failure_loop），落事件 data——消费者可区分失败原因
        （取消路径的 reason=cancelled 同款先例）。message 同仅 failed：固定可读文案
        （已分类故障如内容审查拒绝；未分类异常由运行时侧拼一句带类型名的兜底），
        与上下文超限路径直接 append 的 reason+message 形状一致；只接受调用方
        **自己拼**的常量/模板，绝不透传 provider 回显原文
        （口径见 docs/adr/0033-run-failure-attribution-surface.md §2.1）。

        **终态保底（#1061，A 后是通用路径的子集）**：本方法是唯一以 `terminal=True`
        进漏斗的写者。撞号时 store 在**锁内**改号再落盘（`allocate`，见
        `JsonlSessionStore.append_event`）——持续占位的写者没有可插入的窗口。终态是
        「这个 run 结束了吗」的唯一 durable 事实，放它不落就等于 run 在盘上永远悬空。
        这**不是**无限重试：锁内改号只有一次，store 若仍判定不可写（如已硬删）异常照样
        抛出，绝不假成功。
        """
        # 终态幂等（#1103，CAS）：该 run 已有终态 ⇒ 拒写并**返回既存的那一枚**。
        # 同一 run 的两次 `end_run`（重入的失败臂 / 取消臂）此前会落下
        # `run/completed` 与 `run/failed` 两条，durable 真相层自相矛盾——按
        # `RUN_TERMINAL_TYPES` 投影终态的下游会看到**自相矛盾的终态集合**。
        # 逐下游实读（口径不一，不得写成「都会看到两条」；本清单非穷举）：
        # `progress.py` 两条都发（`run_completed` 里程碑 + `run_failed` 失败项）；
        # `interrupt.py` / `task.py` 是 pop / discard（第二条 no-op）；
        # `multiagent/provider.py` 取 `next(reversed(...))`、`store.py` 的
        # `_terminal_trace_field` 只看末条事件（两者都 last-wins，取到哪条看落盘顺序）。
        # ⚠ `fork.py` 是**后果最重**的一个：`_run_open_delta` 是**累计计数器**
        # （`run/started` +1、终态 −1），同一 run 落两条终态 ⇒ 净 −1 ⇒
        # `find_fork_boundaries` 丢掉该点之后的锚点、`_validate_run_complete`
        # 直接抛「负计数」（本机实测：单终态 `[1, 4]`，补一条多余的 `run/failed`
        # 后变 `[1]` + `ForkBoundaryError`）。
        # 判据与 `run/resumed` 的 `expected_version` 同族：写前核对
        # 当前状态，不符即拒（EventStoreDB 的 expected-revision /
        # Marten 的 `FetchForWriting` 同型）。⚠ 「同族」只指**判据形状**
        # （写前核对当前状态、不符即拒），**不指**持久层保证：
        # `run/resumed` 的 `expected_version` 比对的是**从事件流重放的**
        # `PausedRun.version`（`run_budget.py` 的 `validate_resume`），
        # 本 CAS 读的却是本实例的**内存投影** `self._events`——而 `end_run`
        # 的 CAS 与写盘之间不持任何锁，投影更新（#1547 起在 `_drain_ordered`）
        # 在 store 写锁**之外** ⇒ 只覆盖「同实例内、CAS 与写盘之间无交错」的
        # 形状；#1547 起扫描面已含闸内 `_gate_ready` 暂存与 `_gate_skipped`
        # （取消等待者，事件对象随跳过登记保留，审查 F1）的终态；同实例多线程并发、或另一
        # 实例（各自 `_events` 都没有终态）仍可各落一条——本条注释即其登记处；
        # 根治要把判据移进 store 写锁，不属本票范围。
        # 生产侧 `AgentRuntime._terminal_written` 只护住
        # 它自己的调用链，不是 Session 层的契约——真相层的守卫要落在真相层。
        # 返回既存而非抛错：同一 run 重复收口是**幂等**语义，不是调用方犯错。
        # #1547：投递闸门会把刚写的终态暂存在 `_gate_ready`（更低 seq 在途时
        # 晚于 `append` 返回才进 `_events`）或 `_gate_skipped`（取消等待者）里，
        # CAS 的扫描面必须把这部分也算上，否则背靠背的两次 `end_run` 会在第一
        # 次尚未投递时各落一条终态。
        with self._gate_guard:
            existing = next(
                (
                    e
                    for e in self._events
                    if e.run_id == run_id and e.type in RUN_TERMINAL_TYPES
                ),
                None,
            ) or next(
                (
                    e
                    for e in (*self._gate_ready.values(), *self._gate_skipped.values())
                    if e.run_id == run_id and e.type in RUN_TERMINAL_TYPES
                ),
                None,
            )
        if existing is not None:
            return existing
        event_type = RUN_COMPLETED if status == "completed" else RUN_FAILED
        data: dict = {"final_text": final_text} if final_text else {}
        if usage_total:
            data["usage_total"] = usage_total
        if status == "completed":
            # 成本以十进制**字符串**入事件（`#313`）：`Decimal` 进 `json.dumps`
            # （`session/store.py` 的裸 dumps）会炸，而 `float()` 会引入与 wire
            # 不等价的二进制近似（`11 §6.1`：二进制浮点相等不是契约）。`None`
            # 原样落（不可得 ≠ 0：客户端据此显示"未跟踪"，不是"零成本"）。
            data["cost_usd"] = (
                format(cost_usd, "f") if isinstance(cost_usd, Decimal) else cost_usd
            )
        data["trace_id"] = trace_id
        data["trace_url"] = trace_url
        if status != "completed" and reason:
            data["reason"] = reason
        if status != "completed" and message:
            data["message"] = message
        # #551 M10-8：同一决策里 primary 与 fallback 都失败时保留两级错误**类型名**
        # （首因不再只存在于 model/fallback 事件里）。只带类型名、不带正文——
        # 与 FallbackTransition.reason 同一脱敏边界。仅在给值时才落键。
        if status != "completed" and primary_error:
            data["primary_error"] = primary_error
        if status != "completed" and fallback_error:
            data["fallback_error"] = fallback_error
        # #996：未分类 provider 失败的 HTTP 状态码（整数，取自异常结构属性、非回显
        # 正文；ADR-0033 §2.4）。仅在给值时才落键。
        if status != "completed" and http_status is not None:
            data["http_status"] = http_status
        return self.append(
            event_type,
            data,
            run_id=run_id,
            terminal=True,
        )
