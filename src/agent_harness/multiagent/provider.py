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

import logging
from dataclasses import dataclass
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
    ) -> None:
        """build_runtime 在模型链与 registry 就绪后调用（幂等：重复激活覆盖）。"""
        self._factory = factory
        self._source_registry = source_registry
        self._session_store = session_store
        self._workspace_registry = workspace_registry
        self._parent_session_id = parent_session_id
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
            target, run_result.status, child_session.session_id, run_result.steps,
        )
        return SubAgentResult(
            agent_id=spec.name, status=status, summary=run_result.final_text,
            child_session_id=child_session.session_id,
        )
