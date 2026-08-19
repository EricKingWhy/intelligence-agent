"""AgentRuntime：把 Day 2 手工的两轮协议收进一段可读的异步循环。

核心顺序（每轮固定，不能调换）：
    1. await model.ainvoke(messages)        # 第 N 轮模型调用
    2. messages.append(AIMessage)            # ★ 先落地"模型这轮说了什么"
    3. steps += 1                            # 数模型轮数，不是工具个数
    4. 看 ai.tool_calls 是否为空决定停止还是继续
    5. 串行执行每个 tool_call，用【原 id】构造 ToolMessage 回填
    6. 没了 tool_calls 就返回最终回答；撞到 max_steps 就返回兜底状态

为什么不用高级 Agent 框架：
- 这一层是 Agent 的心脏，亲手写过才知道框架替你维护了什么、出错从哪查；
- 今天只看懂"模型提议 -> Runtime 执行回填 -> 下一轮"这一条主链。
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

    构造时就绑定好 model + registry + executor + max_steps；一次 run() 用一条本地
    messages 链跑完。

    Day 4 Task 5 迁移：不再持有 tools dict / 不再手搓执行。工具怎么跑
    （校验/超时/重试/并发）全部下沉到 ToolRegistry + ToolExecutor；
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
        # max_steps 是"模型不收敛时的保险丝"，不是正常业务停止条件。
        # 正常停止由"模型不再返回 tool_calls"决定；max_steps 只兜底。
        self.max_steps = max_steps

    async def run(self, user_input: str) -> AgentRunResult:
        """跑完整条 Agent Loop，返回 AgentRunResult。

        本方法只负责"驱动循环 + 回填消息 + 计数 + 决定停止"；
        工具怎么跑（校验/超时/重试/并发）全部交给 self.executor（ToolExecutor），
        本方法只消费它的 ToolExecution 结果并回填成 ToolMessage。
        """
        # 本地消息链：每次 run 用全新的一条，不跨 run 复用（今天不做 Session/Memory）。
        messages: list[AnyMessage] = [HumanMessage(content=user_input)]
        steps = 0

        # 日志：run 开始。log_event 是否真的写文件，取决于调用方有没有 setup_logging；
        # AgentRuntime 自己不配 logger（职责分离：循环归循环、基础设施归调用方）。
        # 没有 handler 时这些调用是廉价 no-op，不污染测试、不产生垃圾文件。
        run_span = new_span_id()
        self._log("agent_start", "Agent Loop 开始", span_id=run_span, step=0,
                  outcome="started", agent_name="agent_runtime")

        while True:
            # —— 第 1 步：发起这一轮模型调用 ——
            llm_span = new_span_id()
            ai: AIMessage = await self.model.ainvoke(messages)

            # 日志：一轮 LLM 调用结束。llm_output 只取 content 摘要，避免巨量 tool_calls 噪音。
            self._log("llm_call", f"第 {steps + 1} 轮模型调用完成", span_id=llm_span,
                      parent_span_id=run_span, step=steps + 1,
                      llm_input=user_input, llm_output=str(ai.content)[:200],
                      outcome="success")

            # —— 第 2 步：先把 AIMessage 落进消息链 ——
            # 为什么必须在判断 tool_calls 之前 append？
            #   ToolMessage 在协议上必须回答"历史里真实存在的 assistant tool request"。
            #   先落地 AI 请求 -> 再回填结果，顺序反了协议链就断了。
            messages.append(ai)

            # —— 第 3 步：这一轮算一步（数模型轮数，不是工具个数）——
            steps += 1

            # —— 第 4 步：撞到 max_steps 兜底 ——
            # 为什么放这里？此时本轮模型已经被调用过、消息已落地，
            # 但模型仍在请求工具 -> 不收敛 -> 返回明确状态而非继续烧轮数。
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

            # —— 第 5 步：判断这一轮要不要继续 ——
            # 停止信号看什么？ai.tool_calls 是否为空。
            #   content 为空不代表停止（Tool Calling 时 content 允许为空）；
            #   只有"没有 tool_calls"才代表模型选择"给最终答复"。
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
            # —— 第 6 步：用 ToolExecutor 执行整批 tool_call 并回填 ——
            # Day 4 Task 5：不再手搓执行——工具怎么跑（校验/超时/重试/并发）
            # 全部下沉到 ToolExecutor.execute_batch()。这里只消费它的结果并回填。
            #
            # execute_batch 的内存特性（Task 4）：
            #   - 返回顺序 = 输入顺序（不是完成顺序）→ 回填顺序天然稳定；
            #   - 每条结果都带着 tool_call_id → 配对是显式的，不靠数组下标。
            # 例：下方回填处 content 是 ToolResult 的 JSON，
            #   里面 error_code/retryable 是结构化语义，不再是自由字符串。
            #
            # 先记 args_by_id：日志还想带上模型的原始参数（args），但 execute_batch
            # 拿到的是 ToolExecution（参数已被 Executor 消费）。我们按 tool_call_id
            # 留一份原始 args，供日志用；展示层面 metadata 里已有 duration_ms/attempt。
            args_by_id = {tc.get("id", ""): tc.get("args") for tc in tool_calls}
            execs = await self.executor.execute_batch(tool_calls)
            for e in execs:
                result = e.result
                # ToolMessage 的 content = ToolResult 的 JSON。
                # 为什么用 JSON 而不是 str(result)（Day 3 的旧做法）？
                #   - JSON 含结构化字段：ok / message / error_code / retryable / metadata；
                #   - 模型读到的是稳定契约，能按字段语义纠错（参数错就看 error_code）；
                #   - Day 3 的字符串无法区分成功/失败，只能靠人读。
                content = result.model_dump_json()
                # outcome 只看结构化的 result.ok 位：成功 "success" / 失败 "failure"。
                # 不再需要 Day 3 的 try/except 分支——Executor 铁律一保证
                # 任何失败都已固化成失败 ToolResult，这里永远只消费结果不接异常。
                outcome: str = "success" if result.ok else "failure"

                self._log("tool_operation", f"工具回复 {outcome}",
                          span_id=new_span_id(), parent_span_id=run_span, step=steps,
                          tool_call_id=e.tool_call_id,
                          tool_input=args_by_id.get(e.tool_call_id),  # 模型原始参数
                          tool_output=content[:200],
                          error_code=result.error_code,
                          retryable=result.retryable if not result.ok else None,
                          duration_ms=result.metadata.get("duration_ms"),
                          attempt=result.metadata.get("attempt"),
                          timeout_ms=result.metadata.get("timeout_ms"),
                          outcome=outcome)
                # ToolMessage 的 tool_call_id 必须用【原 id】配对——这正是 Task 4
                # execute_batch 保序返回 + 显式 id 配对的兑现点。
                messages.append(ToolMessage(content=content, tool_call_id=e.tool_call_id))

            # 回填完所有 ToolMessage 后，进入下一轮 while（messages 已更新，再喂给模型）。

    def _log(self, event_type: str, message: str, **fields: Any) -> None:
        """打一条结构化日志；无 handler 时静默 no-op（不污染未配日志的调用方/测试）。

        为什么自己判断 handler 而不是直接 log_event？
          log_event 本身会检查 event_type 合法性并组装 extra，但即便没有 handler，
          Python logging 仍会构造 LogRecord（有开销）。我们在测试里高频跑 run()，
          用 hasHandlers() 短路掉无 handler 的情况，让日志彻底零成本。
          调用方一旦 setup_logging()，handler 就位，日志正常写入 agent.jsonl。
        """
        if not logger.hasHandlers():
            return
        log_event(logger, event_type, message, **fields)