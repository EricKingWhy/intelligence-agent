"""delegate 工具：supervisor 的委派入口（Phase 13 T2, #83, ADR-0015 决策 4）。

DelegationDecision 即 delegate 工具调用参数（spec §4）——编排决策由模型
做出（agentic），工具经统一 ToolExecutor（不变量 #7，零旁路）。V1 阻塞
串行：调用 → child 跑完 → SubAgentResult 回填。

语义：
- child status=completed → ToolResult.success（payload = result JSON）；
- child 非 completed → ToolResult.failure（payload 仍带 result，retryable
  =False）——失败事实如实呈现，重试/放弃由 supervisor 决策（决策 16）；
- 未激活 / 未知 target → 明确失败，绝不静默伪装。
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, Field

from agent_harness.multiagent.provider import InProcessSubagentProvider
from agent_harness.tooling import Tool, ToolResult, ToolSideEffect
from agent_harness.tooling.contract import ToolPermission
from agent_harness.tooling.reconcile import ReconcileHint
from agent_harness.tooling.result import ErrorCode


class _DelegateArgs(BaseModel):
    target: str = Field(
        ...,
        description="委派目标的 profile 名（可选值见工具描述；只能是预定义角色）",
    )
    task: str = Field(
        ...,
        min_length=1,
        description=(
            "给子代理的完整自洽任务描述——子代理看不到你们的对话历史，任务"
            "描述必须包含它需要的全部上下文与验收标准"
        ),
    )
    constraints: list[str] = Field(
        default_factory=list,
        description="可选约束清单（如：只改某目录、必须先写测试）",
    )


class DelegateTool(Tool):
    """委派工具：multiagent capability 贡献的编排入口（可装卸插件）。"""

    def __init__(self, provider: InProcessSubagentProvider) -> None:
        self._provider = provider

    @property
    def name(self) -> str:
        return "delegate"

    @property
    def description(self) -> str:
        available = ", ".join(sorted(self._provider._profiles)) or "（无可用 profile）"
        return (
            "把一个 scoped task 委派给专门的子代理执行（阻塞：返回时任务已完"
            f"成或失败）。可选 target：{available}。子代理拥有独立上下文，"
            "task 描述必须自洽；返回结构化结果（status/summary 等）。适合可"
            "并行的专项深入；琐碎小事请亲自完成，不要为委派而委派。"
        )

    @property
    def args_schema(self) -> type[BaseModel]:
        return _DelegateArgs

    @property
    def side_effect(self) -> ToolSideEffect:
        # child 可写共享 workspace——委派本身就是有副作用的编排动作。
        return ToolSideEffect.MUTATING

    @property
    def permission(self) -> ToolPermission:
        return ToolPermission.WORKSPACE_WRITE

    @property
    def timeout_seconds(self) -> float:
        # child 是一次完整 run（多轮模型调用），父工具超时必须给足余量；
        # 真正的卡流治理由 child 继承的 stall 看门狗承担（秒级）。
        return 1800.0

    @property
    def reconcile_hint(self) -> ReconcileHint:
        return ReconcileHint(
            verifiable=True,
            suggested_action="重复同一委派会得到新 child 会话；换措辞或亲自验证。",
        )

    async def execute(self, args: _DelegateArgs) -> ToolResult:
        try:
            result = await self._provider.run(
                target=args.target, task=args.task, constraints=args.constraints,
            )
        except ValueError as error:
            return ToolResult.failure(
                message=str(error), error_code=ErrorCode.INVALID_ARGUMENT,
            )
        except RuntimeError as error:
            return ToolResult.failure(
                message=str(error), error_code=ErrorCode.TOOL_EXECUTION_ERROR,
            )
        payload: dict[str, Any] = {
            "agent_id": result.agent_id,
            "status": result.status,
            "summary": result.summary,
        }
        # 真实来源字段：无则省略（绝不伪造/补零，ADR-0015 决策 12）
        for field_name in ("citations", "artifacts", "changed_files", "unresolved"):
            value = getattr(result, field_name, [])
            if value:
                payload[field_name] = value
        # 白盒透明（ADR-0015 决策 6/8）：委派事实以 pending_events 经 executor
        # 落盘（tool/call 之后、tool/result 之前）。started/finished 的落盘时点
        # 都是委派完成时——阻塞串行模型下无观察者可见差。
        pending_events = [
            ("agent/delegation-started", {
                "target": args.target, "task": args.task,
                "child_session_id": result.child_session_id,
            }),
            ("agent/delegation-finished", {
                "target": args.target,
                "child_session_id": result.child_session_id,
                "status": result.status, "summary": result.summary,
            }),
        ]
        if result.status == "completed":
            return ToolResult.success(
                message=f"子代理 '{result.agent_id}' 完成：{result.summary[:200]}",
                data={"output": json.dumps(payload, ensure_ascii=False)},
                pending_events=pending_events,
            )
        # failure 无 data 参数：结构化 payload 走 metadata（内容仍经
        # model_dump_json 全量回灌给 supervisor 模型）。
        return ToolResult.failure(
            message=f"子代理 '{result.agent_id}' 未能完成（status={result.status}）："
                    f"{result.summary[:200] or '（无输出）'}",
            error_code=ErrorCode.TOOL_EXECUTION_ERROR,
            retryable=False,
            metadata={"output": json.dumps(payload, ensure_ascii=False)},
            pending_events=pending_events,
        )
