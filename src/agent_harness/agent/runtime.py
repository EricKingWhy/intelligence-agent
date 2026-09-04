"""AgentRuntime：把两轮 tool-calling 协议收进一段可读的异步循环。

每轮固定顺序：
    1. messages = await context_builder.build(session)（按预算投影）
    2. 模型调用（ainvoke 一次性 / astream 流式逐 chunk）
    3. session.append(model/completed)（持久化完整 AIMessage）
    4. steps += 1
    5. 若无 tool_calls → 返回最终回答；若 steps >= max_steps → 返回兜底状态；否则执行工具回填进入下一轮

两个入口：
    - run(): 经典一次性调用，返回 AgentRunResult（向后兼容，252 现有测试不破）。
    - run_stream(): Phase 9 流式入口，async iterator 逐条 yield AgentEvent，
      含纯流式信号 model/delta（不持久化）+ 每个持久化 SessionEvent 的镜像。

事件事实源：Session.append 同步写 JSONL；messages list 退化为运行期投影缓存。
Diagnostic Log（_log）保留不动——执行链路观察与 SessionEvent 分层并存。
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from typing import Any

from langchain_core.messages import AIMessage, AIMessageChunk

from agent_harness.agent.types import (
    STATUS_COMPLETED,
    STATUS_CONTEXT_WINDOW_EXCEEDED,
    STATUS_MAX_STEPS_EXCEEDED,
    AgentEvent,
    AgentRunResult,
    to_agent_event,
)
from agent_harness.context.builder import ContextBuilder, ContextWindowExceededError
from agent_harness.context.provider import ContextProvider
from agent_harness.logging import log_event, new_span_id
from agent_harness.memory.writeback import MemoryWriteback
from agent_harness.session import (
    MODEL_COMPLETED,
    MODEL_DELTA,
    MODEL_STARTED,
    RUN_FAILED,
    TOOL_CALL,
    TOOL_RESULT,
    USER_MESSAGE,
    Session,
    SessionEvent,
)
from agent_harness.storage import (
    CheckpointBoundary,
    CheckpointPolicy,
    OnStableBoundary,
    OperationContext,
    SessionMeta,
)
from agent_harness.tooling import ToolExecutor, ToolRegistry

logger = logging.getLogger("agent_harness.agent")


class AgentRuntime:
    """最小透明 Agent Loop。

    构造时绑定 model + registry + executor + max_steps；一次 run()/run_stream() 通过
    Session 驱动 event-sourced 循环。工具执行（校验/超时/重试/并发）下沉到
    ToolExecutor；本类只保留"驱动循环"这一份职责。
    """

    def __init__(
        self,
        model: Any,
        registry: ToolRegistry,
        executor: ToolExecutor,
        max_steps: int = 20,
        *,
        checkpoint_policy: CheckpointPolicy | None = None,
        session_meta_store: Any | None = None,
        context_builder: ContextBuilder | None = None,
        context_providers: list[ContextProvider] | None = None,
        memory_writer: MemoryWriteback | None = None,
    ) -> None:
        self.registry = registry
        self.executor = executor
        # max_steps 是"模型不收敛时的保险丝"，不是正常业务停止条件；
        # 正常停止由"模型不再返回 tool_calls"决定。
        self.max_steps = max_steps
        # Checkpoint seam（ADR-0004 Round 2）：默认策略 OnStableBoundary，
        # 但只有注入了 CheckpointStore 才真正落盘——Core 不被存储强制依赖。
        self._checkpoint_policy = checkpoint_policy or OnStableBoundary(None)
        self._session_meta_store = session_meta_store
        # 使用未绑定工具的原始 Provider 生成摘要，不让摘要调用请求工具。
        self._context_builder = context_builder or ContextBuilder(model, context_providers=context_providers)
        if context_builder is not None and context_providers:
            self._context_builder.context_providers.extend(context_providers)
        self._memory_writer = memory_writer

        # 把 Registry 的工具定义绑定到模型——模型才会知道有哪些工具可选、
        # 并在回复里产出 tool_calls。bind_tools 是 LangChain 的标准接线点。
        # ScriptedModel 没有 bind_tools（测试用剧本直接构造 tool_calls），跳过绑定。
        definitions = registry.export_model_definitions()
        if definitions and hasattr(model, "bind_tools"):
            self.model = model.bind_tools(definitions)
        else:
            self.model = model

        # _last_result 由 _drive 在循环结束时写入，供 run() 同步取用。
        # 流式入口 run_stream 不读它（消费者自己消费事件流）。
        self._last_result: AgentRunResult | None = None

    async def run(self, session: Session, user_input: str) -> AgentRunResult:
        """跑完整条 Agent Loop，返回 AgentRunResult。

        所有交互历史通过 Session 的 append-only SessionEvent 持久化；
        messages list 退化为每轮从事件投影出的运行期缓存。
        用 ainvoke 一次性拿完整 AIMessage（非流式入口，向后兼容）。
        """
        async for _ in self._drive(session, user_input, stream=False):
            pass  # 丢弃流式事件，只要副作用（持久化 + 最终结果）
        # 循环结束后，结果挂在 session 上最后一个 run/completed 事件里。
        # 但为了保持向后兼容的返回值，我们从 drive 里捕获取最后一个 result。
        return self._last_result  # type: ignore[return-value]

    async def run_stream(
        self, session: Session, user_input: str
    ) -> AsyncIterator[AgentEvent]:
        """流式驱动 Agent Loop，逐条 yield AgentEvent。

        与 run() 的区别：用 model.astream() 逐 chunk 流式产出，每个 chunk
        yield 一个 model/delta AgentEvent（不持久化——完整 AIMessage 由
        model/completed 持久化，delta 只是 ephemeral 流式信号）。
        每个被 session.append 持久化的事件，同时 yield 一个镜像 AgentEvent
        （带 seq），让消费者拿到完整的事件流 + 流式 token。

        SSE endpoint 直接消费这个 iterator；前端据此实时渲染。
        """
        async for event in self._drive(session, user_input, stream=True):
            yield event

    async def _drive(
        self, session: Session, user_input: str, *, stream: bool
    ) -> AsyncIterator[AgentEvent]:
        """共享的主循环——run 和 run_stream 的唯一实现，消除重复。

        stream=True 时用 astream + yield model/delta；stream=False 时用 ainvoke。
        每个持久化事件都 yield 镜像 AgentEvent（带 seq）；纯流式信号也 yield。
        循环结束把最终 AgentRunResult 存到 self._last_result 供 run() 取用。
        """
        # 写入 user 消息事件
        memory_event_start = len(session.events)
        user_event = session.append(USER_MESSAGE, {"content": user_input})
        yield to_agent_event(user_event)
        # USER_ACCEPTED 稳定边界：user/message 已持久化。
        await self._save_checkpoint(session, CheckpointBoundary.USER_ACCEPTED)

        run_id = session.begin_run()
        run_started = session.events[-1]  # begin_run append 了 run/started；events 是公开 property
        yield to_agent_event(run_started)
        steps = 0

        run_span = new_span_id()
        self._log("agent_start", "Agent Loop 开始", span_id=run_span, step=0,
                  outcome="started", agent_name="agent_runtime")

        while True:
            # 第 1 步：ContextBuilder 是模型可见投影的唯一入口。
            context_event_start = len(session.events)
            try:
                messages = await self._context_builder.build(session)
            except ContextWindowExceededError as error:
                failed = session.append(
                    RUN_FAILED, {"reason": STATUS_CONTEXT_WINDOW_EXCEEDED, "message": str(error)},
                    run_id=run_id, step_id=steps,
                )
                yield to_agent_event(failed)
                self._last_result = AgentRunResult(
                    status=STATUS_CONTEXT_WINDOW_EXCEEDED, final_text="", steps=steps,
                )
                # 模型在本轮从未被调用：没有可抽取的对话内容，跳过 writeback。
                return
            for event in session.events[context_event_start:]:
                yield to_agent_event(event)

            # 第 2 步：发起这一轮模型调用（按 stream 选 astream/ainvoke）
            llm_span = new_span_id()

            if stream:
                # 流式：逐 chunk yield model/delta，聚合回完整 AIMessage
                yield AgentEvent(
                    type=MODEL_STARTED,
                    data={"step": steps + 1},
                    run_id=run_id, step_id=steps + 1,
                )
                collected: list[AIMessageChunk] = []
                async for chunk in self.model.astream(messages):
                    collected.append(chunk)
                    delta_text = chunk.content if isinstance(chunk.content, str) else str(chunk.content)
                    if delta_text:  # 空 content chunk（纯 tool_calls）不发 delta
                        yield AgentEvent(
                            type=MODEL_DELTA,
                            data={"delta": delta_text},
                            run_id=run_id, step_id=steps + 1,
                        )
                # 聚合 chunks 成完整 AIMessage：用 reduce 风格 + 累加。
                # 空流（模型没吐任何 chunk）退化成空 content。
                if collected:
                    ai: AIMessage = collected[0]
                    for c in collected[1:]:
                        ai = ai + c  # type: ignore[assignment]
                    # 聚合后保证是 AIMessage（AIMessageChunk + AIMessageChunk = AIMessageChunk，
                    # 后续逻辑期望 .tool_calls 属性，chunk 也有，但类型标注对齐成 AIMessage）
                    if not isinstance(ai, AIMessage):
                        ai = AIMessage(content=ai.content, tool_calls=ai.tool_calls)  # type: ignore[arg-type]
                else:
                    ai = AIMessage(content="")
            else:
                ai = await self.model.ainvoke(messages)

            self._log("llm_call", f"第 {steps + 1} 轮模型调用完成", span_id=llm_span,
                      parent_span_id=run_span, step=steps + 1,
                      llm_input=user_input, llm_output=str(ai.content)[:200],
                      outcome="success")

            # 第 3 步：把 AIMessage 持久化为 model/completed 事件
            tool_calls = ai.tool_calls or []
            model_data: dict[str, Any] = {"content": ai.content if isinstance(ai.content, str) else str(ai.content)}
            if tool_calls:
                model_data["tool_calls"] = [
                    {"id": tc.get("id", ""), "name": tc.get("name", ""), "args": tc.get("args", {})}
                    for tc in tool_calls
                ]
            will_execute_tools = bool(tool_calls) and steps + 1 < self.max_steps
            defer_model_event = (
                will_execute_tools and self.executor.tracks_operations
            )
            model_event: SessionEvent | None = None
            if not defer_model_event:
                model_event = session.append(
                    MODEL_COMPLETED,
                    model_data,
                    run_id=run_id,
                    step_id=steps + 1,
                )
                yield to_agent_event(model_event)
                # MODEL_COMPLETED 稳定边界：本轮模型回复已持久化（无 tool_calls 或
                # 无 Ledger 时，model/completed 立即写入，这里直接保存 Checkpoint）。
                await self._save_checkpoint(session, CheckpointBoundary.MODEL_COMPLETED)
            # 第 4 步：这一轮算一步（数模型轮数，不是工具个数）
            steps += 1

            # 第 5 步：先判停止信号——若模型选择最终答复则立即返回。
            if not tool_calls:
                final = ai.content if isinstance(ai.content, str) else str(ai.content)
                self._log("agent_decision", "模型给出最终回答，Agent Loop 完成",
                          span_id=new_span_id(), parent_span_id=run_span, step=steps,
                          decision="finish", remaining_steps=0,
                          reason="本轮无 tool_calls，模型选择直接答复", outcome="success")
                self._log("task_completed", "Agent Loop 正常结束", span_id=run_span,
                          step=steps, outcome="success")
                end_event = session.end_run(run_id, status="completed", final_text=final)
                self._write_memories(session, memory_event_start)
                yield to_agent_event(end_event)
                # FINAL_COMPLETED 稳定边界：Run 正常结束事件已持久化。
                await self._save_checkpoint(session, CheckpointBoundary.FINAL_COMPLETED)
                self._last_result = AgentRunResult(
                    status=STATUS_COMPLETED, final_text=final, steps=steps,
                )
                return

            # 第 6 步：模型仍在请求工具——若已达 max_steps 则兜底返回。
            if steps >= self.max_steps:
                self._log("agent_decision", "模型不收敛，撞 max_steps 兜底",
                          span_id=new_span_id(), parent_span_id=run_span, step=steps,
                          decision="max_steps_exceeded", remaining_steps=0,
                          reason=f"连续 {steps} 轮仍在请求工具，触发保险丝", outcome="success")
                end_event = session.end_run(run_id, status="failed")
                self._write_memories(session, memory_event_start)
                yield to_agent_event(end_event)
                self._last_result = AgentRunResult(
                    status=STATUS_MAX_STEPS_EXCEEDED, final_text="", steps=steps,
                )
                return

            # 第 7 步：用 ToolExecutor 执行整批 tool_call 并按原 id 回填。
            args_by_id = {tc.get("id", ""): tc.get("args") for tc in tool_calls}
            tool_event_start = len(session.events)
            tool_error = None
            try:
                executions = await self.executor.execute_batch(
                    tool_calls,
                    session=session,
                    operation_context=OperationContext(
                        session_id=session.session_id,
                        run_id=run_id,
                        agent_id="default",
                    ),
                )
            except Exception as error:  # noqa: BLE001
                tool_error = error
            for event in session.events[tool_event_start:]:
                yield to_agent_event(event)
            if tool_error is not None:
                raise tool_error
            if defer_model_event:
                model_event = session.append(
                    MODEL_COMPLETED,
                    model_data,
                    run_id=run_id,
                    step_id=steps,
                )
                yield to_agent_event(model_event)
                # MODEL_COMPLETED 稳定边界：延迟写入的 model/completed 已持久化。
                await self._save_checkpoint(session, CheckpointBoundary.MODEL_COMPLETED)
            for execution in executions:
                result = execution.result
                content = result.model_dump_json()
                outcome: str = "success" if result.ok else "failure"

                tc_args = args_by_id.get(execution.tool_call_id, {})
                tc_name = next(
                    (tc.get("name", "") for tc in tool_calls if tc.get("id") == execution.tool_call_id),
                    "",
                )
                call_event = session.append(
                    TOOL_CALL,
                    {"tool_call_id": execution.tool_call_id, "tool_name": tc_name, "args": tc_args},
                    run_id=run_id, step_id=steps,
                )
                yield to_agent_event(call_event)
                result_event = session.append(
                    TOOL_RESULT,
                    {"tool_call_id": execution.tool_call_id, "content": content},
                    run_id=run_id, step_id=steps,
                )
                yield to_agent_event(result_event)

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
            # TOOL_BATCH_COMPLETED 稳定边界：整批 tool_call/result 已回填。
            await self._save_checkpoint(
                session, CheckpointBoundary.TOOL_BATCH_COMPLETED
            )

    def _write_memories(self, session: Session, start: int) -> None:
        if self._memory_writer is not None:
            self._memory_writer.submit(session, session.events[start:])

    async def _save_checkpoint(
        self,
        session: Session,
        boundary_type: CheckpointBoundary,
    ) -> None:
        """在稳定边界调 CheckpointPolicy.maybe_save；并在配了 SessionMetaStore 时
        同步更新 last_checkpoint_seq。

        关键不变量（#28 要求）：checkpoint 保存发生在对应 SessionEvent 已持久化【之后】；
        checkpoint/saved 绝不写入 SessionEvent（它只是存储层恢复辅助）。
        """
        checkpoint = await self._checkpoint_policy.maybe_save(
            session, boundary_type
        )
        if checkpoint is not None and self._session_meta_store is not None:
            try:
                await self._session_meta_store.update_last_checkpoint_seq(
                    session.session_id, checkpoint.event_seq
                )
            except KeyError:
                # SessionMeta 尚未 upsert（首次 run）：惰性补建一行。
                from datetime import UTC, datetime

                await self._session_meta_store.upsert(
                    SessionMeta(
                        session_id=session.session_id,
                        created_at=datetime.now(UTC).isoformat(timespec="milliseconds"),
                        agent_id="default",
                        last_checkpoint_seq=checkpoint.event_seq,
                    )
                )

    def _log(self, event_type: str, message: str, **fields: Any) -> None:
        """打一条结构化日志；无 handler 时静默 no-op（不污染未配日志的调用方/测试）。"""
        if not logger.hasHandlers():
            return
        log_event(logger, event_type, message, **fields)
