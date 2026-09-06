"""AgentFactory：AgentSpec → 校验 → 构造现有 AgentRuntime（ADR-0015 决策 2/11）。

SubAgent MUST 复用同一 Agent Loop（不变量 #19）——Factory 不造新 Runtime
类，只做三件事：
1. 权限校验：spec 申请的 tool_scope 中，「存在于全量 registry 但不在可授予
   集合」的申请 = 越权提升，拒绝（防 child 自配全量工具的逃逸通道）；
2. 缺席降级：optional capability 的工具（如 websearch 未配）不在全量
   registry 里 → 降级丢弃 + warning（同 capability 降级缺席语义）；
3. 构造期收窄：从全量 registry 过滤出【新】ToolRegistry 实例 + 经
   executor_factory 组装 Executor——child 物理上拿不到未授权工具。

模型链继承（决策 14）：Factory 持有 primary/fallback 模型与流式守卫配置，
child runtime 与 main 同链，fallback/看门狗自动生效、零新代码。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from agent_harness.agent.profiles import AgentSpec
from agent_harness.agent.runtime import AgentRuntime
from agent_harness.tooling import ToolExecutor, ToolRegistry

logger = logging.getLogger(__name__)


class AgentFactory:
    """AgentSpec → AgentRuntime 的唯一构造口（校验 + 收窄 + 继承）。"""

    def __init__(
        self,
        *,
        model: Any,
        fallback_model: Any | None = None,
        executor_factory: Callable[[ToolRegistry], ToolExecutor] | None = None,
        primary_model_name: str = "primary",
        fallback_model_name: str = "fallback",
        stream_idle_timeout: float = 0.0,
        stream_total_timeout: float = 0.0,
    ) -> None:
        self._model = model
        self._fallback_model = fallback_model
        # executor 组装缝：policy/approval/ledger/overflow 等运行配置由调用方
        # 闭包捕获——Factory 不关心 Executor 怎么配，只保证 child registry 先
        # 过滤再进入组装。
        self._executor_factory = executor_factory
        self._primary_model_name = primary_model_name
        self._fallback_model_name = fallback_model_name
        self._stream_idle_timeout = stream_idle_timeout
        self._stream_total_timeout = stream_total_timeout

    def create(
        self,
        spec: AgentSpec,
        *,
        source_registry: ToolRegistry,
        grantable: frozenset[str] | set[str] | None = None,
    ) -> AgentRuntime:
        """按 spec 构造 child runtime（复用同一 Agent Loop，不变量 #19）。"""
        source_names = {tool.name for tool in source_registry.list()}
        grantable_names = set(grantable) if grantable is not None else set(source_names)

        # 越权提升：工具真实存在但不在可授予集合 → 拒绝（防逃逸通道）。
        escalated = (spec.tool_scope & source_names) - grantable_names
        if escalated:
            raise ValueError(
                f"AgentSpec '{spec.name}' 申请了不可授予的工具：{sorted(escalated)}"
                f"（可授予：{sorted(grantable_names)}）"
            )
        # 缺席降级：optional capability 未配（不在全量 registry）→ 丢弃 + 告警。
        absent = spec.tool_scope - source_names
        if absent:
            logger.warning(
                "AgentSpec '%s' 申请的工具未注册（optional capability 未配置？）"
                "已降级丢弃：%s",
                spec.name, sorted(absent),
            )
        effective = spec.tool_scope & source_names

        child_registry = source_registry.filtered(effective)
        executor = (self._executor_factory(child_registry) if self._executor_factory
                    else ToolExecutor(child_registry))
        return AgentRuntime(
            model=self._model,
            registry=child_registry,
            executor=executor,
            max_steps=spec.max_steps,
            fallback_model=self._fallback_model,
            primary_model_name=self._primary_model_name,
            fallback_model_name=self._fallback_model_name,
            stream_idle_timeout=self._stream_idle_timeout,
            stream_total_timeout=self._stream_total_timeout,
        )
