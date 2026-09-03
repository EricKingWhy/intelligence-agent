"""AgentRuntime：把两轮 tool-calling 协议收进一段可读的异步循环。

每轮固定顺序：
    1. await model.ainvoke(messages)
    2. messages.append(AIMessage)
    3. steps += 1
    4. 若无 tool_calls → 返回最终回答；若 steps >= max_steps → 返回兜底状态；否则执行工具回填进入下一轮
"""

from __future__ import annotations

import logging
from typing import Any

from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, ToolMessage

from agent_harness.agent.types import (
    STATUS_COMPLETED,
    STATUS_MAX_STEPS_EXCEEDED,
    AgentRunResult,
)
from agent_harness.logging import log_event, new_span_id
from agent_harness.tooling import ToolExecutor, ToolRegistry

logger = logging.getLogger("agent_harness.agent")


class AgentRuntime:
    """最小透明 Agent Loop。

    构造时绑定 model + registry + executor + max_steps；一次 run() 用一条本地
    messages 链跑完。工具执行（校验/超时/重试/并发）下沉到 ToolExecutor；
    本类只保留"驱动循环"这一份职责。
    """

    def __init__(
        self,
        model: Any,
        registry: ToolRegistry,
        executor: ToolExecutor,
        max_steps: int = 20,
    ) -> None:
        self.model = model
        self.registry = registry
        self.executor = executor
        # max_steps 是"模型不收敛时的保险丝"，不是正常业务停止条件；
        # 正常停止由"模型不再返回 tool_calls"决定。
        self.max_steps = max_steps

    async def run(self, user_input: str) -> AgentRunResult:
        """跑完整条 Agent Loop，返回 AgentRunResult。"""
        messages: list[AnyMessage] = [HumanMessage(content=user_input)]
        steps = 0

        run_span = new_span_id()
        self._log("agent_start", "Agent Loop 开始", span_id=run_span, step=0,
                  outcome="started", agent_name="agent_runtime")

        while True:
            # 第 1 步：发起这一轮模型调用
            llm_span = new_span_id()
            ai: AIMessage = await self.model.ainvoke(messages)

            self._log("llm_call", f"第 {steps + 1} 轮模型调用完成", span_id=llm_span,
                      parent_span_id=run_span, step=steps + 1,
                      llm_input=user_input, llm_output=str(ai.content)[:200],
                      outcome="success")

            # 第 2 步：先把 AIMessage 落进消息链（必须在判断 tool_calls 之前：
            # ToolMessage 必须回答历史里真实存在的 assistant tool request）
            messages.append(ai)

            # 第 3 步：这一轮算一步（数模型轮数，不是工具个数）
            steps += 1

            # 第 4 步：先判停止信号——若模型选择最终答复则立即返回。
            # 必须在 max_steps 之前判：否则模型恰好在最后一轮收敛会被误报为不收敛。
            tool_calls = ai.tool_calls or []
            if not tool_calls:
                final = ai.content if isinstance(ai.content, str) else str(ai.content)
                self._log("agent_decision", "模型给出最终回答，Agent Loop 完成",
                          span_id=new_span_id(), parent_span_id=run_span, step=steps,
                          decision="finish", remaining_steps=0,
                          reason="本轮无 tool_calls，模型选择直接答复", outcome="success")
                self._log("task_completed", "Agent Loop 正常结束", span_id=run_span,
                          step=steps, outcome="success")
                return AgentRunResult(
                    status=STATUS_COMPLETED,
                    final_text=final,
                    steps=steps,
                )

            # 第 5 步：模型仍在请求工具——若已达 max_steps 则兜底返回。
            if steps >= self.max_steps:
                self._log("agent_decision", "模型不收敛，撞 max_steps 兜底",
                          span_id=new_span_id(), parent_span_id=run_span, step=steps,
                          decision="max_steps_exceeded", remaining_steps=0,
                          reason=f"连续 {steps} 轮仍在请求工具，触发保险丝", outcome="success")
                return AgentRunResult(
                    status=STATUS_MAX_STEPS_EXCEEDED,
                    final_text="",
                    steps=steps,
                )

            # 第 6 步：用 ToolExecutor 执行整批 tool_call 并按原 id 回填。
            # execute_batch 保序返回 + 显式 id 配对；ToolMessage 的 content
            # 是 ToolResult 的 JSON（含结构化的 ok/error_code/retryable/metadata）。
            args_by_id = {tc.get("id", ""): tc.get("args") for tc in tool_calls}
            executions = await self.executor.execute_batch(tool_calls)
            for execution in executions:
                result = execution.result
                content = result.model_dump_json()
                outcome: str = "success" if result.ok else "failure"

                self._log("tool_operation", f"工具回复 {outcome}",
                          span_id=new_span_id(), parent_span_id=run_span, step=steps,
                          tool_call_id=execution.tool_call_id,
                          tool_input=args_by_id.get(execution.tool_call_id),
                          tool_output=content[:200],
                          error_code=result.error_code,
                          retryable=result.retryable if not result.ok else None,
                          duration_ms=result.metadata.get("duration_ms"),
                          attempt=result.metadata.get("attempt"),
                          outcome=outcome)
                messages.append(ToolMessage(content=content, tool_call_id=execution.tool_call_id))

    def _log(self, event_type: str, message: str, **fields: Any) -> None:
        """打一条结构化日志；无 handler 时静默 no-op（不污染未配日志的调用方/测试）。"""
        if not logger.hasHandlers():
            return
        log_event(logger, event_type, message, **fields)
