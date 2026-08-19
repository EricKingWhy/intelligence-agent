"""ToolExecutor：Tool 的【执行域】--把 tool_call 跑成结构化 ToolResult。

为什么独立成 executor.py（和 registry.py 分开）：
- Registry 是"配置时"对象：注册什么、查什么，纯静态。
- Executor 是"运行时"对象：按一条 tool_call 跑完 lookup->validate->execute，
  并把任意失败映射成稳定 ErrorCode。两者生命周期、可替换性、测试边界都不同，
  耦合会让"换执行策略"变成"动注册表"。

职责演进：
- Task 2：单次执行链（Validation-first）--校验在 execute 之前，参数非法则 execute 次数=0。
- Task 3：阶段3 外包两层边界--Timeout（asyncio.timeout + tool.timeout_seconds）
  和唯一 Retry Layer（只看 retryable 位，MAX_ATTEMPTS 上限）。

明确【不做】：
- 不做并发/批次调度（Task 4，READ_ONLY 并发 / 含 MUTATING 串行）；
- 不做 Backoff / Circuit Breaker / Retry Budget（重试间隔为立即；复杂度进 Backlog）；
- 不产出 ToolMessage（那是 AgentRuntime 在 Task 5 的接线活）；
- 不调 LLM、不决定 Agent 是否停止、不维护 Session。

设计铁律一：Executor 对外【永远返回 ToolExecution】。
任何失败（找不到工具 / 参数非法 / 超时 / 工具内部抛异常）都被映射成失败 ToolResult，
不让异常冒泡。这条结果最终要回填成 ToolMessage 给模型自纠错--
Executor 的产出是"结果"，不是"中断"。上层（AgentRuntime）只消费结果，不接异常。

设计铁律二：Executor 是 Tool 执行域的【唯一 Retry Layer】。
往内：模型 SDK 层应关闭自己的重试；往外：AgentRuntime 只消费 ToolResult、不重试。
多层各重试 3 次 = 最坏 27 次真实执行（Retry Amplification），一个慢工具足以打爆下游。
"""

from __future__ import annotations

import asyncio
import logging
from time import perf_counter
from typing import Any

from pydantic import BaseModel, ValidationError

from agent_harness.logging import log_event
from agent_harness.tooling.contract import Tool
from agent_harness.tooling.registry import ToolRegistry
from agent_harness.tooling.result import ErrorCode, ToolResult

logger = logging.getLogger("agent_harness.tooling.executor")

#: 一次 tool_call 在执行域内最多尝试几次（含第一次）。
#: 为什么是模块级常量而不是可配置项：重试上限必须收敛在【唯一 Retry Layer】
#: 一处可见可调；一旦可配置，"到底重试几次"会重新散落回各层，铁律二就被架空。
MAX_ATTEMPTS = 3

#: 阶段3 异常分类表：异常类型 -> (error_code, retryable)。
#: 分类是确定性的【类型判断】，绝不解析错误字符串（字符串会变，类型不会）。
#: - TimeoutError 不在表里：asyncio.timeout 到点抛它，单独捕获成 TIMEOUT；
#: - PermissionError -> 权限问题重试也不会有权限，确定性失败；
#: - ConnectionError -> 网络抖动重试可能自愈，暂时性失败；
#: - 其余异常 -> 工具内部错误，默认不重试（工具作者想声明"值得重试"，
#:   应自己返回带 retryable=True 的 ToolResult，重试循环同样尊重这个位）。
_EXCEPTION_CLASSIFICATION: dict[type[Exception], tuple[ErrorCode, bool]] = {
    PermissionError: (ErrorCode.PERMISSION_DENIED, False),
    ConnectionError: (ErrorCode.TRANSIENT_ERROR, True),
}


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
    """Tool 执行域：Validation-first 三阶段 + Timeout 边界 + 唯一 Retry Layer。

    构造时绑定一个 ToolRegistry（静态路由）；execute() 跑一条 tool_call。
    阶段3 内部由 _execute_with_retry 驱动：每次尝试受 tool.timeout_seconds 约束，
    是否重试只看 ToolResult.retryable 位，上限 MAX_ATTEMPTS。
    """

    def __init__(self, registry: ToolRegistry) -> None:
        self._registry = registry

    async def execute(self, tool_call: dict[str, Any]) -> ToolExecution:
        """跑完一条 tool_call，返回 ToolExecution（成功或失败，绝不抛异常给上层）。

        tool_call 形状（和 LangChain 的 tool_calls 一致）：
          {"id": str, "name": str, "args": dict}

        三阶段 + 三失败出口（Validation-first 的核心）：
          [1] Registry.get(name)        找不到     -> KeyError     -> TOOL_NOT_FOUND
          [2] args_schema.model_validate(args)  校验失败 -> ValidationError -> INVALID_ARGUMENT
          [3] tool.execute(validated)   成功/失败  -> 透传 ToolResult / 异常分类映射
        阶段 [1][2] 失败时，tool.execute【根本不被调用】（execute 次数=0）。
        阶段 [3]（Task 3）外包 Timeout 边界 + retryable 驱动的重试循环：
        为什么只包阶段3：查字典、跑 Pydantic 都是本地瞬时操作，真正会慢、会挂
        （HTTP/文件/DB）的只有 execute 这一步。
        """
        tool_call_id = tool_call.get("id", "")
        name = tool_call.get("name", "")
        raw_args = tool_call.get("args") or {}

        # -- 阶段 1：lookup -- Registry 找不到抛 KeyError，由本层映射成 TOOL_NOT_FOUND。
        # 为什么不抛自定义异常：见 registry.py 注释--Registry 只管查，映射成执行域语义是 Executor 的活。
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

        # -- 阶段 2：validation -- Validation-first 的核心位置。
        # 校验发生在 execute【之前】：参数非法 -> tool.execute 根本不被调用（execute 次数=0）。
        # 这条边界以后喂给所有 Tool（Local/Knowledge/MCP），是最锋利的一刀。
        try:
            validated: BaseModel = tool.args_schema.model_validate(raw_args)
        except ValidationError as e:
            # ValidationError -> INVALID_ARGUMENT：取 e.errors() 前 2 条拼进 message，
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

        # -- 阶段 3：execute + Timeout 边界 + 唯一 Retry Layer（Task 3）--
        # 三阶段顺序不变；Timeout/Retry 只包住 tool.execute 这一步。
        result = await self._execute_with_retry(tool_call_id, name, tool, validated)
        return ToolExecution(tool_call_id=tool_call_id, result=result)

    async def _execute_with_retry(
        self, tool_call_id: str, name: str, tool: Tool, validated: BaseModel
    ) -> ToolResult:
        """阶段3 主体：每次尝试被 timeout 包住，retryable 位驱动是否再来一轮。

        一轮 attempt 的数据流：
          t0 = perf_counter()
          asyncio.timeout(tool.timeout_seconds) 包住 await tool.execute(validated)
            -> 正常返回 ToolResult  -> 透传（尊重工具自己的 ok/retryable 语义）
            -> 抛 TimeoutError      -> 映射 TIMEOUT（可重试，外部依赖慢通常是暂时的）
            -> 抛其它 Exception     -> 查 _EXCEPTION_CLASSIFICATION 分类
          记 duration_ms -> 写一条 tool_operation 日志 -> 回填 metadata
          -> 重试决策（失败且 retryable 且未用完 MAX_ATTEMPTS 才重试）-> 决定重试则写一条 retry 日志
        """
        total_ms = 0.0
        result: ToolResult | None = None

        for attempt in range(1, MAX_ATTEMPTS + 1):
            t0 = perf_counter()
            try:
                # Timeout 边界：只包 execute 这一行；到点未返回即被取消并抛 TimeoutError。
                async with asyncio.timeout(tool.timeout_seconds):
                    result = await tool.execute(validated)
            except TimeoutError:
                # asyncio.timeout 到点把 execute 掐断，抛出 TimeoutError。
                # message 写清工具名 + 超时上限，给模型"外部依赖可能暂时无响应"的纠错线索；
                # error_code 取 TIMEOUT（result.py 里该码语义即"超时 → 可重试"）；
                # retryable=True：外部服务慢通常是暂时的，重试可能自愈（对照
                # _EXCEPTION_CLASSIFICATION 里 ConnectionError 同为暂时性失败）。
                result = ToolResult.failure(
                    message=(
                        f"工具 '{name}' 执行超时（上限 {tool.timeout_seconds} 秒），"
                        "可能是外部依赖暂时无响应，可稍后重试。"
                    ),
                    error_code=ErrorCode.TIMEOUT,
                    retryable=True,
                )
            except Exception as e:  # noqa: BLE001
                # 宽捕获理由同 Task 2：工具是开放世界，无法预知会抛什么。
                # 区别是现在先查分类表（isinstance 连子类一起认），查不到再兜底。
                error_code, retryable = ErrorCode.TOOL_EXECUTION_ERROR, False
                for exc_type, (mapped_code, mapped_retryable) in (
                    _EXCEPTION_CLASSIFICATION.items()
                ):
                    if isinstance(e, exc_type):
                        error_code, retryable = mapped_code, mapped_retryable
                        break
                result = ToolResult.failure(
                    message=f"工具 '{name}' 执行异常: {type(e).__name__}: {e}",
                    error_code=error_code,
                    retryable=retryable,
                )

            duration_ms = round((perf_counter() - t0) * 1000, 1)
            total_ms += duration_ms
            assert result is not None  # 三个分支必赋值

            # 把执行元数据回填进 metadata（model_copy 复制，不改工具返回的原对象）。
            # 模型和测试都能从这里读出"第几次尝试、花了多久"。
            result = result.model_copy(
                update={
                    "metadata": {
                        **result.metadata,
                        "attempt": attempt,
                        "max_attempts": MAX_ATTEMPTS,
                        "duration_ms": duration_ms,
                        "total_duration_ms": round(total_ms, 1),
                    }
                }
            )

            # 每个 attempt 一条 tool_operation：JSONL 靠它还原完整重试链。
            self._log(
                "tool_operation",
                f"工具 {name} 第 {attempt}/{MAX_ATTEMPTS} 次尝试"
                f"{'成功' if result.ok else '失败'}",
                tool_call_id=tool_call_id,
                tool_name=name,
                attempt=attempt,
                max_attempts=MAX_ATTEMPTS,
                timeout_ms=round(tool.timeout_seconds * 1000),
                duration_ms=duration_ms,
                error_code=result.error_code,
                retryable=result.retryable if not result.ok else None,
                outcome="success" if result.ok else "failure",
            )

            # -- 重试决策：唯一 Retry Layer 的心脏 --
            # 反向取补集：三种必须停的情况（成功 / retryable=False / 用完上限）
            # 都不成立时才再来一轮。
            should_retry: bool = (
                not result.ok  # a. 已成功 -> 停
                and result.retryable  # b. 确定性失败 -> 停
                and attempt < MAX_ATTEMPTS  # c. 用完上限 -> 停，防无限重试
            )

            if not should_retry:
                break

            # 决定重试：写一条 retry 事件，JSONL 里"为什么再试"有据可查。
            self._log(
                "retry",
                f"工具 {name} 第 {attempt} 次尝试失败且可重试，准备第 {attempt + 1} 次尝试",
                tool_call_id=tool_call_id,
                tool_name=name,
                attempt=attempt,
                max_attempts=MAX_ATTEMPTS,
                error_code=result.error_code,
                retryable=True,
                outcome="pending",
            )

        assert result is not None
        return result

    def _log(self, event_type: str, message: str, **fields: Any) -> None:
        """打一条结构化日志；无 handler 时零成本 no-op。

        同 AgentRuntime 的做法：测试里高频跑 execute()，没有 handler 时
        短路掉 log_event，日志彻底零成本；调用方 setup_logging() 后正常写 JSONL。
        """
        if not logger.hasHandlers():
            return
        log_event(logger, event_type, message, **fields)
