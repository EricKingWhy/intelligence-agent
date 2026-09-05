"""Session：Agent 交互历史的领域聚合根。

持有 session_id 与已加载事件列表（内存缓存），对外提供：
    - start() / resume()  构造入口
    - append()            追加事件（分配 seq + 同步写 JSONL + 更新内存）
    - derive_messages()   从事件投影模型可见 messages
    - begin_run() / end_run()  标记 Run 边界

SessionStore 负责 IO（薄层），Session 负责业务状态（seq 分配、dangling 修复）。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING
from uuid import uuid4

from langchain_core.messages import AnyMessage

from agent_harness.sandbox.base import Sandbox

if TYPE_CHECKING:
    from agent_harness.sandbox.registry import WorkspaceRegistry

from agent_harness.session.derive import (
    DANGLING_TOOL_CONTENT,
    derive_messages,
    detect_dangling,
)
from agent_harness.session.event import (
    EVENT_TYPES,
    RUN_COMPLETED,
    RUN_FAILED,
    RUN_STARTED,
    SESSION_RESUMED,
    SESSION_STARTED,
    STREAM_ONLY_TYPES,
    TOOL_RESULT,
    SessionEvent,
)
from agent_harness.session.store import JsonlSessionStore

logger = logging.getLogger("agent_harness.session")


class Session:
    """Agent 会话聚合根——Runtime 与外部世界的单一交互入口。"""

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
        self._sandbox: Sandbox | None = sandbox

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

    # ── 构造入口 ──

    @classmethod
    def start(
        cls,
        store: JsonlSessionStore,
        *,
        agent_id: str = "default",
        session_id: str | None = None,
        workspace_registry: WorkspaceRegistry | None = None,
    ) -> Session:
        """新建 Session：生成 id、创建 JSONL、append session/started。

        提供 workspace_registry 时，自动创建/绑定 Sandbox 实例到 session.sandbox。
        session_id 允许调用方预生成（web 层"先组装 runtime 后建 Session"的顺序
        需要：_build_runtime 要以 session_id 装配 S3 artifact 命名空间，组装失败
        时不能留下任何已落盘的孤儿 session——R6-6）。
        """
        session_id = session_id or str(uuid4())
        sandbox = None
        if workspace_registry is not None:
            sandbox = workspace_registry.create(session_id)
        session = cls(session_id, store, sandbox=sandbox)
        session.append(SESSION_STARTED, {}, agent_id=agent_id)
        return session

    @classmethod
    def resume(
        cls,
        store: JsonlSessionStore,
        session_id: str,
        *,
        workspace_registry: WorkspaceRegistry | None = None,
    ) -> Session:
        """加载已有 Session：读 JSONL、校验 seq、修复 dangling、append session/resumed。

        提供 workspace_registry 时，自动查回/恢复 Sandbox 实例到 session.sandbox。
        """
        events = store.read_events(session_id)
        if not events:
            raise ValueError(f"Session '{session_id}' 不存在或事件日志为空")

        sandbox = None
        if workspace_registry is not None:
            sandbox = workspace_registry.get(session_id)

        session = cls(session_id, store, events, sandbox=sandbox)

        # 校验 seq 严格递增（不容忍重复或回退）；计数器据此在构造时取 max+1
        seen_seqs: set[int] = set()
        prev_seq = -1
        for event in session._events:
            if event.seq in seen_seqs:
                raise ValueError(
                    f"Session '{session_id}' 事件 seq 重复: {event.seq}"
                )
            if event.seq <= prev_seq:
                raise ValueError(
                    f"Session '{session_id}' 事件 seq 回退: {event.seq}（前一条: {prev_seq}）"
                )
            seen_seqs.add(event.seq)
            prev_seq = event.seq

        # 修复 dangling tool_call：为每个未解决的 tool_call 追加合成 tool/result
        dangling_ids = detect_dangling(session._events)
        for tc_id in dangling_ids:
            logger.warning(
                "Resume 修复 dangling tool_call_id=%s，追加合成 tool/result", tc_id
            )
            session.append(
                TOOL_RESULT,
                {"tool_call_id": tc_id, "content": DANGLING_TOOL_CONTENT},
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
        source_event_ids: list[str] | None = None,
        _mark_dangling: bool = False,
    ) -> SessionEvent:
        """追加一条事件：分配 seq、同步写 JSONL、更新内存。

        _mark_dangling 仅内部使用——在 data 中写入 dangling=true 标记。
        事件类型必须在 EVENT_TYPES 词汇表内；STREAM_ONLY_TYPES（流式专属信号）
        拒绝持久化（invariant #4：Event ≠ Diagnostic Log）。
        """
        # 词汇表校验：先拒绝再写盘，杜绝未知/流式事件悄悄污染 durable log
        if event_type in STREAM_ONLY_TYPES:
            raise ValueError(
                f"流式专属事件 '{event_type}' 不得通过 Session.append 持久化"
                "（仅作为 run_stream() 的 AgentEvent 输出）"
            )
        if event_type not in EVENT_TYPES:
            raise ValueError(f"未知事件类型 '{event_type}'：不在 EVENT_TYPES 词汇表中")
        seq = self._next_seq
        event = SessionEvent(
            seq=seq,
            type=event_type,
            session_id=self.session_id,
            run_id=run_id,
            agent_id=agent_id,
            step_id=step_id,
            data={**data, "dangling": True} if _mark_dangling else data,
            source_event_ids=source_event_ids,
        )
        self._store.append_event(self.session_id, event)
        self._events.append(event)
        # 写盘成功后才推进计数器——失败不消耗 seq
        self._next_seq += 1
        return event

    def derive_messages(self) -> list[AnyMessage]:
        """从已加载事件投影出模型可见 messages（委托纯函数）。"""
        return derive_messages(self._events)

    # ── Run 生命周期 ──

    def begin_run(self, *, agent_id: str = "default") -> str:
        """生成 run_id、append run/started、返回 run_id。"""
        run_id = str(uuid4())
        self.append(RUN_STARTED, {}, run_id=run_id, agent_id=agent_id)
        return run_id

    def end_run(
        self,
        run_id: str,
        *,
        status: str,
        final_text: str = "",
        usage_total: dict | None = None,
        cost_usd: float | None = None,
        trace_id: str | None = None,
    ) -> SessionEvent:
        """append run/completed 或 run/failed，返回该事件（Phase 9 让流式层镜像它）。

        usage_total / cost_usd / trace_id 是前端 Gap 1/2 契约（BACKEND_GAP_PROMPT.md）：
        只扩展 data，不改既有语义；None 表示"未计算"，绝不伪造 0。
        """
        event_type = RUN_COMPLETED if status == "completed" else RUN_FAILED
        data: dict = {"final_text": final_text} if final_text else {}
        if usage_total:
            data["usage_total"] = usage_total
        if status == "completed":
            data["cost_usd"] = cost_usd
            data["trace_id"] = trace_id
        return self.append(
            event_type,
            data,
            run_id=run_id,
        )
