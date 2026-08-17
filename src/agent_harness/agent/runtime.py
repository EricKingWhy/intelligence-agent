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
from collections.abc import Awaitable, Callable
from typing import Any

from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, ToolMessage

from agent_harness.agent.types import (
    STATUS_COMPLETED,
    STATUS_MAX_STEPS_EXCEEDED,
    AgentRunResult,
)
from agent_harness.logging import log_event, new_span_id

logger = logging.getLogger("agent_harness.agent")

# 今天 tools 的形状：name -> 可调用对象。
# 普通函数返回 Any；异步函数返回 Awaitable[Any]。
# Day 4 才升级为正式 ToolRegistry / Schema / Executor，今天先用最朴素的 dict。
ToolCallable = Callable[..., Any] | Callable[..., Awaitable[Any]]


class AgentRuntime:
    """最小透明 Agent Loop。

    构造时就绑定好 model + tools + max_steps；一次 run() 用一条本地 messages 链跑完。
    """

    def __init__(
        self,
        model: Any,
        tools: dict[str, ToolCallable],
        max_steps: int = 20,
    ) -> None:
        self.model = model
        self.tools = tools
        # max_steps 是"模型不收敛时的保险丝"，不是正常业务停止条件。
        # 正常停止由"模型不再返回 tool_calls"决定；max_steps 只兜底。
        self.max_steps = max_steps

    async def run(self, user_input: str) -> AgentRunResult:
        """跑完整条 Agent Loop，返回 AgentRunResult。

        本方法只负责"驱动循环 + 回填消息 + 计数 + 决定停止"；
        工具的实际执行今天用一个内部辅助方法 _exec_tool 完成（下方已实现）。
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
            # —— 第 6 步：串行执行每个 tool_call 并回填 ——
            # 今天按 tool_calls 返回顺序逐个执行，不并发（Day 5+ 才碰并行）。
            for tc in tool_calls:
                # —— Task 3：失败回填 ——
                # 为什么捕获而不让异常冒泡？
                #   工具失败有三种：未知工具名（KeyError）、工具内部异常、参数错误。
                #   生产里这些天天发生；任由它抛会让整个任务死掉，用户体验是"Agent 挂了"。
                #   正确做法：把错误【反馈给模型】，模型看到后常能自我纠错（换工具或直接回答）。
                #
                # 你来写（TODO-T3-EXEC）：
                #   1. try 里调 self._exec_tool(tc) 拿 result
                #   2. except Exception as e: 把错误转成一条错误 ToolMessage 回填
                #      - content 要写清楚【什么工具】【为什么失败】，方便模型纠错
                #      - tool_call_id 必须【原样用 tc["id"]】，和成功回填同样的配对规则
                #   3. 成功和失败最终都要 messages.append(ToolMessage(...))，分支不同只差 content
                #   提示骨架：
                try:
                    result = await self._exec_tool(tc)
                    content = str(result)
                    outcome = "success"
                # —— 工具执行边界必须宽捕获：工具是开放世界（Day 4+
                # 用户可注册任意工具），Runtime 无法预知会抛什么；吞掉并回填给
                # 模型正是本层职责。Bug 型异常（如签名写错）也会经 ToolMessage
                # 暴露给模型/日志，不会被静默掩盖。
                except Exception as e:  # noqa: BLE001
                    content = (
                        f"工具 '{tc['name']}' 执行失败: {type(e).__name__}: {e}。"
                        "请检查工具名和参数后重试，或改用其他方式完成任务。"
                    )
                    outcome = "failure"
                self._log("tool_operation", f"工具 {tc['name']} 执行 {outcome}",
                          span_id=new_span_id(), parent_span_id=run_span, step=steps,
                          tool_name=tc["name"], tool_input=tc.get("args"),
                          tool_output=content[:200], outcome=outcome)
                messages.append(ToolMessage(content=content, tool_call_id=tc["id"]))

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

    async def _exec_tool(self, tool_call: dict) -> Any:
        """按 name 查找并执行单个 tool_call，返回结果（成功）或抛异常（失败）。

        本方法已为你实现：今天只负责"按 name 找到函数并执行"。
        Task 3 才处理"未知工具名 -> 抛 KeyError 被循环捕获回填错误"等失败边界。

        参数取值约定：
        - tool_call["name"]：工具名，用于在 self.tools 里查找；
        - tool_call["args"]：dict，直接 ** 展开作为函数实参。
        """
        fn = self.tools[tool_call["name"]]
        # 同时支持同步函数和 async 函数：今天主要是同步 add，但留 await 兼容异步工具。
        import inspect

        if inspect.iscoroutinefunction(fn):
            return await fn(**tool_call["args"])
        return fn(**tool_call["args"])