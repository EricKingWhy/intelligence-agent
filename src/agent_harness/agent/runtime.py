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
      含持久化 SessionEvent 的镜像 + ADR-0016 起合帧落盘的流式事实
      （model/delta、reasoning/*）+ 纯流式信号 model/started（不持久化）。

事件事实源：Session.append 同步写 JSONL；messages list 退化为运行期投影缓存。
Diagnostic Log（_log）保留不动——执行链路观察与 SessionEvent 分层并存。
"""

from __future__ import annotations

import asyncio
import functools
import logging
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any

from langchain_core.messages import AIMessage, AIMessageChunk

from agent_harness.agent.guards import (
    GuardLevel,
    RepeatedToolFailureGuard,
)
from agent_harness.agent.streaming import BlockStreamer
from agent_harness.agent.types import (
    STATUS_COMPLETED,
    STATUS_CONTEXT_WINDOW_EXCEEDED,
    STATUS_FAILED,
    STATUS_IDENTICAL_TOOL_FAILURE_LOOP,
    STATUS_MAX_STEPS_EXCEEDED,
    AgentEvent,
    AgentRunResult,
    to_agent_event,
)
from agent_harness.context.builder import ContextBuilder, ContextWindowExceededError
from agent_harness.context.provider import ContextProvider
from agent_harness.logging import log_event, new_span_id
from agent_harness.memory.writeback import MemoryWriteback
from agent_harness.model.concurrency import ModelCallGate
from agent_harness.model.failure import (
    PROVIDER_FAILURE_MESSAGES,
    UNCLASSIFIED_FAILURE_MESSAGE,
    classify_provider_failure,
    has_malformed_tool_call_markup,
)
from agent_harness.model.fallback import (
    FallbackPolicy,
    ModelFallbackCoordinator,
    TwoLevelFallbackPolicy,
)
from agent_harness.observability.port import NullTracer, Span, Tracer
from agent_harness.observability.tracer import RunTracer
from agent_harness.prompt import DEFAULT_REGISTRY
from agent_harness.session import (
    CONTEXT_COMPACTED,
    MODEL_COMPLETED,
    MODEL_FAILED,
    MODEL_FALLBACK,
    MODEL_STARTED,
    RUN_FAILED,
    RUN_STARTED,
    TOOL_FAILURE_GUARD,
    USER_MESSAGE,
    Session,
    SessionEvent,
    memory_injected_ids_var,
    run_context_var,
)
from agent_harness.session.event import STEER_APPLIED
from agent_harness.session.queue import SteerRequest, SteerSource
from agent_harness.storage import (
    CheckpointBoundary,
    CheckpointPolicy,
    OnStableBoundary,
    OperationContext,
    SessionMeta,
)
from agent_harness.tooling import ToolCall, ToolExecutor, ToolRegistry

logger = logging.getLogger("agent_harness.agent")

#: 记忆抽取排除的事件类型（ADR-0016 review 修复）：流式增量事实不进
#: MemoryWriteback——reasoning 是 provider 私有思考（隐私边界），text/tool
#: 增量与各自的终态事件（model/completed / tool/result）内容重复。
_MEMORY_EXCLUDED_EVENT_TYPES = frozenset({
    "reasoning/started", "reasoning/delta", "reasoning/completed",
    "reasoning/interrupted", "text/delta", "tool/output_delta",
})


def _usage_from_response(ai: Any) -> dict[str, int] | None:
    """从模型响应如实抽取 token usage；响应没带就返回 None（绝不伪造）。

    负值条目直接丢弃：负 token 数对账无效，入账会污染 usage_total 聚合。
    丢弃遵循"缺失/无效时省略"语义，不是伪造；非数值形状已被 AIMessage 自身
    校验挡在构造期（归因 model 失败，语义正确），到不了这里。

    缓存读取（#200，SDD 03 §162 已声明的 ``cached_tokens`` 兑现）：从
    ``input_token_details`` 取缓存读取量；字段缺失/非数值/负值 ⇒ 键省略
    （**不写 0**——0 会被命中率算成 0% 假话，not_collected 语义才是诚实口径）。

    **两个键名都要认**：langchain 把 OpenAI 的 ``prompt_tokens_details.cached_tokens``
    归一化成 ``cache_read``（实测真链路上 ``input_token_details == {"cache_read": 75}``），
    只有不走归一化的路径才是原始名。历史上只认 ``cached_tokens``，于是真链路恒不采集。
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
    details = meta.get("input_token_details")
    if isinstance(details, dict):
        cached = details.get("cache_read")
        if cached is None:
            cached = details.get("cached_tokens")
        if isinstance(cached, int) and not isinstance(cached, bool) and cached >= 0:
            usage["cached_tokens"] = cached
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


def _extract_reasoning(chunk: Any) -> str:
    """从流式 chunk 抽第三方思考文本（D-B①，ADR-0016 §3.4）。

    ReasoningChatOpenAI 把网关的 delta.reasoning_content 抬进
    additional_kwargs["reasoning_content"]；非字符串/缺失 = 本 chunk 无思考，
    返回空串（调用方据此跳过，零伪造）。
    """
    kwargs = getattr(chunk, "additional_kwargs", None)
    if isinstance(kwargs, dict):
        raw = kwargs.get("reasoning_content")
        if isinstance(raw, str) and raw:
            return raw
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
        readable_message: str | None = None,
    ) -> SessionEvent:
        """模型在途失败/取消 → model/failed，把故障归因到具体一步。

        异常消息可能含 Provider 回显的敏感文本——事件只带类型名（与
        memory/writeback 的脱敏不变量一致），完整消息**与调用栈**只进结构化日志
        （OBS-008：`_log(..., exc_info=True)` 落 `stack_trace(调用栈)`；此前只记类型名，
        排障无从下手）。
        ``readable_message`` 是**已分类故障**的固定可读文案（如内容审查拒绝）——
        调用方只传本项目常量、绝不透传 provider 原文，脱敏边界不变。
        """
        if cancelled:
            message = "model call cancelled"
        else:
            message = readable_message or f"model call failed: {error_type}"
        return self._session.append(
            MODEL_FAILED,
            {"message": message},
            run_id=self.run_id, step_id=step + 1,
        )

    def cancelled_terminal(
        self, *, steps: int, reason: str = "cancelled", trace_id: str | None = None,
        trace_url: str | None = None,
    ) -> SessionEvent | None:
        """取消臂收尾（纯同步、不 yield——生成器关闭中禁止再产出）。

        run 未开始（begin_run 之前被取消）→ None，已写事件保持原样；
        已终结的 run 不补第二条终结（双终结 = 历史不可对账）。
        reason 区分取消来源（02 §17 错误语义分离）：显式 POST /cancel 与
        断连消费 = "cancelled"；孤儿回收 = "orphaned"（ADR-0016 §2.1）。"""
        if self.run_id is None or self._terminal_written:
            return None
        terminal_data: dict[str, Any] = {"reason": reason}
        if self._usage_total:
            # 取消也如实带上 token 消耗（Gap 1 契约，不因取消路径丢账）。
            terminal_data["usage_total"] = dict(self._usage_total)
        terminal_data["trace_id"] = trace_id
        terminal_data["trace_url"] = trace_url
        event = self._session.append(
            RUN_FAILED, terminal_data, run_id=self.run_id, step_id=steps,
        )
        self._terminal_written = True
        return event

    def failure_terminal(
        self, *, steps: int, reason: str | None = None,
        message: str | None = None, trace_id: str | None = None,
        trace_url: str | None = None,
    ) -> SessionEvent | None:
        """异常臂收尾 → run/failed（usage 如实，无数据省略）。单终态约束同上。

        reason 落事件 data（如 identical_tool_failure_loop）——消费者区分失败
        原因，与取消臂的 reason=cancelled 同一语义层。message 是固定可读文案
        （reason+message 成对，与上下文超限路径的 RUN_FAILED 形状一致）。
        本方法不替调用方编原因（两个入参缺省即不落键）；运行期每条失败路径都
        必须带 reason 这件事由**逐路径用例**守，清单见
        docs/adr/0033-run-failure-attribution-surface.md §2.4。
        """
        if self.run_id is None or self._terminal_written:
            return None
        event = self._session.end_run(
            self.run_id, status="failed",
            usage_total=dict(self._usage_total) or None,
            reason=reason,
            message=message,
            trace_id=trace_id,
            trace_url=trace_url,
        )
        self._terminal_written = True
        return event


class _Unset:
    """「调用方没传这个关键字」的哨兵类型（#285 / 残余 R1）。

    `None` 不能兼作哨兵：`compacted_turn_count=None` 是**有意义的实参**（"本轮没有压缩
    发生"），在端口那一侧的调用关键字集合里与"根本没传"是两回事。
    """

    __slots__ = ()


#: 模块级唯一实例（判据用 `is`，不用 `==`——哨兵的身份就是它的全部含义）。
_UNSET = _Unset()


@dataclass
class _Telemetry:
    """一次 run 的观测可变状态单点 owner（#265 / T11 第二切片）。

    收之前：`tracer` 在 `_drive` 局部与臂字段里各存一份（选定后靠 `arms.tracer = tracer`
    同点写回对齐），`ctx_span` / `generation` 是"起/清各两处写"的裸句柄，收口序列在每个
    收尾路径上直呼端口方法。本对象把这些收成**一份**（构造一次、按引用交给使用方，与
    `_RunFinalizer` 同一形状），并守住一条纪律：**句柄不出对象**——调用方只报"哪个阶段
    开始了 / 结束了 / 在途的东西按失败收口"，既不持有也不回传句柄。这样"句柄写在哪里、
    清在哪里"只由本对象的成对方法决定（#247 AC4）——**但这不等于"每个出口都被调用
    到"**，后者见下「收口责任边界」。

    恒定式：本对象的每个方法都是**转发**——调用的端口方法、调用条件与顺序与原调用点
    相同，不加行为、不判空。`context_build_completed` 的调用形状（关键字集合）逐字回到
    #264 之前：成功路径带 `compacted_turn_count`，`close_pending` 与 context 超限臂不带
    （#265 把**经 wrapper 的两处**统一成"一律带关键字"——超限臂随之从裸调变带关键字；
    `close_pending` 一直直呼端口、从不经该参数。那是残余 R1 的口径偏差，由 #285 收口）。

    #285 起：**收口即清口**（超限臂不再例外）。#264 之前那条臂收口后保留句柄，于是同一
    span 会被**第二条收集臂**再收一次——取消臂（"超限 + 消费方在终态帧上断连"）与异常臂
    （"超限臂落终态的 `append` 失败"）两条出口皆然（残余 R2 / R3）。两条出口各有一条
    仓库内用例钉住单次收口：`tests/agent/test_terminal_arms.py`。

    端口实现的选择（`_new_tracer`）与故障保护（`_GuardedTracer`）仍在本对象之外，
    故可选/故障 sink 不拖垮 Core 的性质不变。工具批次的 span 归 ToolExecutor：
    `tracer` 原样转交。

    收口责任边界（按可证伪的方式写）：**收口只经本对象的成对方法**，已收口的句柄在本
    对象里唯一存放。各终结点**何时**调用、取消/异常臂经 `snapshot()` 拿的是哪份副本，
    由 `_drive` 与两个终态臂决定——本对象不保证"每个出口都被调用到"。注意"没被走到"
    与"段内抛错"是两回事：后者由 `_TerminalStages` 逐段兜底收口（残余 R4），前者
    本对象无从保证（那段代码压根没执行）。

    逐段兜底**也不覆盖收尾的全部内容**，边界如实记在这里，免得被读成"收尾已免疫
    一切故障"：一是两段之间的取值（`arms.cancel_reason()`，`runtime.py` 的
    `cancel_reason()`）与终态事件本身的写入仍裸奔，二是兜底**不吞**异常——第一处原样
    在全部段跑完后重抛，只是不再让后面的段陪葬。

    关于那处取值：`cancel_reason()` 本身只是一次委托（`return self.cancel_reason_supplier()
    if self.cancel_reason_supplier else "cancelled"`），它的失败面**等于注入的 supplier 的
    失败面**——`run_stream(cancel_reason_supplier=…)` 是公开参数，当前唯一调用点是 web 的
    lambda（`session/runmanager.py` 里 `return "orphaned" if run.reap_requested else "cancelled"`，
    一次 bool 读），所以**当前**没有失败面；换成会抛的 supplier 则属于未收口的出口
    （登记在 `docs/SDD_TICKET_TRACKER.md` **B-35 残余**第 5 条，收口方式与 `cancel_reason`
    段一致：过 `_TerminalStages`）。
    **这个残余有两轴审查 B 轴给的实测复现**（不是推理）：teardown 注入一个抛
    `RuntimeError` 的 supplier ⇒ 收口段**0 次执行**，`tracer.calls == []`、session 里
    没有 `run/failed`——与修复前 `interrupt_streams()` 抛错的症状**逐条相同**。
    ⇒ 想复现就照 `tests/agent/test_terminal_arms.py` 的 `_ArmsKit` 加一个 throwing
    supplier，别把它当"理论边界"。同批的兜底只捕获 `Exception`（不捕获
    `BaseException`）：`KeyboardInterrupt` / `CancelledError` 会穿透——仓库内无生产者，
    作为边界记在这里。

    边界：`run_span`（诊断日志的根 span id）**不收**——它只在创建时写一次、不进端口，
    没有"起/清两处写"的漂移面，留在 `_drive` 局部（#264 已定的同一条判据）。
    """

    tracer: Tracer = field(default_factory=NullTracer)
    #: 在途句柄：非 None = 有一次尚未收口的观测。未配置观测时是 NullSpan；adapter
    #: 降级时可能是 None——两种情况端口自身都安全（见 observability/port.py）。
    ctx_span: Span | None = None
    generation: Span | None = None

    def run_started(self) -> None:
        self.tracer.run_started()

    def run_completed(
        self, final_text: str, usage_total: dict[str, int] | None = None,
    ) -> None:
        self.tracer.run_completed(final_text, usage_total=usage_total)

    def run_failed(self, reason: str) -> None:
        self.tracer.run_failed(reason)

    @property
    def trace_id(self) -> str | None:
        """本 run 的真实 trace 标识（观测缺席/降级时如实 None，不伪造）。"""
        return self.tracer.trace_id

    @property
    def trace_url(self) -> str | None:
        """与 trace_id 并列的可点击 URL（同一降级模式）。"""
        return self.tracer.trace_url

    def context_build_started(self, *, step: int) -> None:
        self.ctx_span = self.tracer.context_build_started(step=step)

    def context_build_completed(
        self, *, compacted_turn_count: int | None | _Unset = _UNSET,
    ) -> None:
        """收口 context span（**收口即清口**，三条调用路径同一语义）。

        端口调用**无条件**——句柄可能是降级实现的 None，端口自己早退（port.py
        模块 docstring 的句柄契约）。收口后一律置空：清口是"这次观测**经过本对象**
        交代完了"的标记，保留它等于允许第二次收口（残余 R2 / R3 的根因，由 #285 收口）。
        清口只保证"不会收两次"，**不**保证"每个出口都收过一次"——未清不等于已交代：
        收口段可能根本没被走到（run 在进终态臂之前就没了下文）。**段内**抛错与此不同：
        逐段兜底已收口那一路（`_TerminalStages` / 残余 R4），段抛错不再跳过后续段。

        关键字集合逐字回到 #264 之前的形状（残余 R1）：调用方**传了**就带关键字转发
        （`None` 也带——那是"本轮没有压缩发生"的实参，与"没传"不同），**没传**就裸调。
        故缺省值是哨兵 `_UNSET` 而不是 `None`：经本对象的两个调用点里，成功路径在值域内、
        context 超限臂在值域外，一个参数同时表达"两个形状"（`close_pending` 不过这里，
        它直呼端口）。
        """
        if compacted_turn_count is _UNSET:
            self.tracer.context_build_completed(self.ctx_span)
        else:
            self.tracer.context_build_completed(
                self.ctx_span, compacted_turn_count=compacted_turn_count,
            )
        self.ctx_span = None

    def model_call_started(self, *, step: int, messages: Any, model: str | None = None) -> None:
        self.generation = self.tracer.model_call_started(
            step=step, messages=messages, model=model,
        )

    def model_call_completed(
        self, *, output_text: str,
        usage: dict[str, int] | None = None,
        duration_ms: int | None = None,
        finish_reason: str | None = None,
        provider_request_id: str | None = None,
        response_model: str | None = None,
        fallback_transitions: list[Any] | None = None,
        tool_call_names: list[str] | None = None,
    ) -> None:
        self.tracer.model_call_completed(
            self.generation, output_text=output_text, usage=usage,
            duration_ms=duration_ms, finish_reason=finish_reason,
            provider_request_id=provider_request_id, response_model=response_model,
            fallback_transitions=fallback_transitions, tool_call_names=tool_call_names,
        )
        self.generation = None

    def close_pending(
        self, *, error_type: str | None, reason: str, cancelled: bool = False,
    ) -> None:
        """取消臂 / 异常臂的观测收口：在途 ctx_span → 在途 generation → run_failed。

        三条调用的**条件与顺序**逐字保留（原有实现的形状，本票不改）：在途句柄
        才调端口——与成功路径的无条件调用不同，那是原臂的既有语义；取消归因为
        "cancelled"而不是异常类型名。
        """
        if self.ctx_span is not None:
            self.tracer.context_build_completed(self.ctx_span)
            self.ctx_span = None
        if self.generation is not None:
            self.tracer.model_call_failed(
                self.generation,
                error_type=("cancelled" if cancelled else error_type),
            )
            self.generation = None
        self.tracer.run_failed(reason)

    def snapshot(self) -> _Telemetry:
        """收口快照（#264 纪律）：值取一份交给收尾上下文，收口只置空快照自己那份。

        臂上的活值不动——臂写完即 return，回写没有读者；写回反而会掩盖"谁拥有
        这两个句柄"（`tests/agent/test_terminal_arms.py` 的取消臂用例钉住这条）。
        """
        return _Telemetry(
            tracer=self.tracer, ctx_span=self.ctx_span, generation=self.generation,
        )


class _TerminalStages:
    """终态收尾的**逐段兜底**执行器（残余 R4，2026-09-22）。

    收尾此前是直排的：任何一段抛错，它**后面**的段整个不执行。最刺眼的一条是
    `interrupt_streams()` 抛错（streamer 收口失败 / 切换事实落盘失败）时观测收口
    （context span、generation、`run_failed`）**一次都没发生**，run 也拿不到终态事件
    ——与 R2/R3 同族：出口覆盖不齐（B-34 段登记的 R4）。

    本对象把"每段独立兜底"收成一个点：段抛错 ⇒ 记一条结构化日志（类型 + 文本；
    不静默）并继续跑后续段；**第一处异常**在全部段跑完后由 `raise_first()` 原样再抛
    ——不吞、不改类型、不换异常，调用方看到的失败与修前同源，只是收尾不再半途而废。

    被保护的是"收口段"（`interrupt_streams` / `close_observability`）；终态事件本身的
    写入与段间取值不在其列——每一处**逐条列在对应臂的 docstring 里**（取消臂 / 异常臂
    各有一段"不在保护面内的两处"），那是读者该看的地方，这里不重复。

    两条臂都接了这个执行器，但**不是同一段代码**：取消臂逐段直呼，异常臂在段之间把
    收口产生的事件逐条 yield 给流消费者。所以"某条出口修好了"不能由另一条臂的用例
    代替——两边各有用例钉住（`tests/agent/test_terminal_arms.py`）。

    契约：`step` 必须返回**当场物化**的 list（两段都是普通方法、返回 list）。若将来
    某段改成生成器 / 惰性迭代，抛错就发生在调用方的 `for` 里，本兜底接不住。
    边界：`except Exception`——`BaseException`（`KeyboardInterrupt` / `CancelledError`）
    按 Python 惯例穿透，不在兜底面内。仓库内两段收口都是同步普通方法（不 await），
    没有这条路径的生产者；两轴审查 B 轴把它记为已知边界而非缺陷。
    """

    def __init__(self) -> None:
        self.first: Exception | None = None

    def run(self, stage: str, step: Callable[[], list[SessionEvent]]) -> list[SessionEvent]:
        """跑一段收尾；抛错 ⇒ 记日志、返回空、**不**中断后续段。"""
        try:
            return step()
        except Exception as error:  # noqa: BLE001 — 收尾段故障边界（见类 docstring）
            if self.first is None:
                self.first = error
            log_event(
                logger, "system_log", f"终态收尾段 {stage} 失败（已继续执行后续段）",
                level="warn", component="agent_runtime", outcome="stage_failed",
                stage=stage, error_type=type(error).__name__, error_message=str(error),
            )
            return []

    def raise_first(self) -> None:
        """全部段跑完后原样再抛第一处异常（没有失败则什么都不做）。"""
        if self.first is not None:
            raise self.first


@dataclass
class _TerminalContext:
    """取消臂 / 异常臂共享的收尾上下文（架构候选 1）。

    两条臂历史上各自维护近重复的收尾序列（streamer 收口 → 切换事实落盘 →
    model/failed 归因 → tracer 收口），差异只有「能否 yield」与 reason 取值。
    收拢到本对象后，重复收尾由单点方法执行，两条臂只决定 yield 策略。

    两个收尾方法都返回本次追加的持久化事件列表（按 append 顺序），调用方决定
    逐条镜像给流消费者（异常臂）还是丢弃（取消臂——生成器关闭中禁止 yield）。
    """

    session: Session
    run_id: str | None
    steps: int
    terminal: _RunFinalizer
    streamer: BlockStreamer | None
    model_coord: ModelFallbackCoordinator
    #: 观测收口用的**快照**（见 `_Telemetry.snapshot`）：收尾上下文不持活值。
    telemetry: _Telemetry

    def interrupt_streams(self) -> list[SessionEvent]:
        """流式块收口 + 切换事实落盘（取消臂与异常臂同一不变量）。

        顺序固定：interrupted（块级部分内容保留）先于 model/failed（调用级归因）；
        drain 幂等——成功路径已取走则此处为空。
        """
        events: list[SessionEvent] = []
        if self.streamer is not None:
            # 取消臂忽略返回值（生成器关闭中禁止 yield）；异常臂逐条镜像。
            events.extend(self.streamer.interrupt(step=self.steps + 1))
        for transition in self.model_coord.drain_transitions():
            events.append(self.session.append(
                MODEL_FALLBACK,
                {"from_model": transition.from_model,
                 "to_model": transition.to_model,
                 "reason": transition.reason},
                run_id=self.run_id, step_id=self.steps + 1,
            ))
        return events

    def close_observability(
        self, *, error_type: str | None, reason: str, cancelled: bool = False,
        readable_message: str | None = None,
    ) -> list[SessionEvent]:
        """model/failed 归因 + 观测收口（在途 ctx_span / generation / run_failed）。

        ``cancelled`` 区分取消臂（True）与异常臂（False）的 model/failed 消息；
        ``error_type`` 只落类型名（脱敏不变量）；完整消息与调用栈只进结构化日志
        （见 `_log` 的 `exc_info`，OBS-008）。``readable_message`` 是已分类故障
        的固定可读文案，透传给 model/failed（见 append_model_failed）。
        """
        events: list[SessionEvent] = []
        if self.terminal.model_call_open:
            events.append(self.terminal.append_model_failed(
                step=self.steps, cancelled=cancelled, error_type=error_type,
                readable_message=readable_message,
            ))
        # 观测收口（#249 / #265）：在途句柄的三条调用收在 `_Telemetry.close_pending`
        # 里，本方法只决定调用时点与归因入参。端口恒为对象且已包保护层（见
        # _GuardedTracer）：收口侧既不判空也不兜异常，端口实现的故障不会让调用方
        # 紧随其后的终态事件写不出去。
        self.telemetry.close_pending(
            error_type=error_type, reason=reason, cancelled=cancelled,
        )
        return events


@dataclass
class _TerminalArms:
    """一次 run 的终结臂上下文（#264 / T11 第一切片）：六个终结点共享的收尾输入收成一个对象。

    此前六个终结点（context 超限 / completed / max_steps / 同错熔断硬触发 / 取消 / 顶层异常）
    各自在 `_drive` 里重算同一批输入（run_id、步号、streamer、model_coord、memory 起点…），
    近重复的收尾序列散在同一函数的不同缩进层。本对象是这些输入的**单一存放点**，臂是按它命名
    的方法（`_terminal_*`）——`_drive` 仍是唯一的 loop owner，只决定"走哪条臂 + 何时 return"。

    字段纪律（决定了 _drive 里哪些同名局部变量保留、哪些删除）：

    · **建一次**：session / terminal / usage_total / model_coord / result_holder /
      cancel_reason_supplier —— 构造时传入，此后只读。其中 usage_total 与 terminal 是**同一对象
      引用**（_drive 就地累加 usage、写 `model_call_open` 标志），臂读到的自然是当时值。
      **只收臂真正读的**：`run_span`（日志用的 span id）留在 _drive 的局部变量里——臂一个读者
      都没有，收进来就是死字段。
    · **同点写回**：step_base / memory_event_start / streamer —— _drive 里各自
      **只有一处赋值**，臂在那条语句里同步写回；局部变量保留给模型轮/工具批次继续读（本票 Scope
      lock 不搬那段）。唯一写点 ⇒ 不存在两个真相。`run_id` **不在此列**：owner 是
      `_RunFinalizer.begin_run`（本类只读，见下面的 property），不存第二份。
    · **唯一存放**：telemetry —— `tracer` 与在途句柄（ctx_span / generation）住 `_Telemetry`，
      臂只按引用透传（#265 落地：这三样此前双份存放/双处写——tracer 靠同点写回对齐、句柄"起/清"
      各两处写；收进单点 owner 后**句柄不出对象**，#264 的"唯一存放"从臂字段延续到该对象）。
      收尾要的是**快照**（见 `context()`），不是活值。
    """

    session: Session
    terminal: _RunFinalizer
    usage_total: dict[str, int]
    model_coord: ModelFallbackCoordinator
    result_holder: list[AgentRunResult]
    cancel_reason_supplier: Callable[[], str] | None
    step_base: int = 0
    memory_event_start: int = 0
    telemetry: _Telemetry = field(default_factory=_Telemetry)
    streamer: BlockStreamer | None = None

    @property
    def run_id(self) -> str | None:
        """本 run 的 id（= `_RunFinalizer` 在 begin_run 时记下的那个）。

        不另设字段：run id 既决定 `model/failed` / `run/failed` 挂哪个 run，也决定
        终态臂自己 append 的事件（如 context 超限的 `run/failed`）挂哪个 run——
        两份拷贝一旦只更新一份，就会出现"事件挂在 run-1、终态判定为'没有 run'"。
        """
        return self.terminal.run_id

    def envelope_step(self, steps: int) -> int:
        """信封编号 = `step_base + steps`（session 级唯一递增；表达式只此一处）。

        `steps` 是 run 内轮次计数（每次调用点把**当时**的值传进来），基数口径见 _drive 里
        `step_base = max(session.max_step_id, session.user_turn_count)` 的注释。两半必须都在：
        单轮会话 `step_base == 0` 让这个表达式可被误换成 `steps` 而基线不红（#263 的多轮用例
        专门盯这一点）。
        """
        return self.step_base + steps

    def context(self, steps: int) -> _TerminalContext:
        """取消臂 / 异常臂共享的收尾上下文（两臂字段完全重合 ⇒ 单点转换）。

        观测面取 `_Telemetry.snapshot()`：收口只置空快照自己的在途句柄，臂上的活值
        不动（#264 纪律）。
        """
        return _TerminalContext(
            session=self.session, run_id=self.run_id,
            steps=self.envelope_step(steps), terminal=self.terminal,
            streamer=self.streamer, model_coord=self.model_coord,
            telemetry=self.telemetry.snapshot(),
        )

    def cancel_reason(self) -> str:
        """取消臂的 reason（ADR-0016 §2.1）：宿主据此区分 cancelled / orphaned。"""
        return self.cancel_reason_supplier() if self.cancel_reason_supplier else "cancelled"


class _GuardedTracer:
    """端口实现的外层保护（#249）：实现违约抛异常时，观测故障绝不改写 run 语义。

    包在 ``_new_tracer`` 选定的实现外面——调用点既不判空也不各自兜异常，将来
    新增的调用点自动受保护（不变量 #21：旁路故障不拖垮 Core）。逐次调用独立兜底：
    前一次调用抛错不会让后续收口调用被跳过（run 在观测面上仍有终态）。
    句柄方法不在此列：Core 从不调用句柄方法，句柄只作为不透明凭据原样传回端口。
    """

    def __init__(self, inner: Tracer) -> None:
        self._inner = inner

    def __getattr__(self, name: str) -> Any:
        attribute = getattr(self._inner, name)
        if not callable(attribute):
            return attribute

        @functools.wraps(attribute)
        def _guarded(*args: Any, **kwargs: Any) -> Any:
            try:
                return attribute(*args, **kwargs)
            except Exception as error:  # noqa: BLE001 - 观测故障边界
                log_event(
                    logger, "system_log", "观测端口调用失败（旁路故障，已吞）",
                    level="warn", component="tracer", outcome="swallowed",
                    error_type=type(error).__name__, error_message=str(error),
                )
                return None

        return _guarded


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
        system_prompt: str | None = None,
        memory_writer: MemoryWriteback | None = None,
        failure_guard: RepeatedToolFailureGuard | None = None,
        fallback_model: Any | None = None,
        fallback_policy: FallbackPolicy | None = None,
        primary_model_name: str = "primary",
        fallback_model_name: str = "fallback",
        stream_idle_timeout: float = 0.0,
        stream_total_timeout: float = 0.0,
        model_call_gate: ModelCallGate | None = None,
        agent_id: str = "default",
        observability_sink: Any | None = None,
        steer_source: SteerSource | None = None,
        agent_profile: str = "main",
        dropped_tools: tuple[str, ...] = (),
    ) -> None:
        self.registry = registry
        self.executor = executor
        # steer 注入源（ADR-0030 D2 / §4.3）：None = 不注入，行为逐字不变
        # （CLI 与绝大多数单测走这条路径）。
        self._steer_source = steer_source
        # 同错熔断护栏（ADR-0014 #69）：可选注入；默认每 run 一个新实例
        # （计数不跨 run 累积——每个 run 的循环各自干净起步）。
        self._failure_guard = failure_guard
        # Model Fallback（ADR-0014 决策 14-16）：两级 + FallbackPolicy seam。
        # 切换决策在 policy（瞬时性判断）；本类只编排调用序列并持久化
        # model/fallback 事件。切换状态按 run 独立（见 _drive）。
        self._fallback_policy = fallback_policy or TwoLevelFallbackPolicy()
        self._primary_model_name = primary_model_name
        self._fallback_model_name = fallback_model_name
        # 流式守卫（秒，逐项 ≤0 关闭）：idle=死连接，total=慢滴漏——所有 run
        # 都受保护（无 fallback 时走统一失败兜底），见 model/stall.py。
        self._stream_idle_timeout = stream_idle_timeout
        self._stream_total_timeout = stream_total_timeout
        # 进程级模型并发闸（#89）：assembly 创建，parent 与所有 child 共享
        # 同一引用（全局在飞模型调用数的语义），None = 不加闸。
        self._model_call_gate = model_call_gate
        # 归因身份（ADR-0015 决策 6）：child runtime 落 profile 名——run/started
        # 与 Ledger 条目不再全是 "default"（parent 保持 "default" 向后兼容）。
        self._agent_id = agent_id
        # Langfuse 旁路观测（ADR-0018 D2）：可选注入；None/缺席 = 零开销。
        # 埋点是添加性的：tracer 故障被 sink 边界吞掉，绝不影响 Loop 语义。
        self._observability_sink = observability_sink
        # #198：生效档位与被 tool_scope 剔除的工具名（装配层在 registry 收窄后
        # 计算）。run_config 结构化日志与 run/started 事件的数据源——"模型为什么
        # 说没有 write"必须可从日志回溯，不能靠工具集形状反推。默认 "main"/空
        # = 添加性（既有调用方与测试零改动）。
        self._agent_profile = agent_profile
        self._dropped_tools = dropped_tools
        # max_steps 是"模型不收敛时的保险丝"，不是正常业务停止条件；
        # 正常停止由"模型不再返回 tool_calls"决定。
        self.max_steps = max_steps
        # Checkpoint seam（ADR-0004 Round 2）：默认策略 OnStableBoundary，
        # 但只有注入了 CheckpointStore 才真正落盘——Core 不被存储强制依赖。
        self._checkpoint_policy = checkpoint_policy or OnStableBoundary(None)
        self._session_meta_store = session_meta_store
        # 使用未绑定工具的原始 Provider 生成摘要，不让摘要调用请求工具。
        # system_prompt（ADR-0020a，agent_profile 运行时消费）：与 context_builder
        # 不同时传——context_builder 是更完整的注入点（调用方自管 system_prompt）；
        # 同时传时 context_builder 优先，system_prompt 被忽略并记一条 warning。
        if context_builder is not None and system_prompt is not None:
            logger.warning(
                "AgentRuntime 同时收到 context_builder 和 system_prompt——"
                "context_builder 优先，system_prompt 被忽略（调用方应在构造 "
                "context_builder 时注入 system_prompt）"
            )
        self._context_builder = context_builder or ContextBuilder(
            model, context_providers=context_providers, system_prompt=system_prompt,
        )
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
        # fallback 模型同样绑定工具：切换后仍能发 tool_calls（否则带工具的
        # 会话切到 fallback 后模型看不到工具，行为静默退化）。
        self._fallback_model: Any | None = None
        if fallback_model is not None:
            if definitions and hasattr(fallback_model, "bind_tools"):
                self._fallback_model = fallback_model.bind_tools(definitions)
            else:
                self._fallback_model = fallback_model

    @property
    def agent_profile(self) -> str:
        """生效档位（#198）：测试断言装配层接线用。"""
        return self._agent_profile

    @property
    def dropped_tools(self) -> tuple[str, ...]:
        """被 tool_scope 剔除的工具名（#198）：测试断言装配层接线用。"""
        return self._dropped_tools

    async def _inject_steers(
        self, session: Session, run_id: str, step_id: int,
    ) -> list[SessionEvent]:
        """把待注入的 steer 追加成本 run 的 user/message（ADR-0030 §4.3）。

        返回**已持久化**的事件列表（调用方逐个 yield 镜像 AgentEvent）——本方法
        只做 append，不负责广播，避免生成器嵌套里再嵌一层 yield。

        三条硬性要求（都有具体故障模式，不是风格问题）：

        1. 用**本 run 自己的** ``session`` 实例 append：两个 Session 各自推算 seq
           会撞号，写出重复 seq 让会话不可 resume。
        2. steer 消息带 ``steer_id``、**绝不**带 ``injected_by``：后者是"runtime
           注入的文案"，标记它会让用户自己的话被记忆抽取排除
           （`memory/extractor.py`）并从 `user_turn_count` 里漏计。
        3. 陈旧请求（run_id 不匹配 / run_id 未知）**丢弃并记日志**，不注入——给
           错误的 run 注入等于让用户的话出现在无关的上下文里；丢弃是安全的，
           因为该 `steer/requested` 仍未被 `steer/applied` 收口，终态驱动会把它
           当普通输入投递（不变量 #7：事件还在，投递晚一点而已）。
        """
        if self._steer_source is None:  # pragma: no cover - 调用方已判
            return []
        drained = await self._steer_source.drain_steers(session.session_id)
        if not drained:
            return []
        appended: list[SessionEvent] = []
        for steer in self._applicable_steers(drained, run_id):
            user_event = session.append(
                USER_MESSAGE,
                {"content": steer.content, "steer_id": steer.steer_id},
                run_id=run_id, step_id=step_id,
            )
            applied = session.append(
                STEER_APPLIED,
                {"steer_id": steer.steer_id, "applied_seq": user_event.seq, "run_id": run_id},
                run_id=run_id, step_id=step_id,
            )
            appended.extend((user_event, applied))
        return appended

    @staticmethod
    def _applicable_steers(
        drained: list[SteerRequest], run_id: str,
    ) -> list[SteerRequest]:
        """筛掉陈旧 steer（顺序保持队列内 FIFO）。"""
        applicable: list[SteerRequest] = []
        for steer in drained:
            if steer.run_id == run_id:
                applicable.append(steer)
            else:
                logger.warning(
                    "丢弃陈旧 steer（steer_id=%s，目标 run=%s，当前 run=%s）"
                    "——它仍未被 steer/applied 收口，由终态驱动当普通输入投递",
                    steer.steer_id, steer.run_id, run_id,
                )
        return applicable

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
        self, session: Session, user_input: str,
        cancel_reason_supplier: Callable[[], str] | None = None,
    ) -> AsyncIterator[AgentEvent]:
        """流式驱动 Agent Loop，逐条 yield AgentEvent。

        与 run() 的区别：用 model.astream() 逐 chunk 流式产出，思考/文本 chunk
        经 BlockStreamer 合帧持久化（ADR-0016：text/delta 与 reasoning/* 是
        durable 事实，断连重连可按 seq 重放恢复）；model/started 保持
        stream-only。每个被 session.append 持久化的事件，同时 yield 一个
        镜像 AgentEvent（带 seq）。

        cancel_reason_supplier（ADR-0016 §2.1）：取消臂收尾时调用来决定
        run/failed 的 reason（"cancelled" / "orphaned"），让 run 的宿主
        （`RunManager`）区分取消来源；None = 默认 "cancelled"。

        SSE endpoint 直接消费这个 iterator；前端据此实时渲染。
        """
        drive = self._drive(
            session, user_input, stream=True,
            cancel_reason_supplier=cancel_reason_supplier,
        )
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
        cancel_reason_supplier: Callable[[], str] | None = None,
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
        # 终态输入收进 _TerminalArms（#264）：终结臂只读它，不再各自重算一遍。
        run_id: str | None = None
        steps = 0
        # session 级 step 基数（前端 turn 定位键的单调性来源）。事件信封的
        # step_id 必须 session 级唯一递增，续聊 run 若再从 1 编号，第二轮的
        # model/* 会与首轮冲突——前端 withTurnAt 折叠进首轮 turn，首轮回答被
        # 清空、次轮回答错位（TICKET_STEP_ID_COLLISION_MULTI_TURN）。
        # 注意 steps 仍是 run 内轮次计数（max_steps 保险丝与 AgentRunResult.steps
        # 依赖它逐 run 从 0 起算），全局编号一律走 step_base + steps。
        step_base = 0
        # 流式块记账（ADR-0016 §3.3）：思考/文本合帧落盘 + reasoning 块生命周期。
        # begin_run 之前异常 = 没有可记账的 run，保持 None。
        streamer: BlockStreamer | None = None
        # 本轮 run 的 token 消耗聚合（Gap 1）：各轮 usage 如实累加，无数据则省略。
        usage_total: dict[str, int] = {}
        # 终态簿记 owner（批次 B 候选 2）：model 在途标记 + 单终态不变量 +
        # usage 记账收拢一处，取消臂/异常臂只做调用。
        terminal = _RunFinalizer(session, usage_total)
        # 同错熔断护栏：每 run 一个新实例（注入或新建）；计数不跨 run 累积。
        guard = self._failure_guard or RepeatedToolFailureGuard()
        guard.reset()
        # run 归因 token：嵌套运行（delegate→child 在同一父任务里跑）共享父
        # 上下文——child 的 set 会覆盖父值且不会随 child 完成消失，必须显式
        # 恢复，否则父后续的 Ledger/事件归因错挂到 child 的 run_id（#87 实锤）。
        run_context_token = None
        # 记忆注入注册表 token（#202 / ADR-0031 D4）：与 run_context_token 同一
        # 初始化点——异常发生在两个 set 之间时 finally 引用未绑定变量会掩盖
        # 原异常（全量回归实证：UnboundLocalError 掩盖 queue 竞态）。
        memory_injected_token = None
        # Model Fallback + 卡流看门狗 + 并发闸：每 run 一个新 coordinator
        # （切换状态不跨 run 共享）。统一调用路径——未配 fallback 时 coordinator
        # 退化为透传（异常原样上抛），但看门狗/并发闸对所有 run 生效。
        model_coord = self._new_coordinator()
        run_span = new_span_id()
        # 观测状态单点 owner（#265）：tracer 在 begin_run 之后选定（见下），在途
        # 句柄（ctx_span / generation）只住这里——_drive 不再留同名局部变量，句柄
        # 也不出该对象（#264 "唯一存放"的落点）。run_span 留在本函数局部：它不可变、
        # 不进端口，没有"起/清两处写"，收进去就是死状态（#264 同一条判据）。
        telemetry = _Telemetry()
        # 终结臂上下文（#264）：run_id / step_base / streamer / memory 起点
        # 在各自的既有唯一写点同步写回本对象（见 _TerminalArms 的字段纪律）。
        arms = _TerminalArms(
            session=session, terminal=terminal, usage_total=usage_total,
            model_coord=model_coord,
            result_holder=result_holder, cancel_reason_supplier=cancel_reason_supplier,
            telemetry=telemetry,
        )
        try:
            # 写入 user 消息事件
            arms.memory_event_start = session.mark()
            # 本 run 的 step 基数 = 前端此刻已分配的 turn 数，取两者较大：
            # max_step_id 覆盖前轮正常产出的步号；user_turn_count 覆盖前轮在
            # 首个 model 事件之前就终结（失败/取消/上下文超限）留下的空轮——
            # 只按 max_step_id 会让这种情况下第二轮再次与首轮撞号。
            # 必须在 append 本轮 user 消息之前算：本轮消息不计入基数。
            step_base = max(session.max_step_id, session.user_turn_count)
            arms.step_base = step_base
            user_event = session.append(USER_MESSAGE, {"content": user_input})
            yield to_agent_event(user_event)
            # USER_ACCEPTED 稳定边界：user/message 已持久化。
            await self._save_checkpoint(session, CheckpointBoundary.USER_ACCEPTED)

            run_id, turn_index = session.begin_run(
                agent_id=self._agent_id, agent_profile=self._agent_profile,
                # #226：请求侧模型标识落 durable 事件（同一 run 内只在 fallback 时换
                # 模型，那次切换另由 model/fallback 记录）。装配层给的
                # primary_model_name = ModelConfig.model_name = 发给 provider 的
                # `model` 值（model/provider.py:107）。取值口径见 ADR-0034。
                model=self._primary_model_name,
            )
            terminal.begin_run(run_id)
            # Langfuse 旁路 trace 根（ADR-0018 D5）：trace=run、session 聚合。
            # 观测端口（#249）恒为对象——缺席实现是 NullTracer，选定与保护见
            # _new_tracer；此处是它的唯一写点（#265 起由 _Telemetry 持有）。
            telemetry.tracer = self._new_tracer(session, run_id, user_input, turn_index)
            telemetry.run_started()
            # 流式块记账绑定本 run（ADR-0016 §3.3）：此后思考/文本 chunk 经
            # streamer 合帧成 durable delta；取消/失败臂负责 interrupt 收口。
            streamer = BlockStreamer(session)
            arms.streamer = streamer
            streamer.begin_run(run_id)
            # run 归因上下文（R3-7）：memory/context provider 等低层模块在
            # 事件降级时需要 run_id 对账，经 contextvar 传递。嵌套运行的恢复
            # 由外层 finally 兜底（token 捕获于下）。
            run_context_token = run_context_var.set(run_id)
            # 本 run 的记忆注入注册表（#202 / ADR-0031 D4）：设空集合，由
            # MemoryContextProvider.select() 在注入时写入；run 收尾 reset。
            memory_injected_token = memory_injected_ids_var.set(frozenset())
            # 按类型选取本 run 的 run/started——不假设 begin_run 恰好只追加一条事件。
            run_started = next(
                e for e in session.since(arms.memory_event_start) if e.type == RUN_STARTED
            )
            yield to_agent_event(run_started)

            # run_config（#198）：每个 run 的运行条件——档位 / 主模型 / 生效工具
            # 清单 / 被剔除工具——落一条结构化日志。此前这些事实不落任何日志，
            # "模型为什么说没有 write"只能靠工具集形状 + system prompt 自述反推
            # （真机会话 f522d4a9 实证）。工具名单只进诊断日志不进事件流（事件
            # 膨胀边界，docs/TICKET_BATCH_PLAN.md §4）；位置在 run_context_var.set
            # 之后：日志行带 session_id（此前 llm_call 只能靠正文指纹检索）。
            self._log("run_config", "Run 运行条件", span_id=run_span, step=0,
                      agent_profile=self._agent_profile,
                      model_id=self._primary_model_name,
                      tool_names=tuple(t.name for t in self.registry.list()),
                      dropped_tools=tuple(self._dropped_tools),
                      session_id=session.session_id)

            self._log("agent_start", "Agent Loop 开始", span_id=run_span, step=0,
                      outcome="started", agent_name="agent_runtime")

            while True:
                # 第 0 步（ADR-0030 D2）：steer 注入。位置固定在 ContextBuilder
                # 之前——那是模型可见投影的唯一入口，注入必须发生在投影之前才
                # 会被本轮模型调用看到；轮次边界（而不是"边答边改"）是物理约束：
                # 已发出的请求无法改写，已流出的 token 收不回来（ADR §1.3）。
                if self._steer_source is not None:
                    for steer_event in await self._inject_steers(
                        session, run_id, step_base + steps,
                    ):
                        yield to_agent_event(steer_event)

                # 第 1 步：ContextBuilder 是模型可见投影的唯一入口。
                context_event_start = session.mark()
                telemetry.context_build_started(step=steps)
                try:
                    messages = await self._context_builder.build(session)
                except ContextWindowExceededError as error:
                    async for streamed in self._terminal_context_exceeded(
                        arms, steps=steps, error=error,
                    ):
                        yield streamed
                    return
                new_events = list(session.since(context_event_start))
                compaction = next(
                    (e for e in new_events if e.type == CONTEXT_COMPACTED), None,
                )
                telemetry.context_build_completed(
                    compacted_turn_count=(
                        compaction.data.get("compacted_turn_count")
                        if compaction is not None else None
                    ),
                )
                for event in new_events:
                    yield to_agent_event(event)

                # 第 2 步：发起这一轮模型调用（按 stream 选 astream/ainvoke）
                llm_span = new_span_id()
                # 计时锚点：供 llm_call 诊断日志带 duration_ms（与 cli.py 的 llm_call 对齐）。
                llm_started = time.perf_counter()
                # generation 句柄（ADR-0018 D7）：与 llm_call 诊断行同源同时点，
                # 完成/失败时回填；异常臂/取消臂负责收口在途 generation。
                telemetry.model_call_started(
                    step=steps + 1, messages=messages,
                    model=self._primary_model_name,
                )

                # 在途标记：从发起调用到聚合完成，此间抛错按 model/failed 归因。
                # coordinator 统一编排（含 stall 看门狗）：瞬时失败内部切换
                # 重试，非瞬时/无 fallback 时异常照常上抛走统一失败兜底。
                terminal.model_call_open = True
                if stream:
                    # 流式：思考/文本 chunk 经 BlockStreamer 合帧落盘（S19），
                    # 聚合回完整 AIMessage。思考块（reasoning/*）与文本 delta
                    # 都是 durable 事实：断连重连按 seq 重放即可恢复（ADR-0016）。
                    yield AgentEvent(
                        type=MODEL_STARTED,
                        data={"step": step_base + steps + 1},
                        run_id=run_id, step_id=step_base + steps + 1,
                    )
                    assert streamer is not None
                    collected: list[AIMessageChunk] = []
                    async for chunk in model_coord.astream(messages):
                        collected.append(chunk)
                        reasoning_text = _extract_reasoning(chunk)
                        if reasoning_text:
                            for streamed in streamer.offer_reasoning(
                                reasoning_text, step=step_base + steps + 1,
                            ):
                                yield to_agent_event(streamed)
                        delta_text = _extract_text(chunk.content)
                        if delta_text:  # 空 content chunk（纯 tool_calls）不发 delta
                            for streamed in streamer.offer_text(
                                delta_text, step=step_base + steps + 1,
                            ):
                                yield to_agent_event(streamed)
                    # 流结束：关思考块（completed）+ 落文本残余（合帧尾部）
                    for streamed in streamer.end_step(step=step_base + steps + 1):
                        yield to_agent_event(streamed)
                    # 聚合 chunks 成完整 AIMessage：用 reduce 风格 + 累加。
                    # 空流（模型没吐任何 chunk）退化成空 content。
                    if collected:
                        ai: AIMessage = collected[0]
                        rest = collected[1:]
                        # 其余 chunk 走 `AIMessageChunk.__add__` 的 **list 形态**（内部即
                        # langchain_core.messages.ai.add_ai_message_chunks）：在 langchain-core
                        # 1.5.4 上与本块原先的逐项 `+` 字段等价（对照用例见
                        # tests/agent/test_stream_chunk_aggregation.py），但累计内容只复制一次
                        # ⇒ O(N·L) → O(L)。逐项折叠每步都要重抄一遍已累计内容，长回答被切成
                        # 数千 chunk 时就是一次同步 CPU 尖峰（#281）。
                        if rest:
                            ai = ai + rest  # type: ignore[assignment]
                        # 聚合后保证是 AIMessage（AIMessageChunk + AIMessageChunk = AIMessageChunk，
                        # 后续逻辑期望 .tool_calls 属性，chunk 也有，但类型标注对齐成 AIMessage）
                        if not isinstance(ai, AIMessage):
                            ai = AIMessage(content=ai.content, tool_calls=ai.tool_calls)  # type: ignore[arg-type]
                    else:
                        ai = AIMessage(content="")
                else:
                    ai = await model_coord.ainvoke(messages)
                # R6-2（用户拍板）：空响应不是成功——content 与 tool_calls 双空
                # 意味着模型没有产出任何决策（内容过滤/上游静默失败）。在途标记
                # 仍开着时抛出，走统一失败兜底（model/failed + run/failed），
                # SSE 客户端因此能区分"模型答了空话"与"上游失败"。
                extracted_content = _extract_text(ai.content)
                if not extracted_content and not ai.tool_calls:
                    raise RuntimeError(
                        "model returned an empty response (no content, no tool calls)"
                    )
                # DSML 协议泄漏守卫（冒烟实测）：无结构化 tool_calls 且 content
                # 含协议保留标记 = 网关没把工具调用解析成结构化字段，绝不能把
                # 这段标记文本当最终回答持久化——与空响应同一失败语义。标记本身
                # 的定义在 model/failure.py。
                if not ai.tool_calls and has_malformed_tool_call_markup(extracted_content):
                    raise RuntimeError(
                        "model response contains malformed tool-call markup"
                        " (DSML protocol leak); treating as model failure"
                    )
                terminal.model_call_open = False  # 调用完整返回，后续异常不再归因 model
                # duration_ms 严格闭合模型调用本身（ainvoke/astream 区间），
                # 不含 normalize / usage 解析等后处理——与 spec 12 §2 的
                # "provider latency" 语义对齐（后处理是微秒级，但注释与字段语义
                # 要自洽）。
                llm_duration_ms = int((time.perf_counter() - llm_started) * 1000)

                # Model Fallback（ADR-0014 决策 18）：取走本步的切换事实；事件
                # 持久化放在下方 llm_log_fields 构造之后、model/completed 之前
                # ——SessionEvent 流里切换事实先于本步完成事件（llm_call 是
                # Diagnostic Log 通道，另一条观察线，不进 JSONL 顺序）。
                fallback_transitions = (
                    model_coord.drain_transitions() if model_coord is not None else []
                )

                # 第 3 步：把 AIMessage 持久化为 model/completed 事件
                # 值对象归一化（A2）：本循环内所有消费点读类型化字段，不再拆原始 dict。
                calls = ToolCall.normalize_all(ai.tool_calls or [])
                tool_calls = calls
                model_data: dict[str, Any] = {"content": extracted_content}
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
                # 切换事实持久化 + 诊断日志归因（ADR-0014 决策 18：llm_call 带
                # fallback_reason/from/to）。usage = 切换后实际产出本步回答的那次
                # 调用的用量（primary 失败一次的用量上游未结账，不可知，绝不
                # 伪造）；run 级 usage_total 统一归集不分主备。
                if fallback_transitions:
                    for transition in fallback_transitions:
                        fallback_event = session.append(
                            MODEL_FALLBACK,
                            {"from_model": transition.from_model,
                             "to_model": transition.to_model,
                             "reason": transition.reason,
                             **({"usage": usage} if usage else {})},
                            run_id=run_id, step_id=step_base + steps + 1,
                        )
                        yield to_agent_event(fallback_event)
                    llm_log_fields.update(
                        fallback_reason=fallback_transitions[0].reason,
                        fallback_from=fallback_transitions[0].from_model,
                        fallback_to=fallback_transitions[0].to_model,
                    )
                self._log("llm_call", f"第 {steps + 1} 轮模型调用完成",
                          span_id=llm_span, parent_span_id=run_span, step=steps + 1,
                          **llm_log_fields)
                # generation 回填（ADR-0018 D7）：与 llm_call 诊断行同源数据——
                # usage/时延/finish_reason/fallback 履历；无数据键省略零伪造。
                response_meta = getattr(ai, "response_metadata", None) or {}
                telemetry.model_call_completed(
                    output_text=extracted_content,
                    usage=usage,
                    duration_ms=llm_duration_ms,
                    finish_reason=response_meta.get("finish_reason"),
                    provider_request_id=response_meta.get("id"),
                    response_model=model_name,
                    fallback_transitions=fallback_transitions,
                    tool_call_names=[c.name for c in calls] if calls else None,
                )
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
                        step_id=step_base + steps + 1,
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
                    async for streamed in self._terminal_completed(
                        arms, steps=steps, final=final,
                    ):
                        yield streamed
                    return

                # 第 6 步：模型仍在请求工具——若已达 max_steps 则兜底返回。
                if steps >= self.max_steps:
                    self._log("agent_decision", "模型不收敛，撞 max_steps 兜底",
                              span_id=new_span_id(), parent_span_id=run_span, step=steps,
                              decision="max_steps_exceeded", remaining_steps=0,
                              reason=f"连续 {steps} 轮仍在请求工具，触发保险丝", outcome="success")
                    # 文案复用上面那行日志的同一句；终态字段由 failure_terminal 单点供给。
                    async for streamed in self._terminal_failed_run(
                        arms, steps=steps, reason=STATUS_MAX_STEPS_EXCEEDED,
                        message=f"连续 {steps} 轮仍在请求工具，触发保险丝",
                    ):
                        yield streamed
                    return

                # 第 7 步：用 ToolExecutor 执行整批 tool_call 并按原 id 回填。
                # ADR-0016 §4.1：tool/call 预持久化（执行前）——02 §8.3 状态机
                # 要求 call 先于 running/output_delta，前端在工具在途期间就有
                # 可关联的行；中断窗口也总是留下可配对修复的 call 事实。
                for call in calls:
                    call_event = self.executor.emit_call_event(
                        session, tool_call_id=call.id, tool_name=call.name,
                        args=call.args, run_id=run_id, step_id=step_base + steps,
                    )
                    yield to_agent_event(call_event)
                tool_event_start = session.mark()
                tool_error = None
                try:
                    executions = await self.executor.execute_batch(
                        calls,
                        # 工具 span 是 ToolExecutor 的职责：观测端口原样转交
                        # （不经 _Telemetry 转发，本对象只管 run 级在途状态）。
                        tracer=telemetry.tracer,
                        session=session,
                        operation_context=OperationContext(
                            session_id=session.session_id,
                            run_id=run_id,
                            agent_id=self._agent_id,
                        ),
                        step_id=step_base + steps,
                    )
                except Exception as error:  # noqa: BLE001
                    tool_error = error
                # 执行期间追加的事件（tool/output_delta 等）镜像给流式消费者；
                # web 订阅者经 session listener 实时收到（seq 幂等合并不重复）。
                for event in session.since(tool_event_start):
                    yield to_agent_event(event)
                if tool_error is not None:
                    raise tool_error
                if defer_model_event:
                    model_event = session.append(
                        MODEL_COMPLETED,
                        model_data,
                        run_id=run_id,
                        step_id=step_base + steps,
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

                    # 持久化顺序（延迟事件 → TOOL_RESULT）的单一 owner 是
                    # ToolExecutor.emit_*（批次 C 候选 3 + ADR-0016 §4.1 拆分）：
                    # Runtime 只消费已持久化事件并镜像，不再自己 append。
                    for persisted_event in self.executor.emit_pending_events(
                        session,
                        pending_events=execution.pending_events,
                        run_id=run_id, step_id=step_base + steps,
                    ):
                        yield to_agent_event(persisted_event)
                    yield to_agent_event(self.executor.emit_result_event(
                        session, tool_call_id=execution.tool_call_id,
                        content=content, run_id=run_id, step_id=step_base + steps,
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

                # ── 同错熔断护栏（ADR-0014 #69）──
                # 工具回填后、下一轮模型调用前观察本轮工具结果；取最严重信号。
                worst_signal = None
                for call, execution in zip(calls, executions):
                    sig = guard.observe(call.name, call.args, execution.result.ok)
                    if sig.level != GuardLevel.NONE and (
                        worst_signal is None or sig.level > worst_signal.level
                    ):
                        worst_signal = sig
                if worst_signal is not None:
                    guard_span = new_span_id()
                    if worst_signal.level == GuardLevel.SOFT:
                        # 软熔断：注入 user 角色纠正消息——护栏是 runtime 行为
                        # 不污染固定 system prompt（ADR-0014 决策 4）。
                        soft_event = session.append(
                            TOOL_FAILURE_GUARD,
                            {"level": "soft",
                             "tool_name": worst_signal.tool_name,
                             "fingerprint": worst_signal.fingerprint,
                             "consecutive_failures": worst_signal.consecutive_failures},
                            run_id=run_id, step_id=step_base + steps,
                        )
                        yield to_agent_event(soft_event)
                        corrective = session.append(
                            USER_MESSAGE,
                            {"content": DEFAULT_REGISTRY.assemble(
                                "corrective:tool_failure_guard",
                                {
                                    "tool_name": worst_signal.tool_name,
                                    "consecutive_failures": str(
                                        worst_signal.consecutive_failures
                                    ),
                                },
                            ).fragment_text,
                             # runtime 注入的纠正消息不是真实用户发言——标记来源
                             # 供前端投影/审计区分（不变量 #22 边缘），**并供记忆
                             # 抽取剔除**（memory/extractor.py 按此标记单点过滤：
                             # 注入消息一旦被当成真实用户发言，工具输出里的注入指令
                             # 就能被洗成跨会话 USER 记忆）。
                             "injected_by": "tool_failure_guard"},
                            run_id=run_id, step_id=step_base + steps,
                        )
                        yield to_agent_event(corrective)
                        self._log("agent_decision", "同错熔断软触发",
                                  span_id=guard_span, parent_span_id=run_span, step=steps,
                                  decision="tool_failure_guard_soft",
                                  tool_name=worst_signal.tool_name,
                                  consecutive_failures=worst_signal.consecutive_failures)
                    elif worst_signal.level == GuardLevel.HARD:
                        # 硬熔断：强制 end_run(failed)，绝不伪造最终回答。
                        hard_event = session.append(
                            TOOL_FAILURE_GUARD,
                            {"level": "hard",
                             "tool_name": worst_signal.tool_name,
                             "fingerprint": worst_signal.fingerprint,
                             "consecutive_failures": worst_signal.consecutive_failures},
                            run_id=run_id, step_id=step_base + steps,
                        )
                        yield to_agent_event(hard_event)
                        self._log("agent_decision", "同错熔断硬触发，强制终止 run",
                                  span_id=guard_span, parent_span_id=run_span, step=steps,
                                  decision="tool_failure_guard_hard",
                                  tool_name=worst_signal.tool_name,
                                  consecutive_failures=worst_signal.consecutive_failures,
                                  outcome="failed")
                        # 与异常臂同一收尾语义（_RunFinalizer 单终态 owner），
                        # reason 落 run/failed data 供消费者区分失败原因。
                        async for streamed in self._terminal_failed_run(
                            arms, steps=steps, reason=STATUS_IDENTICAL_TOOL_FAILURE_LOOP,
                        ):
                            yield streamed
                        return
        except (asyncio.CancelledError, GeneratorExit):
            # 取消臂：客户端断连（SSE 生成器被取消/关闭）走这里——GeneratorExit /
            # CancelledError 是 BaseException，顶层 except Exception 兜不到，
            # durable 日志会永远停在悬空的 run/started 上（无结局的历史，web 层
            # 也不会调 resume 修复）。只做持久化收尾，不 yield：生成器关闭中
            # 禁止再产出（RuntimeError），取消中的 task 再 yield 也会被立即再取消。
            # 收尾后继续向上传播取消——吞掉取消会让 task 无法正确结束。
            try:
                # 收尾事件一律丢弃不 yield（生成器关闭中禁止产出）——这正是本臂
                # 与异常臂的唯一差异，由 _terminal_cancelled 单点执行。
                self._terminal_cancelled(arms, steps=steps)
            except Exception as terminal_error:  # noqa: BLE001
                self._log("task_failed", "取消收尾事件写入失败（存储故障？）",
                          span_id=run_span, outcome="error",
                          error=str(terminal_error),
                          error_type=type(terminal_error).__name__, exc_info=True)
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
                      error_type=type(error).__name__, exc_info=True)
            # 终结事件写入自身也可能失败（例如存储故障）：逐段防护，保证
            # result_holder 一定拿到终态结果——"run() 必返回失败结果"的契约
            # 不因二次故障被破坏。二次失败进日志，不再向上抛。
            try:
                # 本臂允许 yield——收尾事件（部分内容 + interrupted + 切换事实 +
                # model/failed + 终态）逐条镜像给流消费者（与取消臂的唯一差异）。
                async for streamed in self._terminal_exception(arms, steps=steps, error=error):
                    yield streamed
            except Exception as terminal_error:  # noqa: BLE001
                self._log("task_failed", "失败兜底事件写入失败（存储故障？）",
                          span_id=run_span, outcome="error",
                          error=str(terminal_error),
                          error_type=type(terminal_error).__name__, exc_info=True)
            arms.result_holder.append(
                AgentRunResult(status=STATUS_FAILED, final_text="", steps=steps),
            )
            return
        finally:
            # 嵌套运行恢复：child 的 run 归因在 child _drive 结束时还原为父值
            # （或未设），父后续操作不再错挂 child 的 run_id。close/取消路径
            # 同样收口（reset 为同步操作，生成器关闭中安全）。
            if run_context_token is not None:
                try:
                    run_context_var.reset(run_context_token)
                except ValueError:
                    # SSE 消费方可能在【另一上下文】aclose 本生成器（断连路径）
                    # ——token 无法跨上下文 reset。该上下文随任务消亡，无需恢复；
                    # 正常路径（同任务）的 reset 一定成功。
                    pass
            # 记忆注入注册表收口（#202 / ADR-0031 D4）：下一 run 里 injected 全
            # false。与 run_context_token 同一收口窗口；ValueError 语义同上。
            if memory_injected_token is not None:
                try:
                    memory_injected_ids_var.reset(memory_injected_token)
                except ValueError:
                    pass

    # ─── 终结臂（#264 / T11 第一切片）────────────────────────────────────────
    # 六个终结点（context 超限 / completed / max_steps / 同错熔断硬触发 / 取消 /
    # 顶层异常）的收尾序列从 _drive 提到这里；_drive 仍是唯一 loop owner，只决定
    # "走哪条臂 + 何时 return"。每条臂的**顺序与 append 次数**是 #263 基线冻结的
    # 事实（`tests/agent/test_event_sequence_golden.py`），改动会让基线变红——那
    # 正是这份基线的用途，不要为了"顺手统一"改形状。
    # 共同纪律：终态字段由 _RunFinalizer 单点供给；信封编号走 arms.envelope_step()；
    # 取消臂不 yield（生成器关闭中禁止产出），异常臂逐条镜像收尾事件。

    async def _terminal_context_exceeded(
        self, arms: _TerminalArms, *, steps: int, error: ContextWindowExceededError,
    ) -> AsyncIterator[AgentEvent]:
        """context 超限臂：模型在本轮从未被调用，直接落终态（不经 failure_terminal）。"""
        # 裸调（不带 compacted_turn_count）是 #264 之前的调用形状，逐字保留（残余 R1）；
        # 收口即清口 ⇒ 本句柄不会被第二条收集臂再收一次——取消臂（"终态帧上断连"）与
        # 异常臂（"下面这次 append 失败"）两条出口都因此只收一次（#285 修的 R2 / R3，
        # 两条出口各有仓库内用例）。
        arms.telemetry.context_build_completed()
        arms.telemetry.run_failed(STATUS_CONTEXT_WINDOW_EXCEEDED)
        failed = arms.session.append(
            RUN_FAILED,
            {"reason": STATUS_CONTEXT_WINDOW_EXCEEDED, "message": str(error),
             "trace_id": arms.telemetry.trace_id,
             "trace_url": arms.telemetry.trace_url},
            run_id=arms.run_id, step_id=arms.envelope_step(steps),
        )
        arms.terminal.mark_terminal_written()
        yield to_agent_event(failed)
        # 模型在本轮从未被调用：没有可抽取的对话内容，跳过 writeback。
        arms.result_holder.append(
            AgentRunResult(
                status=STATUS_CONTEXT_WINDOW_EXCEEDED, final_text="", steps=steps,
            ),
        )

    async def _terminal_completed(
        self, arms: _TerminalArms, *, steps: int, final: str,
    ) -> AsyncIterator[AgentEvent]:
        """正常完成臂：终态事件 → 记忆抽取 → 镜像 → FINAL_COMPLETED 边界 → 结果。

        镜像（yield）**夹在记忆抽取与 checkpoint 之间**：先后顺序是基线冻结的事实
        （checkpoint 失败被 _save_checkpoint 吞掉，但记忆抽取失败会走异常臂）。
        """
        arms.telemetry.run_completed(final, usage_total=dict(arms.usage_total) or None)
        end_event = arms.session.end_run(
            arms.run_id, status="completed", final_text=final,
            usage_total=dict(arms.usage_total) or None,
            cost_usd=None,   # TODO(spec 12): 费率表未定义，不伪造
            trace_id=arms.telemetry.trace_id,
            trace_url=arms.telemetry.trace_url,
        )
        arms.terminal.mark_terminal_written()
        self._write_memories(arms.session, arms.memory_event_start)
        yield to_agent_event(end_event)
        # FINAL_COMPLETED 稳定边界：Run 正常结束事件已持久化。
        await self._save_checkpoint(arms.session, CheckpointBoundary.FINAL_COMPLETED)
        arms.result_holder.append(
            AgentRunResult(status=STATUS_COMPLETED, final_text=final, steps=steps),
        )

    async def _terminal_failed_run(
        self, arms: _TerminalArms, *, steps: int, reason: str, message: str | None = None,
    ) -> AsyncIterator[AgentEvent]:
        """max_steps / 同错熔断硬触发共用的失败终态（两臂只差 reason 与可读文案）。

        走 failure_terminal（终态字段的唯一 owner）：max_steps 路径原先自己拼
        end_run，于是 #222 之前它**一个归因键都没有**（字段集中供给被绕过 = 下一次
        加字段还会漏它）。`reason` 同时是 AgentRunResult.status——两臂的 status 与
        reason 用的是同一个常量（STATUS_MAX_STEPS_EXCEEDED /
        STATUS_IDENTICAL_TOOL_FAILURE_LOOP），调用点只传一次。
        """
        arms.telemetry.run_failed(reason)
        end_event = arms.terminal.failure_terminal(
            steps=arms.envelope_step(steps),
            reason=reason,
            message=message,
            trace_id=arms.telemetry.trace_id,
            trace_url=arms.telemetry.trace_url,
        )
        self._write_memories(arms.session, arms.memory_event_start)
        if end_event is not None:
            yield to_agent_event(end_event)
        arms.result_holder.append(
            AgentRunResult(status=reason, final_text="", steps=steps),
        )

    def _terminal_cancelled(self, arms: _TerminalArms, *, steps: int) -> None:
        """取消臂收尾（纯同步、不 yield——生成器关闭中禁止再产出）。

        与异常臂的唯一差异是"收尾事件丢弃"：两臂共用 `_TerminalContext` 的收尾
        序列，本臂把返回值直接丢掉。reason 的解析点（supplier 调用）保持在
        `interrupt_streams()` **之后**——与原臂同序。

        逐段兜底（R4）：两段收口任一抛错都不跳过后续——观测收口照跑、终态事件照写，
        第一处异常在最后原样再抛（修前它会让整条收尾链断在这里：有 span 0 次收口、
        也没有 `run/failed`）。

        **不在保护面内的两处**（读这段别当"整条收尾都免疫了"）：`reason` 的取值与
        终态事件本身的写入都在 `stages` 之外——后者抛错的传播形状与修前一致。
        """
        ctx = arms.context(steps)
        stages = _TerminalStages()
        stages.run("interrupt_streams", ctx.interrupt_streams)
        reason = arms.cancel_reason()
        stages.run(
            "close_observability",
            lambda: ctx.close_observability(error_type=None, reason=reason, cancelled=True),
        )
        arms.terminal.cancelled_terminal(
            steps=arms.envelope_step(steps),
            reason=reason,
            trace_id=arms.telemetry.trace_id,
            trace_url=arms.telemetry.trace_url,
        )
        stages.raise_first()

    async def _terminal_exception(
        self, arms: _TerminalArms, *, steps: int, error: BaseException,
    ) -> AsyncIterator[AgentEvent]:
        """顶层异常臂：归因 → 流收口 → model/failed → run/failed（逐条镜像）。

        归因口径（已分类 / 未分类两条支路，以及"每条失败路径都要有值"为什么必须
        做到）见 docs/adr/0033-run-failure-attribution-surface.md §2.1/§2.4。

        逐段兜底（R4）：两段收口任一抛错都不跳过后续——观测收口照跑、终态事件照写，
        第一处异常在全部收尾跑完后原样再抛（修前 streamer 收口一抛错，本臂后面的
        每一行都不执行）。

        **不在保护面内的两处**（读这段别当"整条收尾都免疫了"）：`to_agent_event(...)`
        的 yield 与终态事件本身的写入都在 `stages` 之外——收口段产出的事件在终态事件
        之前 yield，消费方在这一点断连仍是既有窗口（先于 R4 存在）；终态写入抛错的
        传播形状也与修前一致。
        """
        ctx = arms.context(steps)
        # 分类只在**模型调用在途**时进行（model_call_open 正是 model/failed 的
        # 归因窗口）：本臂同时兜底工具/执行器异常，其错误文本可能恰好引用这些
        # 标记（如抓取到阿里云文档或供应商计费文档），不得误标。
        provider_reason = (
            classify_provider_failure(error) if arms.terminal.model_call_open else None
        )
        provider_message = (
            PROVIDER_FAILURE_MESSAGES[provider_reason] if provider_reason else None
        )
        # 终态文案：未分类也必须有可读兜底（#222）。**只喂 run/failed**——
        # model/failed 那侧保持原样（未分类时它是 "model call failed: {type}"，
        # 前端不投影它，见 ADR-0033 §3），两个面各有各的读者。
        terminal_message = (
            provider_message
            or UNCLASSIFIED_FAILURE_MESSAGE.format(error_type=type(error).__name__)
        )
        stages = _TerminalStages()
        for streamed in stages.run("interrupt_streams", ctx.interrupt_streams):
            yield to_agent_event(streamed)
        for streamed in stages.run(
            "close_observability",
            lambda: ctx.close_observability(
                error_type=type(error).__name__,
                reason=provider_reason or type(error).__name__,
                cancelled=False,
                readable_message=provider_message,
            ),
        ):
            yield to_agent_event(streamed)
        # run_id 为 None 说明异常发生在 begin_run 之前：没有 run 可终结，
        # 已写入的事件保持原样，失败只能由日志承载。
        end_event = arms.terminal.failure_terminal(
            steps=arms.envelope_step(steps),
            reason=provider_reason or type(error).__name__,
            message=terminal_message,
            trace_id=arms.telemetry.trace_id,
            trace_url=arms.telemetry.trace_url,
        )
        if end_event is not None:
            yield to_agent_event(end_event)
        stages.raise_first()

    def _new_coordinator(self) -> ModelFallbackCoordinator:
        """per-run coordinator 工厂（_drive 每调一次；测试可直取验证接线）。"""
        return ModelFallbackCoordinator(
            primary=self.model, fallback=self._fallback_model,
            policy=self._fallback_policy,
            primary_name=self._primary_model_name,
            fallback_name=self._fallback_model_name,
            idle_timeout=self._stream_idle_timeout,
            total_timeout=self._stream_total_timeout,
            gate=self._model_call_gate,
        )

    def _new_tracer(
        self, session: Session, run_id: str, user_input: str, turn_index: int,
    ) -> Tracer:
        """观测实现选择（#249）：全 run 唯一一处"观测是否存在"的判据。

        未注入 / 未启用 sink → NullTracer（零外部副作用）；启用 → RunTracer
        （Langfuse adapter，ADR-0018 D5/D7）。返回的实现外面一律包
        ``_GuardedTracer``——调用点既不判空、也不各自兜异常。
        """
        sink = self._observability_sink
        if sink is None or not sink.enabled:
            return _GuardedTracer(NullTracer())
        return _GuardedTracer(RunTracer(
            sink,
            session_id=session.session_id,
            run_id=run_id,
            agent_id=self._agent_id,
            user_input=user_input,
            turn_index=turn_index,
        ))

    def _write_memories(self, session: Session, start: int) -> None:
        if self._memory_writer is not None:
            # 排除流式增量事实（ADR-0016 review 修复）：reasoning/* 是 provider
            # 思考（02 §15 / PRD §18 隐私硬边界——CoT 不得进记忆存储再回灌
            # 上下文）；text/delta 与 tool/output_delta 与 model/completed、
            # tool/result 内容重复，只会挤占抽取器的 50 条事件窗口。
            self._memory_writer.submit(
                session,
                [e for e in session.since(start)
                 if e.type not in _MEMORY_EXCLUDED_EVENT_TYPES],
            )

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

    def _log(self, event_type: str, message: str, *, exc_info: bool = False,
             **fields: Any) -> None:
        """打一条结构化日志；无 handler 时静默 no-op（不污染未配日志的调用方/测试）。

        `exc_info=True`（须在 except 块内调用）让 JSONL 落 `stack_trace(调用栈)`：
        durable 的 `model/failed` 事件按脱敏不变量**只带异常类型名**，完整消息与调用栈
        必须由日志承载；否则排障只剩一个类型名，「哪一帧、哪个 SDK 调用挂的」全丢
        （OBS-008：模型调用失败时 traceback 被吞）。

        ⚠ 调用栈含**绝对路径（含宿主用户名）、源码行、以及链式异常（`__cause__`/
        `__context__`）的消息**——比原先只记的 `error=str(...)` 更多。诊断 JSONL 是
        本地未脱敏详情汇聚处（不变量 #4：Event ≠ Diagnostic Log），但**不要原样附到
        issue / 上传**；需要外发时先脱敏。
        """
        if not logger.hasHandlers():
            return
        log_event(logger, event_type, message, exc_info=exc_info, **fields)
