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
- registry 经 AgentFactory 过滤（depth=1：child 无 delegate）；
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
from agent_harness.sandbox import WorkspaceRegistry
from agent_harness.session import SESSION_STARTED, Session
from agent_harness.session.store import JsonlSessionStore

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
        elif event.type == "artifact/created":
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
    ) -> None:
        """build_runtime 在模型链与 registry 就绪后调用（幂等：重复激活覆盖）。"""
        self._factory = factory
        self._source_registry = source_registry
        self._session_store = session_store
        self._workspace_registry = workspace_registry
        self._parent_session_id = parent_session_id
        self._summary_limit = summary_limit
        self._overflow_configured = overflow_configured
        self._max_active_children = max_active_children
        self._active_children = (
            asyncio.Semaphore(max_active_children) if max_active_children > 0 else None
        )
        self._activated = True

    def profile(self, target: str) -> AgentSpec:
        try:
            return self._profiles[target]
        except KeyError:
            raise ValueError(
                f"未知 profile '{target}'（可选：{sorted(self._profiles)}）"
            ) from None

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

        # 并发 child 封顶（#90）：超出排队等待，不失败不丢弃。
        async with (self._active_children or nullcontext(None)):
            return await self._run_child(spec, full_task)

    async def _run_child(self, spec: AgentSpec, full_task: str) -> SubAgentResult:
        # child sandbox = 父的同一实例（spec §9：coding 的改动 review 直接可见）
        parent_sandbox = self._workspace_registry.get(self._parent_session_id)
        child_session = Session(
            session_id=str(uuid4()), store=self._session_store,
            sandbox=parent_sandbox,
        )
        child_session.append(SESSION_STARTED, {}, agent_id=spec.name)

        child_runtime = self._factory.create(spec, source_registry=self._source_registry)
        run_result: AgentRunResult = await child_runtime.run(child_session, full_task)
        self.last_child_sessions.append(child_session)

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
