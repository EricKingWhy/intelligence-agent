"""InProcessSubagentProvider：spawn 执行器的 V1 唯一实现（ADR-0015 决策 3）。

SubagentProvider 是「spawn 执行器」的可替换接缝——V1 in-process：child 就是
同一进程内的 AgentRuntime（不变量 #19：SubAgent 复用同一 Agent Loop）；
subprocess/remote(ACP) 未来换实现即可。

依赖分两段注入（构造期零依赖，激活后可用）：
- 构造：profiles（默认三内置）；
- activate()：factory / source_registry / session_store / workspace_registry /
  parent_session_id——由 build_runtime 在模型链与 registry 就绪后调用。
未激活时执行 → 明确失败（绝不静默伪装）。

child 的边界（都在 activate 注入的 factory/registry 里固化）：
- registry 经 AgentFactory 过滤，收窄到「本层实有工具 ∩ 剩余深度允许的可授予
  集合」（#286：remaining=0 时 child 拿不到 delegate，且申请即显式拒绝）；
- sandbox 与父共享同一实例（spec §9：coding 改动 review 可见）；
- Session 独立 JSONL（lineage 由父流 delegation 事件的 child_session_id 引用）。
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import nullcontext
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable
from uuid import uuid4

from agent_harness.agent.factory import AgentFactory
from agent_harness.agent.profiles import BUILTIN_PROFILES, AgentSpec
from agent_harness.agent.runtime import AgentRunResult
from agent_harness.multiagent.depth import (
    SpawnScope,
    bind_scope,
    bind_tree_id,
    child_allowance,
    current_scope,
    current_tree_id,
    grantable_names,
)
from agent_harness.sandbox import WorkspaceRegistry
from agent_harness.session import (
    SESSION_STARTED,
    Session,
    cwd_event_data,
    run_context_var,
    session_cwd,
)
from agent_harness.session.event import RUN_INTERRUPTED, RUN_TERMINAL_TYPES
from agent_harness.session.store import JsonlSessionStore
from agent_harness.storage.delegation_tree import (
    DelegationReservation,
    InMemoryDelegationTreeLedger,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SubAgentResult:
    """子代理的结构化产物（ADR-0015 决策 12）——绝不倾倒完整历史。

    V1 字段集：agent_id / status / summary / child_session_id（lineage 挂点）。
    artifacts / citations / changed_files / unresolved 由 T4 按「真实来源」
    逐字段补齐；tests DEFER。
    """

    agent_id: str
    status: str
    summary: str
    child_session_id: str = ""
    # 以下字段全部「真实来源收集，无则省略」（T4, #85；tests 字段 DEFER）
    citations: list[str] = field(default_factory=list)
    artifacts: list[str] = field(default_factory=list)
    changed_files: list[str] = field(default_factory=list)
    unresolved: list[str] = field(default_factory=list)


_WRITE_TOOL_NAMES = frozenset({"write", "edit", "apply_patch"})
_SEARCH_TOOL_NAMES = frozenset({"retrieve_knowledge", "web_search"})
_UNRESOLVED_MARKERS = ("未解决事项", "未解决")


def collect_result_fields(
    events: list, summary: str = "",
) -> dict[str, list[str]]:
    """从 child 会话事件与最终回答收集真实结果字段（绝不伪造：无则省略）。

    - citations：检索类 tool/result 的 payload 命中（kb:/web: citation）；
    - artifacts：artifact/created 事件的 artifact_id；
    - changed_files：write/edit/apply_patch 的 path 参数推导（去重保序）；
    - unresolved：最终回答「未解决」自报段的轻解析（缺失 = 空数组）。
    """
    import json as _json

    call_names: dict[str, str] = {}
    citations: list[str] = []
    changed: list[str] = []
    artifacts: list[str] = []

    for event in events:
        if event.type == "tool/call":
            call_names[event.data.get("tool_call_id", "")] = event.data.get("tool_name", "")
            tool_name = event.data.get("tool_name", "")
            if tool_name in _WRITE_TOOL_NAMES:
                path = (event.data.get("args") or {}).get("path")
                if path and path not in changed:
                    changed.append(path)
        elif event.type == "tool/result":
            call_id = event.data.get("tool_call_id", "")
            tool_name = call_names.get(call_id, event.data.get("tool_name", ""))
            if tool_name in _SEARCH_TOOL_NAMES:
                try:
                    outer = _json.loads(event.data.get("content", "{}"))
                    output = _json.loads(outer.get("data", {}).get("output", "{}"))
                    for hit in output.get("hits", []):
                        citation = hit.get("citation")
                        if citation and citation not in citations:
                            citations.append(citation)
                except (ValueError, AttributeError):
                    pass  # 非法 payload 只损失该条 citation，不 brick 收集
        elif event.type in ("artifact/created", "artifact/externalized"):
            artifact_id = event.data.get("artifact_id")
            if artifact_id and artifact_id not in artifacts:
                artifacts.append(artifact_id)

    unresolved: list[str] = []
    section_active = False
    for line in (summary or "").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if any(stripped.startswith(m) for m in _UNRESOLVED_MARKERS):
            # 「未解决事项：」标题行：段开始；同行内联条目也算
            section_active = True
            inline = stripped.split("：", 1)[-1].strip() if "：" in stripped else ""
            if inline:
                unresolved.append(inline.lstrip("-•* ").strip())
            continue
        if section_active and stripped.startswith(("-", "•", "*")):
            unresolved.append(stripped.lstrip("-•* ").strip())
        elif section_active:
            section_active = False  # 非列表正文 = 段落结束

    # unresolved 恒在（child system prompt 强制自报；空数组 = 自报无未解决）。
    fields: dict[str, list[str]] = {"unresolved": unresolved}
    if citations:
        fields["citations"] = citations
    if artifacts:
        fields["artifacts"] = artifacts
    if changed:
        fields["changed_files"] = changed
    return fields


@runtime_checkable
class SubagentProvider(Protocol):
    """spawn 执行器接缝：换 subprocess/remote 实现 = 换本 Protocol 的实现。"""

    async def run(self, *, target: str, task: str,
                  constraints: list[str]) -> SubAgentResult: ...

    def profile(self, target: str) -> AgentSpec: ...


class InProcessSubagentProvider:
    """in-process spawn：child = 独立 Session + Factory 构造的 AgentRuntime。"""

    def __init__(self, profiles: dict[str, AgentSpec] | None = None) -> None:
        self._profiles = dict(profiles) if profiles is not None else dict(BUILTIN_PROFILES)
        self._activated = False
        self._factory: AgentFactory | None = None
        self._source_registry = None
        # 根委派配额（#286）：activate 用根 profile 的 max_depth 覆盖。
        self._max_depth = 1
        self._root_max_depth = 1
        self._max_delegations = 8
        self._delegation_ledger = InMemoryDelegationTreeLedger()
        self._session_tree_id: str | None = None
        self._root_session_id: str | None = None
        self._resume_tree_id: str | None = None
        self._resume_bound_run_id: str | None = None
        self._tree_metadata_error = False
        self._session_store: JsonlSessionStore | None = None
        self._workspace_registry: WorkspaceRegistry | None = None
        self._parent_session_id: str | None = None
        # 大产物治理（#86）：summary 超限且未配 overflow store 时截断 +
        # child session 指针（全文在 child JSONL，不丢数据）；store 已配时
        # 全文交父侧 artifact 管线（tool result 携带 artifact_ref）。
        self._summary_limit = 8192
        self._overflow_configured = False
        # 并发 child 封顶（#90, ADR-0015 决策 13）：超出排队不失败。
        self._active_children: asyncio.Semaphore | None = None
        self._max_active_children = 0
        # 子会话观测挂点（未来 Agent Hub / lineage 消费；测试断言共享 sandbox）。
        self.last_child_sessions: list[Session] = []
        # 父 cwd 缓存（WS-1 #151）：写后不可变 → 同一个父只读一次，不重读父 JSONL。
        # 用独立的 loaded 标志而不是 `None` 哨兵：`None`（父确实没有 cwd）是合法
        # 缓存值，读失败则**不缓存**（瞬时 I/O 故障不该把子会话永久钉成未分组）。
        self._parent_cwd_cache: str | None = None
        self._parent_cwd_loaded = False

    def new_runtime_instance(self) -> InProcessSubagentProvider:
        """Create an unactivated provider for one root Runtime.

        Capability wiring is cached process-wide, but session/store/workspace
        bindings are not: each Runtime must own its own mutable provider state.
        Descendant Runtimes keep using this instance through the inherited registry.
        """
        return InProcessSubagentProvider(profiles=self._profiles)

    def activate(
        self,
        *,
        factory: AgentFactory,
        source_registry,
        session_store: JsonlSessionStore,
        workspace_registry: WorkspaceRegistry,
        parent_session_id: str,
        summary_limit: int = 8192,
        overflow_configured: bool = False,
        max_active_children: int = 4,
        max_depth: int = 1,
        max_delegations: int = 8,
        delegation_ledger=None,
    ) -> None:
        """build_runtime 在模型链与 registry 就绪后调用（幂等：重复激活覆盖）。

        `max_depth` = **根配额**（#286 冻结语义 1）：root depth=0，所以它同时就是
        「从根还能往下几层」。来源是根 profile 的 `AgentSpec.max_depth`（装配点传），
        不是 child 的自述——child 抬不动它。默认 1 = V1 出厂语义（ADR-0015 决策 7）：
        忘了传只会更保守，不会更宽；传 0 会被 `child_allowance` 折成「根自己也派不
        出去」，同样 fail-closed，不炸。
        """
        self._factory = factory
        self._source_registry = source_registry
        self._max_depth = max_depth
        self._root_max_depth = max_depth
        self._max_delegations = max_delegations
        self._delegation_ledger = delegation_ledger or InMemoryDelegationTreeLedger()
        self._session_store = session_store
        self._workspace_registry = workspace_registry
        self._parent_session_id = parent_session_id
        self._root_session_id = parent_session_id
        self._session_tree_id = None
        self._resume_tree_id = None
        self._resume_bound_run_id = None
        self._tree_metadata_error = False
        self._parent_cwd_cache = None
        self._parent_cwd_loaded = False
        try:
            events = session_store.read_events(parent_session_id)
            self._parent_cwd_cache = session_cwd(events)
            self._parent_cwd_loaded = True
            started = next((event for event in events if event.type == SESSION_STARTED), None)
            if started is not None:
                tree_id = started.data.get("delegation_tree_id")
                root_id = started.data.get("delegation_root_session_id")
                remaining = started.data.get("delegation_remaining_depth")
                root_max_depth = started.data.get("delegation_root_max_depth")
                root_max_delegations = started.data.get("delegation_root_max_delegations")
                if isinstance(tree_id, str) and tree_id:
                    self._session_tree_id = tree_id
                if isinstance(root_id, str) and root_id:
                    self._root_session_id = root_id
                if isinstance(remaining, int) and not isinstance(remaining, bool):
                    self._max_depth = min(self._max_depth, remaining)
                if isinstance(root_max_depth, int) and not isinstance(root_max_depth, bool):
                    self._root_max_depth = root_max_depth
                if (isinstance(root_max_delegations, int)
                        and not isinstance(root_max_delegations, bool)
                        and root_max_delegations >= 0):
                    self._max_delegations = root_max_delegations
                if self._session_tree_id and not (
                    isinstance(root_id, str) and root_id
                    and isinstance(remaining, int) and not isinstance(remaining, bool)
                    and remaining >= 0
                    and isinstance(root_max_depth, int)
                    and not isinstance(root_max_depth, bool)
                    and root_max_depth >= remaining
                    and isinstance(root_max_delegations, int)
                    and not isinstance(root_max_delegations, bool)
                    and root_max_delegations >= 0
                ):
                    self._tree_metadata_error = True
                if (not self._session_tree_id
                        and started.agent_id not in {None, "default", "main"}):
                    # A child session without the metadata needed to recover its
                    # parent tree must not silently receive a fresh depth/budget.
                    self._tree_metadata_error = True
            if self._session_tree_id is None:
                last_terminal = next(
                    (event for event in reversed(events)
                     if event.type in RUN_TERMINAL_TYPES and event.run_id),
                    None,
                )
                if last_terminal is not None and last_terminal.type == RUN_INTERRUPTED:
                    self._resume_tree_id = last_terminal.run_id
        except Exception:
            # A read failure must not silently grant a fresh tree budget or depth.
            self._tree_metadata_error = True
            logger.warning("读取委派树恢复元数据失败", exc_info=True)
        self._summary_limit = summary_limit
        self._overflow_configured = overflow_configured
        self._max_active_children = max_active_children
        self._active_children = (
            asyncio.Semaphore(max_active_children) if max_active_children > 0 else None
        )
        self._activated = True

    def _parent_cwd(self) -> str | None:
        """父会话的会话侧 cwd 锚（WS-1 #151）；读不到 → None（子会话按未分组处理）。

        父的 cwd 写后不可变，所以同一个父只读一次并缓存——一次委派不该为此重读
        整份父 JSONL。读是**同步**的，与 `session/fork.py` 读父 JSONL 同一形态
        （本仓 `read_events` 就在事件循环里直接调用）；这里刻意不引第二个挂起点：
        并发 spawn 的子会话若在拿到第一条模型消息前多一次 `await`，彼此之间的
        调度顺序就会变——那是"个别 child 失败不影响同批其他 child"这类既有断言的
        隐含前提。只读，**不** `Session.resume`（那会往父日志追加
        `session/resumed`）；失败只记 warning，归属元数据缺失不该拖垮委派。
        """
        if not self._parent_cwd_loaded:
            value, failed = self._read_parent_cwd()
            if not failed:
                self._parent_cwd_cache = value
                self._parent_cwd_loaded = True
            return value
        return self._parent_cwd_cache

    def _read_parent_cwd(self) -> tuple[str | None, bool]:
        """→ (cwd, 是否读取失败)。失败不缓存：换一次 spawn 再试，别把子会话钉死。"""
        if self._session_store is None or self._parent_session_id is None:
            return None, False
        try:
            events = self._session_store.read_events(self._parent_session_id)
            return session_cwd(events), False
        except Exception:  # 归属元数据缺失不该拖垮委派
            logger.warning("读取父会话 cwd 失败，子会话按未分组处理", exc_info=True)
            return None, True

    def profile(self, target: str) -> AgentSpec:
        try:
            return self._profiles[target]
        except KeyError:
            raise ValueError(
                f"未知 profile '{target}'（可选：{sorted(self._profiles)}）"
            ) from None

    def tree_id(self) -> str:
        """Return the inherited tree identity or establish one from this root run."""
        inherited = current_tree_id()
        if inherited:
            return inherited
        if self._session_tree_id:
            return self._session_tree_id
        run_id = run_context_var.get()
        if self._resume_tree_id and run_id:
            if self._resume_bound_run_id is None:
                self._resume_bound_run_id = run_id
            if self._resume_bound_run_id == run_id:
                return self._resume_tree_id
        return run_id or self._parent_session_id or "__no_run__"

    async def reserve_delegation(
        self, tree_id: str, *, max_delegations: int,
    ) -> DelegationReservation:
        if self._tree_metadata_error:
            raise RuntimeError("委派树恢复元数据不可用；拒绝启动子代理")
        return await self._delegation_ledger.reserve(
            tree_id, root_session_id=self._root_session_id or self._parent_session_id or tree_id,
            max_delegations=min(max_delegations, self._max_delegations),
            max_depth=self._root_max_depth,
        )

    async def observe_delegation_result(
        self, tree_id: str, fingerprint: str, *, ok: bool,
    ):
        return await self._delegation_ledger.observe_result(
            tree_id, fingerprint, ok=ok,
        )

    async def run(self, *, target: str, task: str,
                  constraints: list[str]) -> SubAgentResult:
        if not self._activated:
            raise RuntimeError(
                "multiagent provider 未激活（build_runtime 未完成依赖注入），"
                "delegate 不可用"
            )
        spec = self.profile(target)
        full_task = task
        if constraints:
            full_task = task + "\n\n约束：\n" + "\n".join(f"- {c}" for c in constraints)

        # 当前层的配额：根（还没有人往下走过）= 装配点的 max_depth；否则 = 本层
        # 被赋予的剩余额度（#286：child 抬不动它）。
        scope = current_scope() or self._root_scope()
        tree_id = self.tree_id()
        # 并发 child 封顶（#90）：超出排队等待，不失败不丢弃。
        async with (self._active_children or nullcontext(None)):
            return await self._run_child(spec, full_task, scope, tree_id)

    def _root_scope(self) -> SpawnScope:
        """根配额：装配点给的 `max_depth` + 装配点给的 registry。"""
        return SpawnScope(registry=self._source_registry, remaining=self._max_depth)

    async def _run_child(
        self, spec: AgentSpec, full_task: str, scope: SpawnScope, tree_id: str,
    ) -> SubAgentResult:
        # 配额与可授予集合在**开 session 之前**算：越权/超深度的 spawn 要显式
        # 失败，且不该在 session 列表里留下一个只有 session/started 的幽灵子会话。
        # #286 把这条路径从「罕见」（只有越权申请才走）变成「模型每次撞深度上限
        # 都会走」，所以顺序本身现在是可观测性的一部分。
        allowance = child_allowance(scope, spec)
        child_runtime = self._factory.create(
            spec,
            source_registry=scope.registry,
            grantable=grantable_names(scope, allowance),
        )
        # child workspace = 父的同一 canonical owner（spec §9：coding 的改动
        # review 直接可见）；先 durable bind，保证 child Session 一旦落盘，恢复
        # 就能按 child_session_id 重新解析该 workspace。
        child_session_id = str(uuid4())
        child_sandbox = self._workspace_registry.bind_alias(
            child_session_id, self._parent_session_id,
        )
        child_session = Session(
            session_id=child_session_id, store=self._session_store,
            sandbox=child_sandbox,
        )
        # WS-1 #151：子会话的 cwd 与 fork 同一规则——显式继承父会话的会话侧锚。
        # 不写的话每个子代理都会作为"未分组"会话出现在会话列表里（它们与父同属
        # 一个项目）。父无锚（历史遗留 / 父会话日志不在）→ 不写该字段，与父一致。
        child_session.append(
            SESSION_STARTED,
            {
                **cwd_event_data(self._parent_cwd()),
                "delegation_tree_id": tree_id,
                "delegation_root_session_id": self._root_session_id,
                "delegation_remaining_depth": allowance,
                "delegation_root_max_depth": self._root_max_depth,
                "delegation_root_max_delegations": self._max_delegations,
            },
            agent_id=spec.name,
        )
        # spawn 即注册（hub/lineage 语义：子代理在 spawn 时可见，不等完成）
        self.last_child_sessions.append(child_session)

        # 子代理的整段 run 都跑在**它自己**的配额作用域里：它再 spawn 时读到的是
        # 「我手里有什么工具 + 我还能往下几层」，而不是根的（#286 冻结语义 5）。
        with bind_tree_id(tree_id), bind_scope(SpawnScope(
            registry=child_runtime.registry, remaining=allowance,
        )):
            run_result: AgentRunResult = await child_runtime.run(child_session, full_task)

        status = ("completed" if run_result.status == "completed" else "failed")
        logger.info(
            "子代理 '%s' 完成：status=%s child_session=%s steps=%s",
            spec.name, run_result.status, child_session.session_id, run_result.steps,
        )
        fields = collect_result_fields(child_session.events, summary=run_result.final_text)
        summary = run_result.final_text
        # 大产物治理（#86，不变量 #15）：store 已配 → 全文交父侧 artifact 管线；
        # 未配 → 截断 + child session 指针（全文在 child JSONL，不丢数据）。
        if len(summary) > self._summary_limit and not self._overflow_configured:
            pointer = (f"\n\n[summary 超限已截断至 {self._summary_limit} 字符；"
                       f"完整输出见 child session {child_session.session_id}]")
            summary = summary[:self._summary_limit] + pointer
        return SubAgentResult(
            agent_id=spec.name, status=status, summary=summary,
            child_session_id=child_session.session_id, **fields,
        )
