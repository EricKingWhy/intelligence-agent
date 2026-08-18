"""ToolExecutor：Tool 的【执行域】——把 tool_call 跑成结构化 ToolResult。

为什么独立成 executor.py（和 registry.py 分开）：
- Registry 是"配置时"对象：注册什么、查什么，纯静态。
- Executor 是"运行时"对象：按一条 tool_call 跑完 lookup→validate→execute，
  并把任意失败映射成稳定 ErrorCode。两者生命周期、可替换性、测试边界都不同，
  耦合会让"换执行策略"变成"动注册表"。

Task 2 的唯一职责：单次执行链（Validation-first）。明确【不做】：
- 不做 Timeout（Task 3，靠 timeout_seconds + asyncio.timeout）；
- 不做 Retry（Task 3，唯一 Retry Layer）；
- 不做并发/批次调度（Task 4，READ_ONLY 并发 / 含 MUTATING 串行）；
- 不产出 ToolMessage（那是 AgentRuntime 在 Task 5 的接线活）；
- 不调 LLM、不决定 Agent 是否停止、不维护 Session。

设计铁律：Executor 对外【永远返回 ToolExecution】。
任何失败（找不到工具 / 参数非法 / 工具内部抛异常）都被映射成失败 ToolResult，
不让异常冒泡。理由：这条结果最终要回填成 ToolMessage 给模型自纠错——
Executor 的产出是"结果"，不是"中断"。上层（AgentRuntime）只消费结果，不接异常。
"""

from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel, ValidationError

from agent_harness.tooling.contract import Tool
from agent_harness.tooling.registry import ToolRegistry
from agent_harness.tooling.result import ErrorCode, ToolResult

logger = logging.getLogger("agent_harness.tooling.executor")


class ToolExecution(BaseModel):
    """一次 tool_call 的执行产出：结果 + 它回答的是哪个 tool_call。

    为什么需要这一层（而不是 Executor 直接返回 ToolResult）：
    - ToolResult 本身不知道"我是回答哪个 tool_call 的"；
    - 但 ToolMessage 协议要求 tool_call_id 和 assistant 的 tool request 严格配对，
      批次调度（Task 4）也要按原 call 顺序回填。所以"id↔结果"的配对必须显式保留。

    model_config 关闭校验后口子：ToolResult 是嵌套 Pydantic，默认会深度校验，
    这里我们只在 Executor 内部构造、字段已确定合法，关掉无关校验更省。
    """

    model_config = {"arbitrary_types_allowed": True}

    tool_call_id: str
    result: ToolResult


class ToolExecutor:
    """Tool 执行域：单次执行链（Validation-first），无 Timeout/Retry/并发。

    构造时绑定一个 ToolRegistry（静态路由）；execute() 跑一条 tool_call。
    """

    def __init__(self, registry: ToolRegistry) -> None:
        self._registry = registry

    async def execute(self, tool_call: dict[str, Any]) -> ToolExecution:
        """跑完一条 tool_call，返回 ToolExecution（成功或失败，绝不抛异常给上层）。

        tool_call 形状（和 LangChain 的 tool_calls 一致）：
          {"id": str, "name": str, "args": dict}

        三阶段 + 三失败出口（Validation-first 的核心）：
          [1] Registry.get(name)        找不到     → KeyError     → TOOL_NOT_FOUND
          [2] args_schema.model_validate(args)  校验失败 → ValidationError → INVALID_ARGUMENT
          [3] tool.execute(validated)   成功/失败  → 透传 ToolResult / 异常 → TOOL_EXECUTION_ERROR
        阶段 [1][2] 失败时，tool.execute【根本不被调用】（execute 次数=0）。
        """
        tool_call_id = tool_call.get("id", "")
        name = tool_call.get("name", "")
        raw_args = tool_call.get("args") or {}

        # —— 阶段 1：lookup —— Registry 找不到抛 KeyError，由本层映射成 TOOL_NOT_FOUND。
        # 为什么不抛自定义异常：见 registry.py 注释——Registry 只管查，映射成执行域语义是 Executor 的活。
        try:
            tool: Tool = self._registry.get(name)
        except KeyError:
            return ToolExecution(
                tool_call_id=tool_call_id,
                result=ToolResult.failure(
                    message=f"工具 '{name}' 未注册，无法执行。请检查工具名拼写或从可用工具中选择。",
                    error_code=ErrorCode.TOOL_NOT_FOUND,
                    retryable=False,
                ),
            )

        # —— 阶段 2：validation —— Validation-first 的核心位置。
        # 校验发生在 execute【之前】：参数非法 → tool.execute 根本不被调用（execute 次数=0）。
        # 这条边界以后喂给所有 Tool（Local/Knowledge/MCP），是最锋利的一刀。
        try:
            validated: BaseModel = tool.args_schema.model_validate(raw_args)
        except ValidationError as e:
            # ValidationError → INVALID_ARGUMENT：取 e.errors() 前 2 条拼进 message，
            # 让模型知道哪个字段、错在哪，够自纠错又不灌满上下文窗口。
            details = "; ".join(
                f"{'.'.join(str(p) for p in err.get('loc', ()))}: {err.get('msg', '')}"
                for err in e.errors()[:2]
            )
            return ToolExecution(
                tool_call_id=tool_call_id,
                result=ToolResult.failure(
                    message=f"工具 '{name}' 参数校验失败：{details}。请修正参数后重新调用。",
                    error_code=ErrorCode.INVALID_ARGUMENT,
                    retryable=False,  # 参数错是确定性的，重试也是同样的错
                ),
            )

        # —— 阶段 3：execute —— 校验已过，拿到的是合法 Pydantic 实例。
        # 工具自己抛异常 → 映射 TOOL_EXECUTION_ERROR，仍不冒泡给上层。
        # （Task 3 会在这里加 Timeout 边界和 Transient 分类，今天只跑一次。）
        try:
            result: ToolResult = await tool.execute(validated)
            # 工具正常返回 ToolResult，透传（它内部的成功/失败语义由工具自己决定）。
            return ToolExecution(tool_call_id=tool_call_id, result=result)
        except Exception as e:  # noqa: BLE001
            # 工具内部异常（开放世界，Runtime 无法预知工具会抛什么）→ 兜底成执行错误。
            # 宽捕获是本层职责：和 Day3 _exec_tool 同样的开放世界理由，但这里把语义固化成 ErrorCode。
            return ToolExecution(
                tool_call_id=tool_call_id,
                result=ToolResult.failure(
                    message=f"工具 '{name}' 执行异常: {type(e).__name__}: {e}",
                    error_code=ErrorCode.TOOL_EXECUTION_ERROR,
                    retryable=False,
                ),
            )
