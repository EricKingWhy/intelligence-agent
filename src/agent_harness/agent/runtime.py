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

import asyncio
import logging
import time
from collections.abc import AsyncIterator
from typing import Any

from langchain_core.messages import AIMessage, AIMessageChunk

from agent_harness.agent.types import (
    STATUS_COMPLETED,
    STATUS_CONTEXT_WINDOW_EXCEEDED,
    STATUS_FAILED,
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
    MODEL_FAILED,
    MODEL_STARTED,
    RUN_FAILED,
    RUN_STARTED,
    USER_MESSAGE,
    Session,
    SessionEvent,
    run_context_var,
)
from agent_harness.storage import (
    CheckpointBoundary,
    CheckpointPolicy,
    OnStableBoundary,
    OperationContext,
    SessionMeta,
)
from agent_harness.tooling import ToolCall, ToolExecutor, ToolRegistry

logger = logging.getLogger("agent_harness.agent")


def _usage_from_response(ai: Any) -> dict[str, int] | None:
    """从模型响应如实抽取 token usage；响应没带就返回 None（绝不伪造）。

    负值条目直接丢弃：负 token 数对账无效，入账会污染 usage_total 聚合。
    丢弃遵循"缺失/无效时省略"语义，不是伪造；非数值形状已被 AIMessage 自身
    校验挡在构造期（归因 model 失败，语义正确），到不了这里。
    """
    meta = getattr(ai, "usage_metadata", None)
    if not isinstance(meta, dict):
        return None
    usage: dict[str, int] = {}
    for source_key, target_key in ({"input_tokens": "prompt_tokens",
                                    "output_tokens": "completion_tokens",
                                    "total_tokens": "total_tokens"}).items():
        value = meta.get(source_key)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            usage[target_key] = value
    return usage or None


def _model_name_from_response(ai: Any) -> str | None:
    """从响应元数据取本次推理的模型名；拿不到就 None，不猜不编。"""
    meta = getattr(ai, "response_metadata", None) or {}
    name = meta.get("model_name") or meta.get("model")
    return name if isinstance(name, str) and name else None


def _extract_text(content: Any) -> str:
    """从模型 content 抽纯文本：str 直通；list（Anthropic 风格块）只拼 type=text 的块。

    绝不用 str(content) 兜底——那会把 Python repr（"[{'type': 'text', ..."）持久化
    进 model/completed / final_text / delta，derive_messages 再把 repr 文本当对话
    回灌给模型。非文本块（tool_use / image 等）与未知形状一律丢弃：宁可少，不可脏。
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            block.get("text", "")
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        )
    return ""


class _RunFinalizer:
    """一次 Run 的终态簿记 owner（批次 B / 架构候选 2）。

    _drive 的取消臂/异常臂此前各自维护近重复的 ~40 行收尾，"一个 run 至多
    一条终态事件"的不变量由散布在 360 行里的布尔旗执行——近期四次 bug 修复
    （1cfe795 终结事件 / 59e4425 checkpoint 不毒化 / 19d7e47 取消耐久 /
    4e47b90 空响应归失败）全落在两条臂的交互上。终态决策收拢到本类后，
    单终态由单点执行，终态收尾可脱离 ScriptedModel 全链单测。

    usage_total 是 _drive 聚合 dict 的引用（不复制）：终结时快照当时账目。
    """

    def __init__(self, session: Session, usage_total: dict[str, int]) -> None:
        self._session = session
        self._usage_total = usage_total
        self.run_id: str | None = None
        self.model_call_open = False
        self._terminal_written = False

    def begin_run(self, run_id: str) -> None:
        self.run_id = run_id

    def mark_terminal_written(self) -> None:
        """成功路径（completed / max-steps）写入终结事件后调用。"""
        self._terminal_written = True

    def append_model_failed(
        self, *, step: int, cancelled: bool, error_type: str | None = None,
    ) -> SessionEvent:
        """模型在途失败/取消 → model/failed，把故障归因到具体一步。

        异常消息可能含 Provider 回显的敏感文本——事件只带类型名（与
        memory/writeback 的脱敏不变量一致），完整消息只进结构化日志。
        """
        if cancelled:
            message = "model call cancelled"
        else:
            message = f"model call failed: {error_type}"
        return self._session.append(
            MODEL_FAILED,
            {"message": message},
            run_id=self.run_id, step_id=step + 1,
        )

    def cancelled_terminal(self, *, steps: int) -> SessionEvent | None:
        """取消臂收尾（纯同步、不 yield——生成器关闭中禁止再产出）。

        run 未开始（begin_run 之前被取消）→ None，已写事件保持原样；
        已终结的 run 不补第二条终结（双终结 = 历史不可对账）。"""
        if self.run_id is None or self._terminal_written:
            return None
        terminal_data: dict[str, Any] = {"reason": "cancelled"}
        if self._usage_total:
            # 取消也如实带上 token 消耗（Gap 1 契约，不因取消路径丢账）。
            terminal_data["usage_total"] = dict(self._usage_total)
        event = self._session.append(
            RUN_FAILED, terminal_data, run_id=self.run_id, step_id=steps,
        )
        self._terminal_written = True
        return event

    def failure_terminal(self, *, steps: int) -> SessionEvent | None:
        """异常臂收尾 → run/failed（usage 如实，无数据省略）。单终态约束同上。"""
        if self.run_id is None or self._terminal_written:
            return None
        event = self._session.end_run(
            self.run_id, status="failed",
            usage_total=dict(self._usage_total) or None,
        )
        self._terminal_written = True
        return event


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
            # 双入口注入按身份去重：同一 provider 实例已在 builder 列表里时跳过
            # ——否则每 build 重复执行（重复注入内容 + 双倍搜索/超时风险）。
            existing_ids = {id(p) for p in self._context_builder.context_providers}
            self._context_builder.context_providers.extend(
                p for p in context_providers if id(p) not in existing_ids
            )
        self._memory_writer = memory_writer

        # 把 Registry 的工具定义绑定到模型——模型才会知道有哪些工具可选、
        # 并在回复里产出 tool_calls。bind_tools 是 LangChain 的标准接线点。
        # ScriptedModel 没有 bind_tools（测试用剧本直接构造 tool_calls），跳过绑定。
        definitions = registry.export_model_definitions()
        if definitions and hasattr(model, "bind_tools"):
            self.model = model.bind_tools(definitions)
        else:
            self.model = model

    async def run(self, session: Session, user_input: str) -> AgentRunResult:
        """跑完整条 Agent Loop，返回 AgentRunResult。

        所有交互历史通过 Session 的 append-only SessionEvent 持久化；
        messages list 退化为每轮从事件投影出的运行期缓存。
        用 ainvoke 一次性拿完整 AIMessage（非流式入口，向后兼容）。
        """
        # 结果经本次调用专属的 holder 回传，不经实例字段——一个 Runtime 并发
        # 跑多个 run 时各拿各的，绝不出现"谁后终结谁生效"的跨 run 串台。
        result_holder: list[AgentRunResult] = []
        async for _ in self._drive(session, user_input, stream=False, result_holder=result_holder):
            pass  # 丢弃流式事件，只要副作用（持久化 + 最终结果）
        return result_holder[-1]

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
        drive = self._drive(session, user_input, stream=True)
        try:
            async for event in drive:
                yield event
        finally:
            # 委托生成器不自动关闭内层（PEP 525）：消费者对 run_stream 直接
            # aclose / GC 时，内层 _drive 收不到 GeneratorExit，取消臂的持久化
            # 收尾（终结悬空 run/started）永远不会执行。这里显式收口——
            # 收尾中禁止再 yield，但允许 await；_drive 的取消臂是纯同步收尾。
            await drive.aclose()

    async def _drive(
        self, session: Session, user_input: str, *, stream: bool,
        result_holder: list[AgentRunResult] | None = None,
    ) -> AsyncIterator[AgentEvent]:
        """共享的主循环——run 和 run_stream 的唯一实现，消除重复。

        stream=True 时用 astream + yield model/delta；stream=False 时用 ainvoke。
        每个持久化事件都 yield 镜像 AgentEvent（带 seq）；纯流式信号也 yield。
        终结时把 AgentRunResult 写进本次调用专属的 result_holder 供 run() 取用
        （不经实例字段：并发 run 共享同一个 Runtime 时结果互不串台）。
        """
        if result_holder is None:
            # run_stream 不读结果；终结路径统一写入载体，调用方各给各的。
            result_holder = []
        # ── 失败兜底所需的安全默认值：run 绝不能永久悬挂在悬空的 run/started 上 ──
        # 异常可能发生在 begin_run 之前（user 持久化 / checkpoint 阶段），此时
        # 没有可终结的 run；run_span / steps / usage_total 同理需要初值。
        run_id: str | None = None
        steps = 0
        # 本轮 run 的 token 消耗聚合（Gap 1）：各轮 usage 如实累加，无数据则省略。
        usage_total: dict[str, int] = {}
        # 终态簿记 owner（批次 B 候选 2）：model 在途标记 + 单终态不变量 +
        # usage 记账收拢一处，取消臂/异常臂只做调用。
        terminal = _RunFinalizer(session, usage_total)
        run_span = new_span_id()
        try:
            # 写入 user 消息事件
            memory_event_start = session.mark()
            user_event = session.append(USER_MESSAGE, {"content": user_input})
            yield to_agent_event(user_event)
            # USER_ACCEPTED 稳定边界：user/message 已持久化。
            await self._save_checkpoint(session, CheckpointBoundary.USER_ACCEPTED)

            run_id = session.begin_run()
            terminal.begin_run(run_id)
            # run 归因上下文（R3-7）：memory/context provider 等低层模块在
            # 事件降级时需要 run_id 对账，经 contextvar 传递（task 作用域，
            # 随请求 task 结束自然消亡），不改变 Provider 协议签名。
            run_context_var.set(run_id)
            # 按类型选取本 run 的 run/started——不假设 begin_run 恰好只追加一条事件。
            run_started = next(e for e in session.since(memory_event_start) if e.type == RUN_STARTED)
            yield to_agent_event(run_started)

            self._log("agent_start", "Agent Loop 开始", span_id=run_span, step=0,
                      outcome="started", agent_name="agent_runtime")

            while True:
                # 第 1 步：ContextBuilder 是模型可见投影的唯一入口。
                context_event_start = session.mark()
                try:
                    messages = await self._context_builder.build(session)
                except ContextWindowExceededError as error:
                    failed = session.append(
                        RUN_FAILED, {"reason": STATUS_CONTEXT_WINDOW_EXCEEDED, "message": str(error)},
                        run_id=run_id, step_id=steps,
                    )
                    terminal.mark_terminal_written()
                    yield to_agent_event(failed)
                    result_holder.append(
                        AgentRunResult(status=STATUS_CONTEXT_WINDOW_EXCEEDED, final_text="", steps=steps),
                    )
                    # 模型在本轮从未被调用：没有可抽取的对话内容，跳过 writeback。
                    return
                for event in session.since(context_event_start):
                    yield to_agent_event(event)

                # 第 2 步：发起这一轮模型调用（按 stream 选 astream/ainvoke）
                llm_span = new_span_id()
                # 计时锚点：供 llm_call 诊断日志带 duration_ms（与 cli.py 的 llm_call 对齐）。
                llm_started = time.perf_counter()

                # 在途标记：从发起调用到聚合完成，此间抛错按 model/failed 归因。
                terminal.model_call_open = True
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
                        delta_text = _extract_text(chunk.content)
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
                # R6-2（用户拍板）：空响应不是成功——content 与 tool_calls 双空
                # 意味着模型没有产出任何决策（内容过滤/上游静默失败）。在途标记
                # 仍开着时抛出，走统一失败兜底（model/failed + run/failed），
                # SSE 客户端因此能区分"模型答了空话"与"上游失败"。
                if not _extract_text(ai.content) and not ai.tool_calls:
                    raise RuntimeError(
                        "model returned an empty response (no content, no tool calls)"
                    )
                terminal.model_call_open = False  # 调用完整返回，后续异常不再归因 model
                # duration_ms 严格闭合模型调用本身（ainvoke/astream 区间），
                # 不含 normalize / usage 解析等后处理——与 spec 12 §2 的
                # "provider latency" 语义对齐（后处理是微秒级，但注释与字段语义
                # 要自洽）。
                llm_duration_ms = int((time.perf_counter() - llm_started) * 1000)

                # 第 3 步：把 AIMessage 持久化为 model/completed 事件
                # 值对象归一化（A2）：本循环内所有消费点读类型化字段，不再拆原始 dict。
                calls = ToolCall.normalize_all(ai.tool_calls or [])
                tool_calls = calls
                model_data: dict[str, Any] = {"content": _extract_text(ai.content)}
                model_name = _model_name_from_response(ai)
                if model_name:
                    model_data["model"] = model_name
                usage = _usage_from_response(ai)
                if usage:
                    model_data["usage"] = usage
                    for key, value in usage.items():
                        usage_total[key] = usage_total.get(key, 0) + value
                # llm_call 诊断日志带模型归因 + 时延 + 用量（与 cli.py 对齐，spec 02 §7/§10
                # 要求每步可在 Diagnostic Log 定位到具体 provider/model）。
                llm_log_fields: dict[str, Any] = {
                    "llm_input": user_input,
                    "llm_output": str(ai.content)[:200],
                    "duration_ms": llm_duration_ms,
                    "outcome": "success",
                }
                if model_name:
                    llm_log_fields["model_id"] = model_name
                if usage:
                    llm_log_fields["token_usage"] = usage
                self._log("llm_call", f"第 {steps + 1} 轮模型调用完成",
                          span_id=llm_span, parent_span_id=run_span, step=steps + 1,
                          **llm_log_fields)
                if tool_calls:
                    model_data["tool_calls"] = [
                        {"id": c.id, "name": c.name, "args": c.args} for c in calls
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
                    final = _extract_text(ai.content)
                    self._log("agent_decision", "模型给出最终回答，Agent Loop 完成",
                              span_id=new_span_id(), parent_span_id=run_span, step=steps,
                              decision="finish", remaining_steps=0,
                              reason="本轮无 tool_calls，模型选择直接答复", outcome="success")
                    self._log("task_completed", "Agent Loop 正常结束", span_id=run_span,
                              step=steps, outcome="success")
                    end_event = session.end_run(run_id, status="completed", final_text=final,
                                                usage_total=dict(usage_total) or None,
                                                cost_usd=None,   # TODO(spec 12): 费率表未定义，不伪造
                                                trace_id=None)   # TODO(Phase 15): Langfuse 接入后填真实 trace
                    terminal.mark_terminal_written()
                    self._write_memories(session, memory_event_start)
                    yield to_agent_event(end_event)
                    # FINAL_COMPLETED 稳定边界：Run 正常结束事件已持久化。
                    await self._save_checkpoint(session, CheckpointBoundary.FINAL_COMPLETED)
                    result_holder.append(
                        AgentRunResult(status=STATUS_COMPLETED, final_text=final, steps=steps),
                    )
                    return

                # 第 6 步：模型仍在请求工具——若已达 max_steps 则兜底返回。
                if steps >= self.max_steps:
                    self._log("agent_decision", "模型不收敛，撞 max_steps 兜底",
                              span_id=new_span_id(), parent_span_id=run_span, step=steps,
                              decision="max_steps_exceeded", remaining_steps=0,
                              reason=f"连续 {steps} 轮仍在请求工具，触发保险丝", outcome="success")
                    end_event = session.end_run(run_id, status="failed",
                                                usage_total=dict(usage_total) or None)
                    terminal.mark_terminal_written()
                    self._write_memories(session, memory_event_start)
                    yield to_agent_event(end_event)
                    result_holder.append(
                        AgentRunResult(status=STATUS_MAX_STEPS_EXCEEDED, final_text="", steps=steps),
                    )
                    return

                # 第 7 步：用 ToolExecutor 执行整批 tool_call 并按原 id 回填。
                tool_event_start = session.mark()
                tool_error = None
                try:
                    executions = await self.executor.execute_batch(
                        calls,
                        session=session,
                        operation_context=OperationContext(
                            session_id=session.session_id,
                            run_id=run_id,
                            agent_id="default",
                        ),
                    )
                except Exception as error:  # noqa: BLE001
                    tool_error = error
                for event in session.since(tool_event_start):
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
                # execute_batch 契约保证返回顺序与输入一致（gather 保序 / 串行补 CANCELLED），
                # 因此按位置配对 call↔execution——空/重复 id 也不会串对。
                for call, execution in zip(calls, executions):
                    result = execution.result
                    content = result.model_dump_json()
                    outcome: str = "success" if result.ok else "failure"

                    # 持久化顺序（TOOL_CALL → 延迟事件 → TOOL_RESULT）的单一
                    # owner 是 ToolExecutor.emit_*（批次 C 候选 3）：Runtime 只
                    # 消费已持久化事件并镜像给流式消费者，不再自己 append——
                    # 此前该顺序在 runtime 与 executor abort flush 各编码一遍。
                    for persisted_event in self.executor.emit_call_events(
                        session,
                        tool_call_id=execution.tool_call_id, tool_name=call.name,
                        args=call.args, pending_events=execution.pending_events,
                        run_id=run_id, step_id=steps,
                    ):
                        yield to_agent_event(persisted_event)
                    yield to_agent_event(self.executor.emit_result_event(
                        session, tool_call_id=execution.tool_call_id,
                        content=content, run_id=run_id, step_id=steps,
                    ))

                    self._log("tool_operation", f"工具回复 {outcome}",
                              span_id=new_span_id(), parent_span_id=run_span, step=steps,
                              tool_call_id=execution.tool_call_id,
                              tool_input=call.args,
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
        except (asyncio.CancelledError, GeneratorExit):
            # 取消臂：客户端断连（SSE 生成器被取消/关闭）走这里——GeneratorExit /
            # CancelledError 是 BaseException，顶层 except Exception 兜不到，
            # durable 日志会永远停在悬空的 run/started 上（无结局的历史，web 层
            # 也不会调 resume 修复）。只做持久化收尾，不 yield：生成器关闭中
            # 禁止再产出（RuntimeError），取消中的 task 再 yield 也会被立即再取消。
            # 收尾后继续向上传播取消——吞掉取消会让 task 无法正确结束。
            try:
                if terminal.model_call_open:
                    terminal.append_model_failed(step=steps, cancelled=True)
                terminal.cancelled_terminal(steps=steps)
            except Exception as terminal_error:  # noqa: BLE001
                self._log("task_failed", "取消收尾事件写入失败（存储故障？）",
                          span_id=run_span, outcome="error",
                          error=str(terminal_error),
                          error_type=type(terminal_error).__name__)
            self._log("task_failed", "Agent Loop 被取消（客户端断连？）",
                      span_id=run_span, outcome="cancelled")
            raise
        except Exception as error:  # noqa: BLE001
            # 顶层失败兜底：模型 / 执行器抛异常时，JSONL 绝不能停在悬空的
            # run/started 上（resume 后是一段没有结局的历史），SSE 消费者也
            # 必须收到终止帧。这里补齐终结事件后正常 return——不向上抛：
            # 失败事实由终结事件 + 结构化日志承载，流干净收尾。
            # task_failed 与正常结束的 task_completed 成对（logging.EVENT_TYPES 白名单）。
            self._log("task_failed", "Agent Loop 异常终止", span_id=run_span,
                      outcome="error", error=str(error),
                      error_type=type(error).__name__)
            # 终结事件写入自身也可能失败（例如存储故障）：逐段防护，保证
            # result_holder 一定拿到终态结果——"run() 必返回失败结果"的契约
            # 不因二次故障被破坏。二次失败进日志，不再向上抛。
            try:
                # 模型调用在途时补 model/failed：把故障归因到具体一步，供 resume /
                # 审计区分"模型故障"与"工具故障"。异常消息可能含 Provider 回显的
                # 敏感文本——事件只带类型名（脱敏不变量），完整消息只进日志。
                if terminal.model_call_open:
                    model_failed = terminal.append_model_failed(
                        step=steps, cancelled=False, error_type=type(error).__name__,
                    )
                    yield to_agent_event(model_failed)
                # run_id 为 None 说明异常发生在 begin_run 之前：没有 run 可终结，
                # 已写入的事件保持原样，失败只能由日志承载。
                end_event = terminal.failure_terminal(steps=steps)
                if end_event is not None:
                    yield to_agent_event(end_event)
            except Exception as terminal_error:  # noqa: BLE001
                self._log("task_failed", "失败兜底事件写入失败（存储故障？）",
                          span_id=run_span, outcome="error",
                          error=str(terminal_error),
                          error_type=type(terminal_error).__name__)
            result_holder.append(
                AgentRunResult(status=STATUS_FAILED, final_text="", steps=steps),
            )
            return

    def _write_memories(self, session: Session, start: int) -> None:
        if self._memory_writer is not None:
            self._memory_writer.submit(session, session.since(start))

    async def _save_checkpoint(
        self,
        session: Session,
        boundary_type: CheckpointBoundary,
    ) -> None:
        """在稳定边界调 CheckpointPolicy.maybe_save；并在配了 SessionMetaStore 时
        同步更新 last_checkpoint_seq。

        关键不变量（#28 要求）：checkpoint 保存发生在对应 SessionEvent 已持久化【之后】；
        checkpoint/saved 绝不写入 SessionEvent（它只是存储层恢复辅助）。

        checkpoint 是恢复辅助（本方法定位即此），绝不能毒化 run 结果：存储故障若
        沿调用链传给顶层失败兜底，会给已持久化 run/completed 的 run 补一条矛盾的
        run/failed（双终结事件，历史不可对账）。这里吞掉异常只落日志，不向上抛。
        """
        try:
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
        except Exception:
            # 宽捕获理由：checkpoint 是恢复辅助，任何存储侧故障都不属于 run 语义。
            logger.exception(
                "checkpoint 保存失败（boundary=%s, session=%s）：不影响 run 结果",
                boundary_type.value,
                session.session_id,
            )

    def _log(self, event_type: str, message: str, **fields: Any) -> None:
        """打一条结构化日志；无 handler 时静默 no-op（不污染未配日志的调用方/测试）。"""
        if not logger.hasHandlers():
            return
        log_event(logger, event_type, message, **fields)
