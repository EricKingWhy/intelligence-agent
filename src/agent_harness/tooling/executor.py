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
- Task 4：批次调度 execute_batch（全 READ_ONLY 并发 / 含 MUTATING 整批串行）+ 严格保序。
  每条 tool_call 仍走单条 execute()，批次层只决定并发还是串行。

明确【不做】：
- 不做细粒度 DAG / 读写冲突分析（整批串行一刀切即可）；
- 不做并发上限（信号量）、批次级超时、部分失败回滚（都进 Backlog）；
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
from agent_harness.tooling.approval import (
    ApprovalCallback,
    ApprovalRequest,
    ApprovalResponse,
    approval_reason,
    needs_approval,
)
from agent_harness.tooling.contract import PermissionPolicy, Tool, ToolSideEffect
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

    def __init__(
        self,
        registry: ToolRegistry,
        *,
        policy: PermissionPolicy = PermissionPolicy.WORKSPACE_WRITE,
        approval_callback: ApprovalCallback | None = None,
    ) -> None:
        self._registry = registry
        self._policy = policy
        self._approval_callback = approval_callback

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

        # -- 阶段 2.5：approval gate -- 05_SANDBOX_CODING_TOOLS.md §6 的 REQUIRE_APPROVAL。
        # 在 validate 之后、execute 之前：参数已合法，但授权关卡决定是否能跑。
        # per-call scoping 由设计保证：每次 execute 独立检查，不存储"已批准"状态。
        denied = self._check_approval(tool_call_id, name, tool, raw_args)
        if denied is not None:
            return denied

        # -- 阶段 3：execute + Timeout 边界 + 唯一 Retry Layer（Task 3）--
        # 三阶段顺序不变；Timeout/Retry 只包住 tool.execute 这一步。
        result = await self._execute_with_retry(tool_call_id, name, tool, validated)
        return ToolExecution(tool_call_id=tool_call_id, result=result)

    async def execute_batch(self, tool_calls: list[dict[str, Any]]) -> list[ToolExecution]:
        """执行一批 tool_calls，返回 ToolExecution 列表（顺序 = 输入顺序）。

        一条可解释规则决定调度（Task 4 的核心）：
          - 全 READ_ONLY → asyncio.gather 并发执行（独立读操作可安全重叠）；
          - 任一 MUTATING → 整批按原顺序串行（保守默认，避免副作用乱序/部分失败难归因）。
        无论并发还是串行，结果列表都按【输入 tool_calls 的顺序】返回，不是完成顺序。

        为什么这是 Task 5 的接线点：
          AgentRuntime 的 `for tc in tool_calls` 串行循环会被这一句替换。
          每条 tool_call 仍走完整 execute()（lookup→validate→timeout→retry 全复用），
          execute_batch 只在【批次层】决定并发还是串行，不重复执行域逻辑。

        并发为什么用 gather 而不是 TaskGroup：
          gather 最关键的特性是【返回顺序 = 喂入顺序，与完成顺序无关】--
          这正是"并发跑、保序返"的天然实现，也是 ToolMessage 配对的安全网。
          TaskGroup（3.11+）侧重结构化异常传播，返回顺序语义不如 gather 直观；
          部分失败的传播策略也不同（gather(return_exceptions=True) 可兜底，TaskGroup 取消其余）。
          这里要的是"每条都跑完、结果按序排"，gather 更贴。

        mode 决策：
          扫描本批所有 tool_call 的 side_effect：
          - 某工具名查不到（将 TOOL_NOT_FOUND）→ 按 READ_ONLY 算，不影响并发决策、让其走正常 execute 报错；
          - 全部 READ_ONLY → "parallel"；
          - 任一 MUTATING → "serial"。
        """
        if not tool_calls:
            return []

        mode = self._decide_mode(tool_calls)

        if mode == "parallel":
            # gather 的顺序保持：即使第 3 个先完成，返回列表仍是 [结果1, 结果2, 结果3]。
            return await asyncio.gather(
                *(self.execute(tc) for tc in tool_calls)
            )

        # serial：含 MUTATING 整批串行，按原顺序逐个 await。
        return [await self.execute(tc) for tc in tool_calls]

    def _decide_mode(self, tool_calls: list[dict[str, Any]]) -> str:
        """扫描批次，决定并发还是串行。

        一条可解释规则：全 READ_ONLY 才并发；任一 MUTATING 整批串行。
        未注册的工具名按 READ_ONLY 算（不影响调度，让其走 execute 正常报错）。
        """
        for tc in tool_calls:
            name = tc.get("name", "")
            try:
                tool = self._registry.get(name)
            except KeyError:
                # 工具不存在：不该用它干扰并发决策（可能只是本批其它工具合法地并发）。
                # 让它走正常 execute() 报 TOOL_NOT_FOUND，mode 只看合法工具的 side_effect。
                continue
            # 任一 MUTATING 即整批串行：一个就够，无需扫剩下的工具。
            # 判断钉在 side_effect 枚举位上（与 should_retry 只看 retryable 同理）：
            # 确定性语义，不猜工具名、不解析描述。
            if tool.side_effect == ToolSideEffect.MUTATING:
                return "serial"

        # 全部扫完没命中 MUTATING → 全 READ_ONLY → 并发。
        return "parallel"

    def _check_approval(
        self,
        tool_call_id: str,
        name: str,
        tool: Tool,
        raw_args: dict[str, Any],
    ) -> ToolExecution | None:
        """阶段 2.5：审批关卡。返回 None 表示放行，返回 ToolExecution 表示拒绝。

        逻辑（05_SANDBOX_CODING_TOOLS.md §6）：
        - DANGER_FULL_ACCESS → 放行。
        - tool.permission 级别在 policy 允许范围内 → 放行。
        - 超级别或 DANGER 在受限 policy 下 → 需要 approval：
          - 无 callback → PERMISSION_DENIED（安全默认）。
          - 有 callback → 调 callback；approved → 放行；denied → PERMISSION_DENIED。
        """
        tool_perm = tool.permission
        if not needs_approval(tool_perm, self._policy):
            return None

        reason = approval_reason(tool_perm, self._policy)
        request = ApprovalRequest(
            tool_name=name,
            args=raw_args,
            permission=tool_perm,
            policy=self._policy,
            reason=reason,
        )

        if self._approval_callback is None:
            # 安全默认值：无审批回调 → 拒绝。绝不静默放行高风险操作。
            return ToolExecution(
                tool_call_id=tool_call_id,
                result=ToolResult.failure(
                    message=f"工具 '{name}' 需要审批但未配置审批回调，已被拒绝。{reason}",
                    error_code=ErrorCode.PERMISSION_DENIED,
                    retryable=False,
                ),
            )

        response: ApprovalResponse = self._approval_callback(request)
        if response.approved:
            # per-call scoping：批准只对这次 execute 生效，不存状态。
            return None

        return ToolExecution(
            tool_call_id=tool_call_id,
            result=ToolResult.failure(
                message=f"工具 '{name}' 的执行请求已被拒绝。{response.reason or reason}",
                error_code=ErrorCode.PERMISSION_DENIED,
                retryable=False,
            ),
        )

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
