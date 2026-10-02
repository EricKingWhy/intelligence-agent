"""AgentRuntime：把两轮 tool-calling 协议收进一段可读的异步循环。

每轮固定顺序：
    1. messages = await context_builder.build(session)（按预算投影）
    2. 模型调用（ainvoke 一次性 / astream 流式逐 chunk）
    3. session.append(model/completed)（持久化完整 AIMessage）
    4. steps += 1
    5. 若无 tool_calls → 返回最终回答；若 steps >= max_agent_turns（local fuse）→ 返回兜底状态；否则执行工具回填进入下一轮

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
import json
import logging
import time
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage

from agent_harness.agent.budget import (
    DEFAULT_MAX_AGENT_TURNS,
    SOURCE_DEPLOYMENT,
    LocalFuse,
)
from agent_harness.agent.completion import (
    BLOCK_SOURCE_POLICY,
    BLOCK_SOURCE_QUIESCENCE,
    CompletionPolicy,
    DefaultCompletionPolicy,
    QuiescenceReport,
    collect_quiescence_report,
    policy_rejection_reason,
)
from agent_harness.agent.guards import (
    STUCK_LEVEL_PAUSED,
    STUCK_PATTERN_TOOL_FAILURE,
    RepeatedToolFailureGuard,
    StuckDetector,
    StuckSignal,
    external_failure_signal,
    worst_stuck_signal,
)
from agent_harness.agent.resume_evidence import StuckEvidencePort, steer_applies_to_run
from agent_harness.agent.run_budget import (
    CLOSEOUT_DETERMINISTIC,
    CLOSEOUT_MODEL,
    INT64_MAX,
    REASON_STUCK,
    TRIGGER_MAX_CONTEXT_TOKENS,
    BudgetConsumed,
    LaunchRunBudget,
    RunLimits,
    SessionAdmission,
    SessionBudgetPort,
    SessionBudgetSnapshot,
    _decimal_or_none,
    accounting_unknown_pause_dimensions,
    add_consumed,
    apply_blocked_by,
    as_run_started_budget,
    build_limits_snapshot,
    build_pause_data,
    closeout_capacity,
    consumed_from_events,
    describe_resume_requirements,
    deterministic_continuation,
    normalize_continuation,
    pause_trigger,
    reason_for_dimension,
    session_closeout_capacity,
    stuck_resume_requirements,
    utc_now,
)
from agent_harness.agent.streaming import BlockStreamer
from agent_harness.agent.types import (
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_PAUSED,
    STATUS_QUIESCENCE_BLOCKED,
    AgentEvent,
    AgentRunResult,
    to_agent_event,
)
from agent_harness.context.builder import (
    ContextBuilder,
    ContextWindowExceededError,
    ProtectedFactBudgetExceededError,
)
from agent_harness.context.provider import ContextProvider
from agent_harness.logging import log_event, new_span_id
from agent_harness.memory.writeback import MemoryWriteback
from agent_harness.model.accounting import (
    PROVIDER_ROLE_CLOSEOUT,
    REQUEST_OUTCOME_COMPLETED,
    REQUEST_OUTCOME_FAILED,
    cost_usd_from_response,
)
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
    GUARD_STUCK,
    MODEL_COMPLETED,
    MODEL_FAILED,
    MODEL_FALLBACK,
    MODEL_REQUEST,
    MODEL_STARTED,
    OPERATION_RECONCILE_REQUIRED,
    RUN_FAILED,
    RUN_PAUSED,
    RUN_STARTED,
    TOOL_FAILURE_GUARD,
    USER_MESSAGE,
    Session,
    SessionEvent,
    current_session_var,
    memory_injected_ids_var,
    run_context_var,
)
from agent_harness.session.event import (
    CONTEXT_PROTECTED_FACTS_EXCEEDED,
    STEER_APPLIED,
    TOOL_RESULT,
)
from agent_harness.session.queue import SteerRequest, SteerSource
from agent_harness.storage import (
    CheckpointBoundary,
    CheckpointPolicy,
    OnStableBoundary,
    OperationContext,
    OperationState,
    SessionMeta,
    needs_reconcile,
)
from agent_harness.storage.checkpoint import note_checkpoint_save_failure
from agent_harness.tooling import ToolCall, ToolExecutor, ToolRegistry
from agent_harness.tooling.quota import ToolQuotaWindow

if TYPE_CHECKING:
    # #298 T7b：只借类型，不借实现。`memory.v2.runner` 会连带拉起 jobs / executor /
    # policy / formation 一整套（以及它们的 langchain 依赖），而 Runtime 需要的只是
    # "把这一轮告诉宿主"这一个方法——注解在 `from __future__ import annotations` 下
    # 是惰性的，所以这层依赖在**运行期**不存在。
    from agent_harness.memory.v2.runner import MemoryFormationNotifier

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

    **越界（> `INT64_MAX`）同样丢弃（#552）**：session 账落 SQLite INTEGER 列，
    越界值入库即 `OverflowError`（BUG-R4-02）。该维转**未知**（省略）而非 clamp /
    记 0——`11 §6.1`「不可得 ≠ 0」，audit 明令 MUST NOT clamp。run 级 `usage_total`
    与 session 投影因此**同为未知**，不再 10³⁰ vs 0 分裂。

    缓存读取（#200，SDD 03 §162 已声明的 ``cached_tokens`` 兑现）：从
    ``input_token_details`` 取缓存读取量；字段缺失/非数值/负值 ⇒ 键省略
    （**不写 0**——0 会被命中率算成 0% 假话，not_collected 语义才是诚实口径）。

    **两个键名都要认**：provider 线走 langchain 归一化后是 ``cache_read``，不走
    归一化的路径才是原始名 ``cached_tokens``；成因与取证见
    `docs/design/CONTEXT_CAPACITY_DASHBOARD.md` §3.1。
    """
    meta = getattr(ai, "usage_metadata", None)
    if not isinstance(meta, dict):
        return None
    usage: dict[str, int] = {}
    for source_key, target_key in ({"input_tokens": "prompt_tokens",
                                    "output_tokens": "completion_tokens",
                                    "total_tokens": "total_tokens"}).items():
        value = meta.get(source_key)
        if (
            isinstance(value, int) and not isinstance(value, bool)
            and 0 <= value <= INT64_MAX
        ):
            usage[target_key] = value
    details = meta.get("input_token_details")
    if isinstance(details, dict):
        cached = details.get("cache_read")
        if cached is None:
            cached = details.get("cached_tokens")
        if (
            isinstance(cached, int) and not isinstance(cached, bool)
            and 0 <= cached <= INT64_MAX
        ):
            usage["cached_tokens"] = cached
    return usage or None


def _accumulate_usage(
    total: dict[str, int | None], usage: dict[str, int] | None,
) -> None:
    """把一次响应的 usage 并入 run 级聚合账（`#552` C1）。

    与 session 侧**同口径**（`storage/delegation_tree.py::record_session_model_requests`
    的 `total if total <= INT64_MAX else None`）：**合法值相加溢出 int64** ⇒ 该维
    转未知（`None`，**粘性**）——不 clamp、不记 0（`11 §6.1`：不可得 ≠ 0）。
    此前 run 级用 Python bigint 裸加（不溢出）⇒ 两步各 `2**62` 会停在 `2**63`，
    而同输入下 session 账为 `None`，两本账永久分裂（#552 BUG-R4-02 同一症状）。

    表示选了「该维整维置 `None`（粘性）」而非「从 `usage_total` 省略」：run 级
    聚合账的既有读法就是"该维存在、值不可知 = `None`"——见 `BudgetConsumed`
    （`run_budget.py`：`total_tokens: int | None`、`with_usage` 让 `None` 粘住、
    `as_projection` 仍落该键），与 session 的 `NULL`（列存在、值未知）逐字对齐。
    省略会让下一轮 `get(key, 0)` 把已未知的维**重新从 0 起算**（少算成假账），
    且与"没有这个维度"混淆——那不叫收口。

    粘性：某维一旦未知，后面再精确的加数也补不回缺的那一块（同
    `_RunFinalizer.add_cost` 的 `None` 粘性与 `BudgetConsumed.with_usage`）。
    """
    if not usage:
        return
    for key, value in usage.items():
        current = total.get(key, 0)
        if current is None:
            continue  # 已转未知：粘住，绝不再从 0 起算
        level = current + value
        total[key] = level if level <= INT64_MAX else None


def _model_name_from_response(ai: Any) -> str | None:
    """从响应元数据取本次推理的模型名；拿不到就 None，不猜不编。"""
    meta = getattr(ai, "response_metadata", None) or {}
    name = meta.get("model_name") or meta.get("model")
    return name if isinstance(name, str) and name else None


def _finish_reason_from_response(ai: Any) -> str | None:
    """从响应元数据取 finish_reason；键缺失返回 None，语义交调用方回落。

    刻意不做 ``isinstance``/空串归一：这里必须与 R6-2 守卫的原 inline 读取
    （``.get("finish_reason")`` 原样返回）逐值等价——空串等异常值按原样落到
    守卫的 catch-all 分支，行为零变化由 verbatim 钉住测试背书。
    """
    meta = getattr(ai, "response_metadata", None) or {}
    return meta.get("finish_reason")


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


def _closeout_instruction(
    *, trigger_dimension: str, consumed: BudgetConsumed, limits: RunLimits,
    reason: str | None = None, stuck: Mapping[str, Any] | None = None,
) -> str:
    """暂停前那一次有界 closeout 的指令（`#312`，`#313` 扩到四维，`#317` 加 stuck）。

    指令本身**不**进事件流：持久化的只有它的产出（`run/paused.continuation`）——
    continuation 是"这次暂停的续跑说明书"，不是对话内容，不该被
    `derive_messages` 当成模型可见历史回灌（不变量 #5/#6）。

    账目**未知**的维度如实写"未知"：让模型看见"不知道花了多少 token"比让它看见一个
    假的 0 更安全（它会据此写"剩下的不多了"这类判断）。

    `reason=stuck` 换一段事实描述（ADR-0048 D6）：这次暂停**不是**预算到顶，所以指令里
    不能出现"抬 ceiling"的暗示（模型会照抄进 continuation，而那次恢复必被 409 挡死）。
    """
    if reason == REASON_STUCK:
        payload = dict(stuck or {})
        return (
            "运行即将因**同一个模式反复出现且没有进展**而暂停，"
            "现在需要一份供恢复使用的续跑说明。\n"
            f"重复模式：{payload.get('pattern')}（已到第 "
            f"{_budget_value_text(payload.get('count'))} 次，阈值 "
            f"{_budget_value_text(payload.get('threshold'))}）；"
            f"本逻辑 run 已消耗 {consumed.agent_turns} 轮 / "
            f"{_budget_value_text(consumed.model_requests)} 次请求 / "
            f"{_budget_value_text(consumed.total_tokens)} token / "
            f"${_budget_value_text(consumed.cost_usd)}。\n"
            "这次暂停**不是**预算问题：不要把恢复写成提高额度或延长时间——能解开它的"
            f"只有这些依据：{describe_resume_requirements(payload)}；"
            "写成其余依据的那次恢复会被 409 挡死。\n"
            "只依据上面的会话历史作答，**不要**调用工具、不要推测还没发生的事。\n"
            "只输出一个 JSON 对象（不要代码块、不要多余文字），键固定为：\n"
            '{"completed": ["已确实完成的事"], "remaining": ["还没做完的事"], '
            '"blockers": ["阻塞点"], "next_safe_action": "恢复后第一步该做什么"}\n'
            "四个键都必填；completed/remaining/blockers 是字符串数组（可为空数组）。"
        )
    return (
        "运行即将因回合预算到顶而暂停，现在需要一份供恢复使用的续跑说明。\n"
        f"触发维度：{trigger_dimension}（ceiling="
        f"{_budget_value_text(limits.ceiling_of(trigger_dimension))}）；"
        f"本逻辑 run 已消耗 {consumed.agent_turns} 轮 / "
        f"{_budget_value_text(consumed.model_requests)} 次请求 / "
        f"{_budget_value_text(consumed.total_tokens)} token / "
        f"${_budget_value_text(consumed.cost_usd)}。\n"
        "只依据上面的会话历史作答，**不要**调用工具、不要推测还没发生的事。\n"
        "只输出一个 JSON 对象（不要代码块、不要多余文字），键固定为：\n"
        '{"completed": ["已确实完成的事"], "remaining": ["还没做完的事"], '
        '"blockers": ["阻塞点"], "next_safe_action": "恢复后第一步该做什么"}\n'
        "四个键都必填；completed/remaining/blockers 是字符串数组（可为空数组）。"
    )


def _budget_value_text(value: Any) -> str:
    """账目数值 → closeout 指令里的文案（`None` = 未知，不写成 0）。"""
    if value is None:
        return "未知"
    if isinstance(value, Decimal):
        return format(value, "f")
    return str(value)


def _parse_closeout_json(text: str) -> Any:
    """解析 closeout 的 JSON 产出（容忍 ```json 代码块围栏）；失败返回 `None`。

    只做"取出 JSON"，不做宽容修补——合不合契约由 `normalize_continuation` 判，
    修补一个形状不对的产出等于替模型编 continuation。
    """
    stripped = text.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        stripped = "\n".join(lines).strip()
    try:
        return json.loads(stripped)
    except (ValueError, TypeError):
        return None


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

    def __init__(self, session: Session, usage_total: dict[str, int | None]) -> None:
        self._session = session
        self._usage_total = usage_total
        self.run_id: str | None = None
        self.model_call_open = False
        self._terminal_written = False
        #: 本执行累计的**归属成本**（`#313` T5）。`None` = 至今没有任何一次响应
        #: 自报过成本（或有一次没自报）——即"不可得"，不是 0（`11 §6.1`）。
        self.cost_total: Decimal | None = None

    def add_cost(self, cost: Decimal | None) -> None:
        """记一次响应的 Provider 归属成本（`None` = 这次没有可靠归属）。

        `None` **粘性**：一次不可归属就把整本账标成未知，后面的加数补不回缺的
        那一块（"总和 = 已知部分 + 未知部分"，后者不可知）。所以生产链路
        （`reports_cost=False`）的终态 `cost_usd` 恒为 `None`——那是实话，
        不是"这个 run 不要钱"。
        """
        if cost is None:
            self.cost_total = None
            return
        self.cost_total = (self.cost_total or Decimal(0)) + cost

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
        primary_error: str | None = None, fallback_error: str | None = None,
    ) -> SessionEvent | None:
        """异常臂收尾 → run/failed（usage 如实，无数据省略）。单终态约束同上。

        reason 落事件 data（如 identical_tool_failure_loop）——消费者区分失败
        原因，与取消臂的 reason=cancelled 同一语义层。message 是固定可读文案
        （reason+message 成对，与上下文超限路径的 RUN_FAILED 形状一致）。
        `primary_error` / `fallback_error` 只在**同一决策两级都失败**时给（#551
        M10-8），同样是类型名、不带正文。本方法不替调用方编原因（两个入参缺省即
        不落键）；运行期每条失败路径都必须带 reason 这件事由**逐路径用例**守，清单见
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
            primary_error=primary_error,
            fallback_error=fallback_error,
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
        """流式块收口 + 切换事实与请求账目落盘（取消臂与异常臂同一不变量）。

        顺序固定：interrupted（块级部分内容保留）先于 model/fallback 与 model/request
        （调用级归因）先于 model/failed；drain 幂等——成功路径已取走则此处为空。
        """
        events: list[SessionEvent] = []
        if self.streamer is not None:
            # 取消臂忽略返回值（生成器关闭中禁止 yield）；异常臂逐条镜像。
            events.extend(self.streamer.interrupt(step=self.steps + 1))
        for transition in self.model_coord.drain_transitions():
            events.append(self.session.append(
                MODEL_FALLBACK,
                transition.event_data(),
                run_id=self.run_id, step_id=self.steps + 1,
            ))
        # `#313`：调用失败/取消时，**已经发出去**的请求同样要落账——它们占
        # `model_requests` 一席（`02 §5.1`），只是没有产出决策（不增 agent_turns）。
        # 成功路径在这里 drain 到空（上面已取走并落盘），所以本循环是幂等的。
        for request in self.model_coord.drain_requests():
            events.append(self.session.append(
                MODEL_REQUEST,
                {"role": request.role, "outcome": request.outcome},
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

    此前六个终结点（context 超限 / completed / local fuse / 同错熔断硬触发 / 取消 / 顶层异常）
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
    #: 值 `None` = 该维累加溢出 int64 后转未知（`#552` C1，粘性）——见 `_accumulate_usage`。
    usage_total: dict[str, int | None]
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

    构造时绑定 model + registry + executor + max_agent_turns；一次 run()/run_stream() 通过
    Session 驱动 event-sourced 循环。工具执行（校验/超时/重试/并发）下沉到
    ToolExecutor；本类只保留"驱动循环"这一份职责。

    `max_agent_turns` 是 **local fuse**（`02 §5.1` 第一层，ADR-0044 D1）：一个实例的
    高位保险丝，**不跨兄弟池化**。生效值的解析（Deployment → AgentProfile → 请求覆盖）
    在 `agent_harness.agent.budget.resolve_local_fuse`——本类只消费解析结果，不做策略判断。
    """

    def __init__(
        self,
        model: Any,
        registry: ToolRegistry,
        executor: ToolExecutor,
        max_agent_turns: int = DEFAULT_MAX_AGENT_TURNS,
        *,
        checkpoint_policy: CheckpointPolicy | None = None,
        session_meta_store: Any | None = None,
        context_builder: ContextBuilder | None = None,
        context_providers: list[ContextProvider] | None = None,
        system_prompt: str | None = None,
        memory_writer: MemoryWriteback | None = None,
        memory_formation: MemoryFormationNotifier | None = None,
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
        run_budget: LaunchRunBudget | None = None,
        local_fuse_source: str = SOURCE_DEPLOYMENT,
        completion_policy: CompletionPolicy | None = None,
        stuck_evidence: StuckEvidencePort | None = None,
        session_budget: SessionBudgetPort | None = None,
    ) -> None:
        self.registry = registry
        self.executor = executor
        # steer 注入源（ADR-0030 D2 / §4.3）：None = 不注入，行为逐字不变
        # （CLI 与绝大多数单测走这条路径）。
        self._steer_source = steer_source
        # 同错熔断护栏（ADR-0014 #69）：可选注入；默认每 run 一个新实例
        # （计数不跨 run 累积——每个 run 的循环各自干净起步）。
        # `#317` 起它**只是** ①（同动作同错误）那一档的引擎，状态由 `StuckDetector`
        # 重放事件重建（见那里的 docstring）：注入它的意义从"带状态"变成"换阈值"。
        self._failure_guard = failure_guard
        # stuck 暂停的恢复依据端口（`#317`；ADR-0048 D8）：只在**暂停那一刻**读一次
        # （`current()`），None = 观测不到 ⇒ 环境 / 策略两条依据不可用（fail-closed，
        # `stuck_resume_evidence` 会按 409 拒），相关 steer 那条不受影响。
        self._stuck_evidence = stuck_evidence
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
        # max_agent_turns 是"模型不收敛时的保险丝"，不是正常业务停止条件；
        # 正常停止由"模型不再返回 tool_calls"决定（`02 §5`）。
        if max_agent_turns < 1:
            raise ValueError(f"max_agent_turns 必须 ≥ 1：{max_agent_turns}")
        self.max_agent_turns = max_agent_turns
        # local fuse 的**来源**（deployment / agent_profile / 请求覆盖，`#308` 已解析）。
        # 运行时只把它投影进 `run/paused.limits.local`（客户端要看"谁定的"），
        # 不做任何策略判断——解析规则仍只在 `agent/budget.py` 一处。
        self.local_fuse_source = local_fuse_source
        # RunBudget 上下文（`#312`）：本次执行的账本起点（version / ceiling / 已消耗）。
        # `None` = 没有 run 作用域预算信息（沿用既有行为：无 ceiling、账本从 0 起）。
        self._run_budget = run_budget or LaunchRunBudget()
        # SessionBudget 端口（`#318`）：跨 run / 跨会话共享的树账（`02 §5.1` 第三层）。
        # `None` = 本执行没接 session 账（行为与 `#317` 收口时逐字相同——绝大多数
        # 单测与 CLI 直连路径）。准入 / 退回 / 记账的语义见 run_budget 的 Protocol。
        self._session_budget = session_budget
        # 完成闸门的策略 seam（`#316` / `02 §5.4`）：Core 只提供一个默认实现（静止后
        # 接受最终响应），域策略由嵌入方注入。**不是**配置项：完成规则不该由部署方
        # 之外的第三处（config / API）替它决定（`02 §9`）。
        self._completion_policy = completion_policy or DefaultCompletionPolicy()
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
        # V2 记忆形成的宿主（#298 T7b）：None = 未装配（V1 路径与绝大多数单测的
        # 形状），两个终态臂因此零改动。与 `memory_writer` 并列而不是取代它——
        # V1 的退出是 #303（clean-slate cutover，破坏性改动）的事，本票只把 V2
        # 这条链接上。
        self._memory_formation = memory_formation

        # 把 Registry 的工具定义绑定到模型——模型才会知道有哪些工具可选、
        # 并在回复里产出 tool_calls。bind_tools 是 LangChain 的标准接线点。
        # ScriptedModel 没有 bind_tools（测试用剧本直接构造 tool_calls），跳过绑定。
        # `_raw_model` 另存一份**未绑定工具**的 provider：预算暂停时的一次有界
        # closeout 用它生成 continuation（同"摘要不让模型请求工具"的既有做法，
        # 见本构造器上方 system_prompt 注释）——否则 closeout 可能又产出一个
        # tool_call，而暂停点之后不执行任何工具。
        self._raw_model = model
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
            user_data = {"content": steer.content, "steer_id": steer.steer_id}
            for key in (
                "revoke_fact_id", "refutes_event_id", "protected_facts",
                "remember_as_procedural_rule",
            ):
                value = getattr(steer, key, None)
                if value is not None:
                    user_data[key] = value
            user_event = session.append(
                USER_MESSAGE,
                user_data,
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
        """筛掉陈旧 steer（顺序保持队列内 FIFO）。

        "可作用于本 run"的判据与 stuck 恢复判定共用 `steer_applies_to_run`
        （`#317`）：两处各写一遍的代价是漂移——恢复侧曾把 `run_id=None` 的会话级
        steer 算成依据，而这里从来不认它。
        """
        applicable: list[SteerRequest] = []
        for steer in drained:
            if steer_applies_to_run(steer.run_id, run_id):
                applicable.append(steer)
            else:
                logger.warning(
                    "丢弃陈旧 steer（steer_id=%s，目标 run=%s，当前 run=%s）"
                    "——它仍未被 steer/applied 收口，由终态驱动当普通输入投递",
                    steer.steer_id, steer.run_id, run_id,
                )
        return applicable

    async def run(self, session: Session, user_input: str | None) -> AgentRunResult:
        """跑完整条 Agent Loop，返回 AgentRunResult。

        所有交互历史通过 Session 的 append-only SessionEvent 持久化；
        messages list 退化为每轮从事件投影出的运行期缓存。
        用 ainvoke 一次性拿完整 AIMessage（非流式入口，向后兼容）。

        ``user_input=None``（`#312`）：同 run 续跑且**没有**新任务文本——不落
        `user/message`，直接从已持久化历史继续（票面「Ordinary budget resume needs
        no new task text」；伪一条"继续"会往事件流里塞客户端从未说过的话）。
        """
        # 结果经本次调用专属的 holder 回传，不经实例字段——一个 Runtime 并发
        # 跑多个 run 时各拿各的，绝不出现"谁后终结谁生效"的跨 run 串台。
        result_holder: list[AgentRunResult] = []
        async for _ in self._drive(session, user_input, stream=False, result_holder=result_holder):
            pass  # 丢弃流式事件，只要副作用（持久化 + 最终结果）
        return result_holder[-1]

    async def run_stream(
        self, session: Session, user_input: str | None,
        cancel_reason_supplier: Callable[[], str] | None = None,
        result_holder: list[AgentRunResult] | None = None,
        user_input_metadata: dict[str, Any] | None = None,
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

        result_holder：需要 AgentRunResult（status/steps/final_text）的非流式
        消费者传入 list（约定同 run()：终结路径统一写入，失败兜底也保证写入）；
        None（默认）= 丢弃——SSE endpoint 只消费事件流，不读结果。

        SSE endpoint 直接消费这个 iterator；前端据此实时渲染。
        """
        drive = self._drive(
            session, user_input, stream=True,
            result_holder=result_holder,
            cancel_reason_supplier=cancel_reason_supplier,
            user_input_metadata=user_input_metadata,
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
        self, session: Session, user_input: str | None, *, stream: bool,
        result_holder: list[AgentRunResult] | None = None,
        cancel_reason_supplier: Callable[[], str] | None = None,
        user_input_metadata: dict[str, Any] | None = None,
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
        # 注意 steps 仍是 run 内轮次计数（max_agent_turns 保险丝与 AgentRunResult.steps
        # 依赖它逐 run 从 0 起算），全局编号一律走 step_base + steps。
        step_base = 0
        # 流式块记账（ADR-0016 §3.3）：思考/文本合帧落盘 + reasoning 块生命周期。
        # begin_run 之前异常 = 没有可记账的 run，保持 None。
        streamer: BlockStreamer | None = None
        # 本轮 run 的 token 消耗聚合（Gap 1）：各轮 usage 如实累加；该维累加溢出
        # int64 ⇒ 值转 `None`（未知，粘性，`_accumulate_usage`）。
        usage_total: dict[str, int | None] = {}
        # 终态簿记 owner（批次 B 候选 2）：model 在途标记 + 单终态不变量 +
        # usage 记账收拢一处，取消臂/异常臂只做调用。
        terminal = _RunFinalizer(session, usage_total)
        # 同错熔断护栏（ADR-0014 #69）与多模式 stuck 检测（`#317` / ADR-0048）：
        # **一个**责任域、**一个**判定点（`02 §5.3`）。实例在 run_id 定下来之后建
        # （见下）：检测状态全部由该逻辑 run 的事件重放重建，所以本执行不需要从
        # 上一次执行继承任何内存状态（重启 / 同 run 恢复天然一致）——注入的
        # `failure_guard` 只贡献 ① 的**阈值**，不再贡献状态（`StuckDetector` 构造时
        # 把它 reset 掉再重放）。
        # run 归因 token：嵌套运行（delegate→child 在同一父任务里跑）共享父
        # 上下文——child 的 set 会覆盖父值且不会随 child 完成消失，必须显式
        # 恢复，否则父后续的 Ledger/事件归因错挂到 child 的 run_id（#87 实锤）。
        run_context_token = None
        # 记忆注入注册表 token（#202 / ADR-0031 D4）：与 run_context_token 同一
        # 初始化点——异常发生在两个 set 之间时 finally 引用未绑定变量会掩盖
        # 原异常（全量回归实证：UnboundLocalError 掩盖 queue 竞态）。
        memory_injected_token = None
        # W-26（#380）：current_session_var 的 token，与上面两个 token 同一条
        # UnboundLocalError 纪律（set 之前生成器被关闭时 finally 不能炸）。
        session_token = None
        # session 树账的预留状态（`#318`）：True = 当前步已原子预留（turns/requests
        # 各一格）而决策尚未被接纳——取消臂 / 异常臂 / context 超限臂据此退回。
        # 初始化在 try 之前（与两个 token 同一条 UnboundLocalError 纪律）。
        session_step_reserved = False
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
            if user_input is not None:
                message_data = {"content": user_input}
                message_data.update(
                    {
                        key: user_input_metadata[key]
                        for key in (
                            "revoke_fact_id",
                            "refutes_event_id",
                            "protected_facts",
                            "remember_as_procedural_rule",
                        )
                        if user_input_metadata is not None
                        and key in user_input_metadata
                    }
                )
                user_event = session.append(USER_MESSAGE, message_data)
                yield to_agent_event(user_event)
                # USER_ACCEPTED 稳定边界：user/message 已持久化。
                await self._save_checkpoint(session, CheckpointBoundary.USER_ACCEPTED)
            # user_input is None = 同 run 续跑且无新任务文本（`#312`）：既没有新
            # user/message 可落，也没有「用户输入已被接纳」这个稳定边界可记——
            # 恢复这个事实由服务层的 `run/resumed` 事件表达（恢复刻度见它）。

            launch_budget = self._run_budget
            resuming = launch_budget.run_id is not None
            if resuming:
                # `#312` 同 run 续跑：**不**新建 run_id、**不**再落 run/started。
                # "恢复"这个事实由服务层在 CAS 通过后立刻落 `run/resumed`（durable，
                # 且在任何工作开始之前），runtime 只是接上那条逻辑 run 继续跑——
                # 计数器与 stuck 指纹都不重置（`02 §5.2`），消耗以事件为准。
                run_id = launch_budget.run_id
                turn_index = launch_budget.turn_index or 1
            else:
                run_id, turn_index = session.begin_run(
                    agent_id=self._agent_id, agent_profile=self._agent_profile,
                    # #226：请求侧模型标识落 durable 事件（同一 run 内只在 fallback 时换
                    # 模型，那次切换另由 model/fallback 记录）。装配层给的
                    # primary_model_name = ModelConfig.model_name = 发给 provider 的
                    # `model` 值（model/provider.py:107）。取值口径见 ADR-0034。
                    model=self._primary_model_name,
                    # `#312`：run 作用域 ceiling 在**开始时就 durable**（只在显式配了
                    # ceiling 时落键）——否则重启后投影不出"配置的绝对 ceiling"，
                    # 而 `03 §3.4` 要求 limits 可从事件重建成同样的值。
                    budget=as_run_started_budget(launch_budget.limits),
                )
            terminal.begin_run(run_id)
            # 多模式 stuck 检测器（`#317` / ADR-0048 D1）：**每次执行新建**，状态由本
            # 逻辑 run 的事件重放重建——重启 / 同 run 恢复因此得到同样的计数与指纹，
            # 不需要任何额外存储（`03 §3.4` 的不变量）。位置必须在 run_id 定下来
            # 之后：重放按 run_id 过滤，新 run 才不会把上一个逻辑 run 的计数带进来。
            # 重放期间产生的信号被丢弃（那些动作当时已经落过盘），随后由
            # `advance(session.events)` 只吃新事件（游标在检测器内部）。
            detector = StuckDetector.from_events(
                session.events, run_id, failure_guard=self._failure_guard,
            )
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
            # W-26（#380）：要写会话事件的工具（update_plan）在执行期经
            # `current_session_var` 拿会话——`Tool.execute` 协议不带 session，
            # 生产装配里工具注册又早于 Session 对象存在，构造注入接不上。
            # 设点与收尾和 run_context_var 完全同构。
            session_token = current_session_var.set(session)
            # 本 run 的记忆注入注册表（#202 / ADR-0031 D4）：设空集合，由
            # MemoryContextProvider.select() 在注入时写入；run 收尾 reset。
            memory_injected_token = memory_injected_ids_var.set(frozenset())
            if not resuming:
                # 按类型选取本 run 的 run/started——不假设 begin_run 恰好只追加一条事件。
                # 续跑执行没有新的 run/started（逻辑 run 是同一个），跳过镜像。
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
                # 第 -1 步（`#312` T4）：**唯一**的回合预算准入点。位置在 steer 注入与
                # ContextBuilder 之前——`02 §5.1` 要求判定发生在"任何 model / tool /
                # child 工作开始之前"，所以到顶时不再灌 steer、不再发起模型调用；
                # 本次执行以一条非终态 `run/paused` 收口（预留的那次 closeout 除外）。
                # 两个作用域在同一次判定里比（run 累计 ceiling / local fuse），命中
                # 哪个维度由 `pause_trigger` 如实回传——不合并成一个计数器（`02 §5.1`）。
                # 这一次读的账本同时是下面工具批次的 per-tool 配额**基数**（同一个
                # 读数两处用）：两次读会各自扫描事件，除了更贵还会让"判定用的数"与
                # "闸门用的数"有机会说两套话。
                consumed_so_far = self._execution_consumed(
                    launch_budget, session, arms.memory_event_start,
                )
                trigger_dimension = self._pause_trigger(
                    launch_budget, steps=steps, consumed=consumed_so_far,
                )
                if trigger_dimension is not None:
                    self._log("agent_decision", "回合预算到顶，run 暂停（非终态）",
                              span_id=new_span_id(), parent_span_id=run_span, step=steps,
                              decision="budget_paused", remaining_steps=0,
                              reason=f"命中 {trigger_dimension}，本次执行以 run/paused 收口",
                              outcome="success")
                    async for streamed in self._terminal_paused(
                        arms, launch=launch_budget, steps=steps,
                        trigger_dimension=trigger_dimension,
                    ):
                        yield streamed
                    return

                # session 作用域准入（`#318`）：run 判定通过**之后**。存储层在单事务里
                # 完成"判 + 预留"（turns / requests 各一格），并发兄弟竞争最后一格时至多
                # 一个被接纳（`10 §13`）；被拒 ⇒ 本次执行以 session 维度的 `run/paused`
                # 收口（trigger_dimension = `session.*` 配置字段路径）。
                session_admission: SessionAdmission | None = None
                if self._session_budget is not None:
                    session_admission = await self._session_budget.admit_step()
                    if not session_admission.accepted:
                        self._log(
                            "agent_decision", "session 预算到顶，run 暂停（非终态）",
                            span_id=new_span_id(), parent_span_id=run_span, step=steps,
                            decision="budget_paused", remaining_steps=0,
                            reason=f"命中 {session_admission.trigger_dimension}，"
                                   "本次执行以 run/paused 收口",
                            outcome="success",
                        )
                        async for streamed in self._terminal_paused(
                            arms, launch=launch_budget, steps=steps,
                            trigger_dimension=session_admission.trigger_dimension,
                            session_admission=session_admission,
                        ):
                            yield streamed
                        return
                    session_step_reserved = True

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
                    # session 预留整步退回（`#318`）：context 超限臂的契约是"模型在
                    # 本轮从未被调用"——turns / requests 的预留都没变成真账。
                    if session_step_reserved:
                        session_step_reserved = False
                        await self._session_budget.refund_step()
                    async for streamed in self._terminal_context_exceeded(
                        arms, launch=launch_budget, steps=steps, error=error,
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
                    # 模型流的句柄必须留着并**显式关闭**（`#313`）：`async for` 在
                    # `yield` 处被中断（消费方断连 / 本函数被 aclose）时不会关闭内层
                    # 生成器，于是"请求已发出、没拿到响应"这一格永远不会被记账，而
                    # 取消臂随后的 drain 已经跑过 ⇒ 那一格从账上消失（`02 §5.1`：
                    # `model_requests` 数的是**实际发出去**的请求）。先关流、再收尾
                    # 的顺序由本 finally 保证：它与 with 语句同一语义，只是不能写成
                    # with（异步发生器没有 `__aenter__`）。
                    model_stream = model_coord.astream(messages)
                    try:
                        async for chunk in model_stream:
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
                    finally:
                        # 已耗尽时是 no-op；在途时抛 GeneratorExit 进 `astream`，
                        # 由它记下那一格（outcome=failed）后再把 GeneratorExit 吞掉。
                        await model_stream.aclose()
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
                # `#313`（T5）：响应一拿到就**先记账**，早于下面任何接纳判定——
                # 空响应 / DSML 泄漏 / 后续任何拒绝都**不**退回这一次请求的消耗
                # （请求真的发出去了、Provider 也真的计了费）。此前 usage 只在
                # "被接纳"分支里累加，被拒的那一轮 usage 直接丢账。
                model_name = _model_name_from_response(ai)
                usage = _usage_from_response(ai)
                _accumulate_usage(usage_total, usage)
                model_cost = cost_usd_from_response(ai)
                terminal.add_cost(model_cost)
                # 每一次**实际**请求恰落一条 durable `model/request`（`model_requests`
                # 的计数点，`02 §5.1`），并按 append 顺序镜像：流帧必须是落盘日志的
                # 前缀（golden 判据）。位置在 `model/fallback` / `model/completed` 之前
                # ——请求是先发生的事实，决策与切换是对它的解释。
                request_events = self._record_model_requests(
                    session, model_coord, run_id=run_id, step=step_base + steps + 1,
                    usage=usage, cost=model_cost, model=model_name,
                )
                for request_event in request_events:
                    yield to_agent_event(request_event)
                # session 树账的 requests / tokens / cost 计数点（`#318`）：与本步的
                # `model/request` 事件一一对应——**减一**：本步准入已在 `admit_session_step`
                # 里预付了一格 requests（并发兄弟竞争的原子预留），预付那格对应的正是
                # 本步第一次实际请求，这里只补**超出一格**的部分（fallback 追加的尝试）；
                # usage / cost 仍随产出响应的那一次给，count=0 也要落——tokens / cost
                # 的树级计数点只有这里（缺席 = 该维转未知，`None` 粘性）。
                if self._session_budget is not None and request_events:
                    await self._session_budget.record_model_requests(
                        count=max(len(request_events) - 1, 0), usage=usage,
                        cost=model_cost,
                    )
                # R6-2（用户拍板）：空响应不是成功——content 与 tool_calls 双空
                # 意味着模型没有产出任何决策（内容过滤/上游静默失败）。在途标记
                # 仍开着时抛出，走统一失败兜底（model/failed + run/failed），
                # SSE 客户端因此能区分"模型答了空话"与"上游失败"。
                # #479：双空但 invalid_tool_calls 有货（#449 的 C 形态——本轮发起过
                # 工具调用，args 被 salvage 也解析不了）时，消息必须区分形态，
                # 否则"empty response"会把排查者引向内容过滤/上游失败。#506：截断
                # 推测只属于 length 收尾（langchain finish_reason 口径下的截断信号；
                # Pi 只认 length、DSH 只对 stop+零内容块触发 EMPTY_RESPONSE 且
                # max-tokens 保留截断语义——同向。实测 langchain_anthropic 写的是
                # stop_reason 键而非 finish_reason ⇒ Anthropic 线不命中 length
                # 分支、走中性回落）——stop 等非截断收尾只断言 salvage 后仍解析
                # 不了，不臆断成因；finish_reason 缺失回落中性、不臆断截断。
                # 消息按 OBS-008 只进诊断日志（事件侧仍只有类型名），归因与失败
                # 兜底语义各分支完全一致。
                extracted_content = _extract_text(ai.content)
                if not extracted_content and not ai.tool_calls:
                    invalid_calls = getattr(ai, "invalid_tool_calls", None)
                    if invalid_calls:
                        finish_reason = _finish_reason_from_response(ai)
                        prefix = (
                            f"model returned an empty response; "
                            f"{len(invalid_calls)} "
                        )
                        if finish_reason == "length":
                            tail = (
                                "unparsable tool_call_chunks present"
                                " (likely truncated — none were executed)"
                            )
                        elif finish_reason is None:
                            tail = (
                                "unparsable tool_call_chunks present"
                                " (none were executed)"
                            )
                        else:
                            tail = (
                                "malformed tool_call arguments"
                                " (unparsable after salvage — none were executed)"
                            )
                        raise RuntimeError(prefix + tail)
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
                if model_name:
                    model_data["model"] = model_name
                if usage:
                    # usage / model_name 在上面的记账块里已抽好（同一份事实，不重抽）。
                    model_data["usage"] = usage
                # C2（#552 配对）：本步**发生过 fallback 切换**时（`fallback_transitions`
                # 非空），`ai` 是 primary + fallback 的**拼接体**，而
                # `_finish_reason_from_response` 读到的 finish_reason 来自 primary
                # （langchain 合并 response_metadata 时 fallback 无该键 ⇒ 左值胜出）——
                # 它描述的不是这条拼接流。落进 model/completed 会让只读答案层的消费者
                # 把"primary 卡流 + fallback 重答"误读成"模型正常说完"（M10-3 症状面）。
                # 故这一支**不落该键**：「有 finish_reason」从此只代表**无 fallback 的
                # 干净终结**。primary 的收尾事实不丢——已由同一步的 `model/fallback`
                # （`primary_finish_reason` / `primary_content_chars`）承载，那才是它该挂
                # 的 attempt 边界。**不**落 `stream_terminated_without_finish_reason`：该键
                # 的既有语义是"流**没有** finish_reason"（R3），此处流里是有的、只是属于
                # primary——落它 = 用解释覆盖事实；边界由 model/fallback 标记。
                # 无 fallback 时保持 #551 M10-2 的既有诚实口径：有 finish_reason 就落键
                # （下游据此区分"模型说完了"）；缺失则**只标注**"流在没有 finish_reason
                # 的情况下结束"——不臆断截断、不删内容、不去重（#506 的"缺失回落中性"
                # 决策不推翻）。
                if not fallback_transitions:
                    finish_reason = _finish_reason_from_response(ai)
                    if finish_reason:
                        model_data["finish_reason"] = finish_reason
                    else:
                        model_data["stream_terminated_without_finish_reason"] = True
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
                            {**transition.event_data(),
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
                # 「这批工具会不会真的跑」只看有没有 tool_calls：预算判定只决定**下一轮**
                # 还起不起新模型调用（第 6 步），本轮的整批工具照跑（第 7 步）。
                # **不**把 `_pause_trigger` 的前瞻掺进来：那会让"要不要延迟落
                # model/completed"跟着"循环还会不会继续"走，在暂停边界上说出"工具不会跑"
                # 却照样把工具跑完（延迟落盘的目的是"model 决策的 durable 记录不早于它
                # 引用的那批工具"，与循环走不走无关）。
                defer_model_event = bool(tool_calls) and self.executor.tracks_operations
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
                # 决策已接纳（model/completed 已落或按稳定边界延迟落）⇒ 本步的
                # session 预留成为真账，取消 / 异常臂不再退回（`#318`）。
                session_step_reserved = False

                # ── #449：length 截断轮的全部 tool_call 判错（准入前拒绝）──
                # 位置在**完成闸门之前**：合法桶为空（salvage 全灭、只剩 invalid 桶）
                # 的截断轮若先闸门，会被当最终答复静默收口。finish_reason ==
                # "length" 且存在 tool_calls（含 invalid 桶）时，流式参数可能已被
                # JSON salvage 丢尾或凭空补全（分桶实测见
                # tests/agent/test_length_truncation.py §聚合事实），照常执行就是拿
                # 残缺参数跑真实副作用。Pi 语义：整批判错、绝不执行，错误即消息
                # （04 §4）回给模型、重发由模型决定；零配额（显式 0 增量）。事件形状
                # 的单一 owner 仍是 executor（emit_truncation_refusals），Runtime 只镜像。
                # invalid 桶条目不在 model_data.tool_calls 里（derive 不投影、无配对
                # 义务），只参与本判定、不落 tool 事件；这轮跳过 stuck 喂事件，检测器
                # 下一轮照常补上（它读的是 durable 全流）。本轮之后没有工具账增量
                # （delta 显式 0），故不调 _record_session_tool_deltas。
                if (response_meta.get("finish_reason") == "length"
                        and (calls or getattr(ai, "invalid_tool_calls", None))):
                    if calls:
                        for refusal_event in self.executor.emit_truncation_refusals(
                            session, calls, run_id=run_id, step_id=step_base + steps,
                        ):
                            yield to_agent_event(refusal_event)
                    if defer_model_event:
                        model_event = session.append(
                            MODEL_COMPLETED,
                            model_data,
                            run_id=run_id,
                            step_id=step_base + steps,
                        )
                        yield to_agent_event(model_event)
                        # MODEL_COMPLETED 稳定边界：判错事件之后延迟写入的 model/completed。
                        await self._save_checkpoint(session, CheckpointBoundary.MODEL_COMPLETED)
                    await self._save_checkpoint(
                        session, CheckpointBoundary.TOOL_BATCH_COMPLETED
                    )
                    self._log("tool_operation", "截断轮 tool_call 全部判错，未执行（#449）",
                              span_id=new_span_id(), parent_span_id=run_span, step=steps,
                              tool_call_ids=[c.id for c in calls], outcome="failure")
                    continue

                # 第 5 步：先判停止信号——若模型选择最终答复，进**完成闸门**（`#316`）。
                # 顺序是契约（`02 §5.4`）：先证六条 quiescence，静止才轮到 policy；
                # 两道任一不过都不落 `run/completed`，而**闸门这个臂自身零写入**：
                # 本轮 `model/completed` 在进闸门前就按稳定边界落盘了（它恰好落在
                # "模型不再请求工具"这一支，见 `_terminal_quiescence_blocked` 的契约）。
                if not tool_calls:
                    final = _extract_text(ai.content)
                    report = await self._quiescence_report(arms)
                    blocked_source = BLOCK_SOURCE_QUIESCENCE
                    blocked_reason = report.refusal_reason()
                    if report.quiescent:
                        decision = await self._completion_policy.decide(
                            report=report, final_text=final, run_id=arms.run_id or "",
                        )
                        if decision.accepted:
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
                        blocked_source = BLOCK_SOURCE_POLICY
                        blocked_reason = policy_rejection_reason(
                            self._completion_policy, decision,
                        )
                    # ── 走到这里说明这一轮**无法完成**（quiescence 或 policy 拒了）──
                    # 只有到这一刻才轮到 stuck 判定（ADR-0048 D5：护栏**不得越过完成
                    # 闸门**，否则"模型已经给出合格最终答复"会被一个计数推翻）。③
                    # （无工具独白）与"无工具调用形态的 ④/⑤"的可达面就在这里：模型
                    # 自己停下了、这个 run 又完不成，护栏才有话说。
                    signal = worst_stuck_signal(detector.advance(session.events))
                    if signal is not None:
                        if signal.level == STUCK_LEVEL_PAUSED:
                            async for streamed in self._stuck_pause_arm(
                                arms, signal=signal, launch=launch_budget, steps=steps,
                                run_span=run_span,
                            ):
                                yield streamed
                            return
                        for replan_event in self._stuck_replan_arm(
                            arms, signal=signal, run_span=run_span,
                            step_id=step_base + steps, steps=steps,
                        ):
                            yield to_agent_event(replan_event)
                        # 纠正消息已 durable（它在事件流里 ⇒ 下一轮 ContextBuilder
                        # 必然看见它）⇒ 回循环顶部重问一次。有界：每个模式**恰好一次**
                        # （检测器里的 `*_replanned` 闩），且循环顶部的预算准入照常先判
                        # ——暂停边界上不会多出一次模型调用。
                        continue
                    await self._terminal_quiescence_blocked(
                        arms, steps=steps, report=report,
                        source=blocked_source, reason=blocked_reason,
                    )
                    return

                # 第 6 步：模型仍在请求工具。预算是否到顶**不在这里判**——那件事统一
                # 在循环顶部（第 -1 步）用 `_pause_trigger` 判一次，两个作用域不会因为
                # 多一个判定点而漂移；本执行到顶时不会走到这里（顶部已经收口）。

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
                # per-tool 配额窗口（`#314` T6）：**一批一个**实例，基数是循环顶部那次
                # 事件派生的 run 账（`consumed_so_far.tool_calls_by_tool`）——本批内
                # 并发调用靠窗口里的同步预留不互相超发，批次之后由下一次循环顶部的
                # 暂停判定读**账本**（ToolExecutor 落的 `budget_delta`）决定是否暂停。
                # 基数不可得（resume 时账本无法重建）⇒ **不建窗口**：把"未知"当成 0
                # 会多放行 ceiling 条调用，宁可不启用这道闸门（那种状态其实到不了这里：
                # 暂停判定对未知基数是 fail-closed 的，见 `agent/run_budget.py`）。
                admitted_calls = consumed_so_far.tool_calls_by_tool
                tool_quota = (
                    ToolQuotaWindow(
                        limits=launch_budget.limits.tool_call_limits,
                        consumed=admitted_calls,
                    )
                    if launch_budget.limits.tool_call_limits and admitted_calls is not None
                    else None
                )
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
                        tool_quota=tool_quota,
                        # 本 run 的绝对 deadline（`#315`）：批次层只透传，判定在
                        # ToolExecutor 的接纳闸门（唯一判定点，`04 §9.1`）。
                        run_deadline=launch_budget.limits.deadline_at,
                    )
                except Exception as error:  # noqa: BLE001
                    tool_error = error
                # 执行期间追加的事件（tool/output_delta 等）镜像给流式消费者；
                # web 订阅者经 session listener 实时收到（seq 幂等合并不重复）。
                for event in session.since(tool_event_start):
                    yield to_agent_event(event)
                # session 树账的工具计数点（`#318`）：从本批 `tool/result` 的
                # `budget_delta` 提取增量（接纳点的唯一写入者 = ToolExecutor，与
                # run 作用域同一份事实、另一作用域的账）。
                if self._session_budget is not None:
                    await self._record_session_tool_deltas(session.since(tool_event_start))
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
                        # 预算增量由执行域算好（唯一计数点），这里只原样落盘：
                        # 0 增量（准入前被拒 / 未执行）同样如实落，账本据此求和。
                        budget_delta=execution.budget_delta,
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

                # ── 循环护栏（ADR-0014 的 #69 扩展为五模式：`#317` / ADR-0048）──
                # 工具回填后、下一轮模型调用前：把新事件喂给**唯一**的检测器，取严重
                # 程度最高的**一个**动作（同一次触发最多动作一次，暂停优先）。
                #
                # 两个来源合成这一个动作（与 ADR-0014 的"每条调用要么读信号、要么喂护栏"
                # 同一条规矩，`#317` 保持它）：
                #
                # * **带 `runtime_signal` 的调用**（今天只有 `delegate`：委派树的持久
                #   护栏）后面站着一个**自己的 durable 账本**——计数跨后代、跨进程，
                #   不在本 run 的事件流里（子会话的失败落在子会话的 JSONL）。它的
                #   SOFT / HARD 经 `external_failure_signal` 翻译成本 run 的同一种信号；
                #   同时把这几条 `tool_call_id` 报给检测器**别在 ① 上重数**——同一批
                #   失败记两遍会让阈值提前到顶（`StuckDetector.advance` 的口径）。
                # * **其余调用**由事件派生的检测器计数（五模式全在它手里，ADR-0048 D1）。
                #   配额拒绝 / 到点被拒（`#314`/`#315`）不喂护栏这条语义也在它里面
                #   （`GUARD_EXEMPT_ERROR_CODES`，判据只剩一处）。
                externally_counted: set[str] = set()
                external_signals: list[StuckSignal] = []
                for execution in executions:
                    persisted = execution.result.runtime_signal
                    if persisted is None:
                        continue
                    foreign = external_failure_signal(
                        level=persisted.level, tool_name=persisted.tool_name,
                        fingerprint=persisted.fingerprint,
                        count=persisted.consecutive_failures,
                    )
                    if foreign is None:
                        # level=none（树计数照常推进但没到线）等：不动作，也**不**免
                        # 掉本检测器的 ①——那个账本此刻并没有给出结论。
                        continue
                    external_signals.append(foreign)
                    externally_counted.add(execution.tool_call_id)
                signal = worst_stuck_signal(
                    [
                        *detector.advance(
                            session.events,
                            externally_counted_call_ids=frozenset(externally_counted),
                        ),
                        *external_signals,
                    ]
                )
                if signal is not None:
                    if signal.level == STUCK_LEVEL_PAUSED:
                        async for streamed in self._stuck_pause_arm(
                            arms, signal=signal, launch=launch_budget, steps=steps,
                            run_span=run_span,
                        ):
                            yield streamed
                        return
                    for replan_event in self._stuck_replan_arm(
                        arms, signal=signal, run_span=run_span,
                        step_id=step_base + steps, steps=steps,
                    ):
                        yield to_agent_event(replan_event)
                    # 纠正消息已 durable ⇒ 继续循环（下一轮 ContextBuilder 必然看见
                    # 它）。有界：每个模式**恰好**一次纠正（检测器里的 `*_replanned`
                    # 闩），且循环顶部的预算准入照常先判。
        except (asyncio.CancelledError, GeneratorExit):
            # 取消臂：客户端断连（SSE 生成器被取消/关闭）走这里——GeneratorExit /
            # CancelledError 是 BaseException，顶层 except Exception 兜不到，
            # durable 日志会永远停在悬空的 run/started 上（无结局的历史，web 层
            # 也不会调 resume 修复）。只做持久化收尾，不 yield：生成器关闭中
            # 禁止再产出（RuntimeError），取消中的 task 再 yield 也会被立即再取消。
            # 收尾后继续向上传播取消——吞掉取消会让 task 无法正确结束。
            try:
                # session 树账退回预留的 turns（`#318`）：本步决策未接纳；请求是否
                # 发出过由失败收尾的 drain 落账（失败/在途请求照样占 model_requests
                # 一席），所以只退 turns 一格。shield：收尾期间再取消也不能让账目
                # 退回失败打断收尾（退回失败只进日志，不改变取消语义）。
                if self._session_budget is not None and session_step_reserved:
                    session_step_reserved = False
                    try:
                        await asyncio.shield(self._session_budget.refund_turn())
                    except Exception:  # noqa: BLE001 - 存储故障不打断取消收尾
                        self._log("task_failed", "session 预算退回失败（存储故障？）",
                                  span_id=run_span, outcome="error")
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
            # session 树账退回预留的 turns（`#318`）：失败臂的 drain 已把发出的请求
            # 落账（model/request failed），决策未接纳 ⇒ 只退 turns 一格。退回失败
            # 不改变失败收尾（账目偏差方向是收紧，审计事件缺一条而已）。
            if self._session_budget is not None and session_step_reserved:
                session_step_reserved = False
                try:
                    await self._session_budget.refund_turn()
                except Exception:  # noqa: BLE001 - 收尾优先
                    self._log("task_failed", "session 预算退回失败（存储故障？）",
                              span_id=run_span, outcome="error")
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
            # W-26（#380）：current_session_var 与 run_context_var 同一收口窗口、
            # 同一 ValueError 语义（跨上下文 aclose 时 token 随任务消亡）。
            if session_token is not None:
                try:
                    current_session_var.reset(session_token)
                except ValueError:
                    pass

    # ─── 终结臂（#264 / T11 第一切片）────────────────────────────────────────
    # 六个终结点（context 超限 / completed / local fuse / 同错熔断硬触发 / 取消 /
    # 顶层异常）的收尾序列从 _drive 提到这里；_drive 仍是唯一 loop owner，只决定
    # "走哪条臂 + 何时 return"。每条臂的**顺序与 append 次数**是 #263 基线冻结的
    # 事实（`tests/agent/test_event_sequence_golden.py`），改动会让基线变红——那
    # 正是这份基线的用途，不要为了"顺手统一"改形状。
    # 共同纪律：终态字段由 _RunFinalizer 单点供给；信封编号走 arms.envelope_step()；
    # 取消臂不 yield（生成器关闭中禁止产出），异常臂逐条镜像收尾事件。

    def _record_model_requests(
        self, session: Session, coord: ModelFallbackCoordinator, *,
        run_id: str | None, step: int, usage: dict[str, int] | None,
        cost: Decimal | None, model: str | None,
    ) -> list[SessionEvent]:
        """把本步的每一次**实际**请求落成 durable `model/request` 并返回（供镜像）。

        usage / cost 只挂在**产出响应**的那一次上（= 最后一格且 `outcome=completed`）：
        前面那些失败/被拒的尝试，Provider 没有给出可归属的账目——缺席实现不伪造
        （`11 §6.1`），而它们的缺席会让本 run 的 token / cost 累计转为**未知**
        （`agent/run_budget.py` 的 `None` 粘性），这正是"不可得 ≠ 0"在计数器上的落点。
        """
        attempts = coord.drain_requests()
        events: list[SessionEvent] = []
        for index, attempt in enumerate(attempts):
            produced_response = (
                index == len(attempts) - 1
                and attempt.outcome == REQUEST_OUTCOME_COMPLETED
            )
            events.append(self._append_model_request(
                session, role=attempt.role, outcome=attempt.outcome,
                run_id=run_id, step=step,
                usage=usage if produced_response else None,
                cost=cost if produced_response else None,
                model=model if produced_response else None,
            ))
        return events

    def _append_model_request(
        self, session: Session, *, role: str, outcome: str,
        run_id: str | None, step: int, usage: dict[str, int] | None = None,
        cost: Decimal | None = None, model: str | None = None,
    ) -> SessionEvent:
        """落一条 `model/request`（`model_requests` 的唯一计数点，`02 §5.1`）。

        只写**知道**的键：usage / cost / model 缺席就不落键（不可得 ≠ 0）。
        `cost_usd` 是十进制**字符串**：`Decimal` 进 `json.dumps` 会炸，而 `float()`
        引入与 wire 不等价的二进制近似（`11 §6.1`：二进制浮点相等不是契约）。
        """
        data: dict[str, Any] = {"role": role, "outcome": outcome}
        if model:
            data["model"] = model
        if usage:
            data["usage"] = usage
        if cost is not None:
            data["cost_usd"] = format(cost, "f")
        return session.append(MODEL_REQUEST, data, run_id=run_id, step_id=step)

    async def _record_session_tool_deltas(self, events: list[SessionEvent]) -> None:
        """把一批事件里的 `budget_delta` 并进 session 树账（`#318`）。

        读法与 `run_budget._add_tool_delta` 同一条（同一次接纳的两本账）：贡献为 0
        的名字不落表；读不懂的 delta 贡献 0（recovery / dangling 修复的合成结果
        不来自 Executor 的接纳点）。
        """
        calls: dict[str, int] = {}
        attempts: dict[str, int] = {}
        for event in events:
            if event.type != TOOL_RESULT:
                continue
            delta = event.data.get("budget_delta")
            if not isinstance(delta, dict):
                continue
            name = delta.get("tool_name")
            if not isinstance(name, str) or not name:
                continue
            for key, table in (("tool_calls", calls), ("tool_attempts", attempts)):
                value = delta.get(key)
                if isinstance(value, int) and not isinstance(value, bool) and value > 0:
                    table[name] = table.get(name, 0) + value
        if calls or attempts:
            await self._session_budget.record_tools(calls=calls, attempts=attempts)

    def _now(self) -> datetime:
        """本执行读挂钟的**唯一**入口（`#315`）。

        为什么收成一个方法：deadline 是唯一"要跟当前时刻比"的判定，而时刻是**不可
        注入的外部输入**——散点 `datetime.now()` 会让"到点了吗"在测试里只能靠 sleep
        或改系统时钟来驱动。收成一处之后，用例 monkeypatch 这一个方法就能把"现在"
        钉在任意时刻，生产路径仍是裸挂钟（与 `run_budget.utc_now` 同一取舍）。
        """
        return utc_now()

    def _execution_consumed(
        self, launch: LaunchRunBudget, session: Session, start: int,
    ) -> BudgetConsumed:
        """逻辑 run 的**累计**账 = 启动时的账 + 本次执行新发生的账。

        两侧都是事件派生值（`consumed_from_events` 读 `model/completed` 与
        `model/request`），所以这里既不新增计数器、也不会与落盘事实漂移——
        `#313` 的"计数点唯一"就落在这句话上。
        """
        return add_consumed(launch.consumed, consumed_from_events(session.since(start)))

    def _pause_trigger(
        self, launch: LaunchRunBudget, *, steps: int, consumed: BudgetConsumed,
    ) -> str | None:
        """本次执行此刻还能不能继续；到顶时返回命中的预算维度（`#312` T4 / `#313` T5）。

        **唯一**判定入口：循环顶部的准入（"要不要暂停"）。`model/completed` 的延迟落盘
        曾经也调它（"本轮之后还会不会继续"），`#312` 的审查把那条用法去掉了——延迟的
        语义是"model 决策的 durable 记录不早于它引用的那批工具"，与本执行还能不能继续
        无关（把预算看进去会让暂停边界上写出"工具不会跑"却照样跑完一批）。

        `steps` = 本执行**已接纳**的轮数；run 作用域四维的累计消耗由调用方按事件算好
        传进来（`_execution_consumed`），本方法只做判定、不自己数数。
        local fuse 用本执行轮数计（它是**实例级**保险丝，续跑执行拿到的是新实例，
        `02 §5.1` 的三层控制互不替代）。
        """
        return pause_trigger(
            consumed=consumed,
            run_limits=launch.limits,
            execution_steps=steps,
            local_fuse_turns=self.max_agent_turns,
            # deadline 的判定要比"现在"；时刻从本执行的唯一入口读（`_now`），
            # 判定规则本身仍在 `run_budget.pause_trigger` 一处。
            now=self._now(),
        )

    def _stuck_replan_arm(
        self, arms: _TerminalArms, *, signal: StuckSignal, run_span: str,
        step_id: int, steps: int,
    ) -> list[SessionEvent]:
        """恰好一次纠正性 replan 的落盘侧（`#317`；ADR-0048 D5）。

        ① 沿用 ADR-0014 的**既有形状**（`tool/failure-guard(level=soft)` + 既有纠正
        文案，逐字不变，`injected_by=tool_failure_guard`）——那个形状已经冻结在契约与
        前端投影里，换事件等于让同一条语义有两个历史形状；②–⑤ 是新语义（此前没有任何
        事件能表达它们），落新的结构化 `guard/stuck(level=replan)` + 新片段
        `corrective:stuck_pattern`（`injected_by=stuck_guard`）。

        "恰好一次"由检测器的闩保证（计数 `== T` 时给一次 replan 信号，此后该模式不再
        给），本函数只负责如实落盘与注入。

        **返回值必须由调用方按序镜像**给流消费者：durable 与流帧的顺序要一致。
        """
        session = arms.session
        if signal.pattern == STUCK_PATTERN_TOOL_FAILURE:
            event = session.append(
                TOOL_FAILURE_GUARD,
                {"level": "soft",
                 "tool_name": signal.tool_name,
                 "fingerprint": signal.fingerprint,
                 "consecutive_failures": signal.count},
                run_id=arms.run_id, step_id=step_id,
            )
            content = DEFAULT_REGISTRY.assemble(
                "corrective:tool_failure_guard",
                {"tool_name": signal.tool_name or "", "consecutive_failures": str(signal.count)},
            ).fragment_text
            injected_by = "tool_failure_guard"
            decision = "tool_failure_guard_soft"
        else:
            event = session.append(
                GUARD_STUCK, signal.as_event_data(),
                run_id=arms.run_id, step_id=step_id,
            )
            content = DEFAULT_REGISTRY.assemble(
                "corrective:stuck_pattern",
                {"pattern_label": signal.label, "pattern_count": str(signal.count)},
            ).fragment_text
            injected_by = "stuck_guard"
            decision = "stuck_replan"
        corrective = session.append(
            USER_MESSAGE,
            # runtime 注入的纠正消息不是真实用户发言——标记来源供前端投影 / 审计区分
            # （不变量 #22 边缘），**并供记忆抽取剔除**（memory/extractor.py 按此标记
            # 单点过滤：注入消息一旦被当成真实用户发言，工具输出里的注入指令就能被
            # 洗成跨会话 USER 记忆）。
            {"content": content, "injected_by": injected_by},
            run_id=arms.run_id, step_id=step_id,
        )
        self._log("agent_decision", "护栏触发纠正性 replan",
                  span_id=new_span_id(), parent_span_id=run_span, step=steps,
                  decision=decision, pattern=signal.pattern, count=signal.count,
                  tool_name=signal.tool_name, outcome="success")
        return [event, corrective]

    async def _stuck_pause_arm(
        self, arms: _TerminalArms, *, signal: StuckSignal,
        launch: LaunchRunBudget, steps: int, run_span: str,
    ) -> AsyncIterator[AgentEvent]:
        """任何模式**再**达阈值 ⇒ stuck 暂停（`#317`；ADR-0048 D5）。

        先落结构化 `guard/stuck(level=paused)`（"为什么停"的可审计事实，含计数与
        指纹），再走既有暂停臂收口（对账闸门 → closeout → 恰好一条 `run/paused` →
        单终态簿记）。**不落 `run/failed`**：stuck 是可恢复的暂停，不是终态
        （`02 §5.2`；`STATUS_IDENTICAL_TOOL_FAILURE_LOOP` 因此从生产路径消失，
        常量保留——历史会话仍能读）。

        `trigger_dimension` 用**模式名**（ADR-0048 D5）：它不是配置字段路径，因为
        stuck 没有"该抬高哪一个 ceiling"可言——恢复要的是外部输入的变化。
        """
        event = arms.session.append(
            GUARD_STUCK, signal.as_event_data(),
            run_id=arms.run_id, step_id=arms.envelope_step(steps),
        )
        self._log("agent_decision", "stuck 模式再达阈值，run 暂停（非终态）",
                  span_id=new_span_id(), parent_span_id=run_span, step=steps,
                  decision="stuck_paused", pattern=signal.pattern, count=signal.count,
                  tool_name=signal.tool_name, outcome="success")
        yield to_agent_event(event)
        async for streamed in self._terminal_paused(
            arms, launch=launch, steps=steps,
            trigger_dimension=signal.pattern, stuck=signal,
        ):
            yield streamed

    async def _terminal_paused(
        self, arms: _TerminalArms, *, launch: LaunchRunBudget, steps: int,
        trigger_dimension: str, stuck: StuckSignal | None = None,
        session_admission: SessionAdmission | None = None,
    ) -> AsyncIterator[AgentEvent]:
        """暂停臂（`#312` T4；`02 §5.2` / `03 §3.4` / ADR-0044 D3）。

        `#317` 起它同时承载 stuck 暂停（`stuck` 参数非空 ⇒ `reason=stuck`）：收口顺序
        与对账闸门**完全共用**，区别只在三格载荷（`reason` / `resume_requirements` /
        `stuck`）与 continuation 的文案——"本次执行如何收口"没有第二套实现。

        顺序（每一条都是契约事实，不是实现口味）：

        1. **对账闸门**（`#315`，ADR-0046 §2 D4）：本 run 里"副作用未证"的 Operation 先
           提升到 `NEED_RECONCILE` 并落 `operation/reconcile-required`
           （见 `_raise_reconcile_required`）。**不以暂停原因为条件**——恢复闸门是
           session 级的，预算暂停带着未点名的未证行同样会被那次 409 挡死。到点后世界
           状态可能已经变了，而本次执行正要停下——此时"不知道"必须**落成 durable
           事实**，不能只留在进程内存里。
        2. **closeout**：先看还剩不剩容量（`closeout_capacity`）——剩则用**一次**
           有界模型调用产出 continuation（`closeout_source=model`）；没容量、调用失败
           或产出不合契约 ⇒ 落**确定性** continuation（只用已持久化事实，不伪造进展/
           成功/工具结果）。deadline 到点后容量判定恒为"没有"（到点不发 Provider
           请求，`04 §9.1`），所以这条暂停路径是确定性的。
        3. 落**恰好一条** `run/paused`（durable、**非终态**）并镜像给流消费者。
        4. 本次执行到此收口：**不**落 `run/completed` / `run/failed`（`02 §5.2` 明文），
           **不**做记忆形成（run 没结束，此刻抽取过早——`memory/v2/eligibility.py`
           的白名单里没有 paused），也不新增 Checkpoint 边界（`07 §3` 只有四个）。

        **closeout 不记进 `consumed.agent_turns`**：那个 counter 只数被接纳进 loop 的
        模型决策，closeout 是 `model_requests`（计数点与「预留」的完整推导见
        `agent/run_budget.py` 的模块 docstring 与 `pause_trigger`）。但它**要**记进
        `model_requests` / `total_tokens` / `cost_usd` —— 它是一次真实的 Provider
        请求（`02 §5.1` 明文把 closeout 与 primary/fallback 并列），所以它的
        `model/request` 事件在下面先落盘、再算暂停快照的 `consumed`：
        快照是**恢复的基数**，漏掉 closeout 会让"恢复后重算的账"比快照多一格
        （T4 的 `resume_headroom_ok` 与 `run/resumed.consumed` 就会各说一套）。
        """
        session = arms.session
        envelope_step = arms.envelope_step(steps)
        # `reason` / `resume_requirements` / `stuck` 三格是**同一条判据的三个投影**
        # （`stuck is None` 即预算 / deadline 路径，逐字与 `#315` 之前相同）：
        # 先由维度算出原因是"抬 ceiling"还是"换时刻"，stuck 则直接是第三个原因，
        # 并带上它的检测事实与"缺哪三类依据"的清单（`03 §3.4`）。
        reason = REASON_STUCK if stuck is not None else reason_for_dimension(trigger_dimension)
        # 恢复依据的**暂停侧快照**（`#317`；ADR-0048 D7/D8）：只在 stuck 暂停读一次
        # 证据端口。读不到（无端口 / 目录不存在）⇒ 两个值为 None —— 那是 fail-closed
        # 的一侧（恢复侧会按"无快照可比"拒绝那两条依据），而不是编一个值出来。
        stuck_payload: dict[str, Any] | None = None
        if stuck is not None:
            evidence = self._stuck_evidence.current() if self._stuck_evidence else None
            stuck_payload = {
                **stuck.as_pause_payload(),
                "environment_revision": (
                    evidence.environment_revision if evidence else None
                ),
                "policy_version": evidence.policy_version if evidence else None,
                # 逐维值与上面的摘要同生共死：摘要只能比较、还原不回来，恢复侧要靠它把
                # "暂停时生效的那一套"还原回自己的 amend（ADR-0048 D6/D8）。
                "policy_inputs": evidence.policy_inputs if evidence else None,
            }
        # 暂停收尾前的对账闸门（`#315` / ADR-0046 §2 D4）：位置在 continuation
        # **之前**——"存在未 reconcile 的副作用"是 continuation 必须如实写出的事实
        # （它要进 blockers 并改写 next_safe_action），不能等 continuation 定稿后
        # 再补一句。返回的是**已提升到 NEED_RECONCILE 的**那些调用（含事件已落盘）。
        #
        # **不以暂停原因为条件**（2026-09-26 两轴审查的 P1 缺陷，来源 = Correctness 轴）：
        # 恢复闸门（`session/service.py`）是 session 级、与暂停原因无关的 ⇒ 预算暂停
        # 若带着一条未点名的未证行，它的 continuation 仍会指示"抬高 ceiling 后恢复"，
        # 而那次恢复必被 409 挡死——正是 `03 §5` / ADR-0044 D4 禁止的
        # "暗示可安全续跑的暂停"。所以判据只看账本（`needs_reconcile`），不看 reason。
        reconcile_events, blocked_by = await self._raise_reconcile_required(
            arms, run_id=arms.run_id, step_id=envelope_step,
        )
        consumed_before = self._execution_consumed(
            launch, session, arms.memory_event_start,
        )
        # session 作用域的 closeout 容量（`#318`）：session 维度命中的暂停（或本执行
        # 接了 session 账的任何暂停）里，closeout 也受 session ceiling 约束——它是一次
        # 真实请求、花树的钱。`session_admission.snapshot` 是**准入那一刻**的读数；
        # closeout 之前的请求落账发生在工具批后（本臂之前），差一格的读数用账本现读
        # 修正——port 缺席（未接 session 账）时保持 None，旧形状逐字不变。
        session_snapshot: SessionBudgetSnapshot | None = None
        if self._session_budget is not None:
            if session_admission is not None:
                session_snapshot = session_admission.snapshot
            else:
                session_snapshot = await self._session_budget.snapshot()
        continuation, closeout_source, closeout_events = await self._closeout_continuation(
            arms, trigger_dimension=trigger_dimension,
            consumed=consumed_before, limits=launch.limits, step_id=envelope_step,
            blocked_by=blocked_by, reason=reason, stuck=stuck_payload,
            session_snapshot=session_snapshot,
        )
        # closeout 的请求落进 session 树账（`#318`）：它也是一次真实请求
        # （`02 §5.1` 把 closeout 与 primary / fallback 并列）；usage / cost 从
        # 产出响应的那格事件取（缺席 = 该维转未知，None 粘性）。
        if self._session_budget is not None and closeout_events:
            closeout_requests = [
                event for event in closeout_events if event.type == MODEL_REQUEST
            ]
            if closeout_requests:
                completed = closeout_requests[-1]
                closeout_usage = completed.data.get("usage")
                closeout_usage = (
                    closeout_usage if isinstance(closeout_usage, dict) else None
                )
                await self._session_budget.record_model_requests(
                    count=len(closeout_requests),
                    usage=closeout_usage,
                    cost=_decimal_or_none(completed.data.get("cost_usd")),
                )
        # 先镜像对账事件、再镜像 closeout：三条都是 durable，顺序与落盘顺序一致
        # （流帧必须是落盘日志的前缀，见 golden）。顺序本身也有语义：客户端先读到
        # "某个操作进入 NEED_RECONCILE"，再读到"本次执行暂停"——与 `03 §5`
        # 「对账优先于恢复」同向，投影据此把状态报成 needs_reconcile 而不是 paused
        # （暂停原因不参与这条闸门的判据，见 `_raise_reconcile_required`）。
        for reconcile_event in reconcile_events:
            yield to_agent_event(reconcile_event)
        # closeout 的 `model/request` 先镜像再落 `run/paused`：两条都是 durable，
        # 顺序与落盘顺序一致（流帧必须是落盘日志的前缀，见 golden）。
        for closeout_event in closeout_events:
            yield to_agent_event(closeout_event)
        # closeout 那次请求已经落账（`_closeout_continuation` 里 append）⇒ 重新算一次
        # 快照，让它包含进去。两次都从事件读，所以这是"再读一次真相"，不是累加。
        consumed = self._execution_consumed(launch, session, arms.memory_event_start)
        # `#318`：session 账行的 CAS 版本与 consumed（**closeout 之后的最新读数**）。
        # session 维触发的暂停，恢复要靠它们点名"抬哪个维 + expected_version"——
        # 缺了这一格，客户端必须再来一轮投影请求才能组出合法的恢复体。port 缺席
        # （未接 session 账 / 旧路径）⇒ 键缺席，旧形状逐字不变。
        session_payload: dict[str, Any] | None = None
        if self._session_budget is not None:
            session_current = await self._session_budget.snapshot()
            session_payload = {
                "version": session_current.version,
                "consumed": session_current.consumed.as_projection(),
            }
        limits = build_limits_snapshot(
            run_limits=launch.limits,
            local_fuse=LocalFuse(
                max_agent_turns=self.max_agent_turns, source=self.local_fuse_source,
            ),
            session_limits=(
                session_snapshot.limits if session_snapshot is not None else None
            ),
        )
        pause_data = build_pause_data(
            reason=reason,
            trigger_dimension=trigger_dimension,
            version=launch.version,
            consumed=consumed,
            limits=limits,
            continuation=continuation,
            closeout_source=closeout_source,
            # stuck 暂停的前置条件就是那三条依据里**当前可用**的那些（`03 §3.4`：
            # 非空只出现在它；可用子集由快照两格决定，见 `stuck_resume_requirements`）；
            # 预算 / deadline 暂停没有额外前置条件。"存在未 reconcile 的副作用"
            # **不**写在这一格：它由 `operation/reconcile-required` 事件 + Ledger 行
            # 自己表达，恢复闸门也读那一份（`03 §5`：对账优先于恢复是**状态**规则，
            # 不是暂停字段）。
            resume_requirements=(
                stuck_resume_requirements(stuck_payload) if stuck is not None else ()
            ),
            stuck=stuck_payload,
            # `#518` BUG-10：由**账目未知**（fail-closed）触发的暂停显式带依据，
            # 否则 "budget_exhausted + consumed:null" 运维无法解释。判定喂
            # `consumed_before`（触发那一刻的读数），不是下面 closeout 后的重读
            # ——两者今天必然相等（未知维配了 ceiling ⇒ closeout 无容量），但
            # 判据语义上属于触发时刻，不依赖那条容量政策不变量。
            accounting_unknown=accounting_unknown_pause_dimensions(
                trigger_dimension=trigger_dimension,
                consumed=consumed_before,
                session_snapshot=session_snapshot,
            ),
            # 与各终结臂同源（ADR-0033 的归因面）：暂停也是本次执行的收口，
            # 用户从事件就能找到那一段 trace。run 未终结 ⇒ 这不是 run 的 trace；
            # trace_url 此刻还不存在（只在终态回调里合成，见 build_pause_data）。
            trace_id=arms.telemetry.trace_id,
        )
        if session_payload is not None:
            # `#318`：`session` 键在 `run/paused.data` 顶层（limits 里已有 session 的
            # **ceiling**，这里补的是**账行 identity + consumed**）。缺席 = 没接 session 账。
            pause_data["session"] = session_payload
        paused = arms.session.append(
            RUN_PAUSED,
            pause_data,
            run_id=arms.run_id, step_id=envelope_step,
        )
        # 本次执行的**收口事实**已落盘：后续任何臂都不得再补一条终态（单终态不变量的
        # 执行层落点）。逻辑 run 仍开着——这由 `run/paused` 自己表达（它不在
        # RUN_TERMINAL_TYPES 里），`run/resumed` 会以同一 run_id 接回。
        arms.terminal.mark_terminal_written()
        yield to_agent_event(paused)
        arms.result_holder.append(
            AgentRunResult(status=STATUS_PAUSED, final_text="", steps=steps),
        )

    async def _raise_reconcile_required(
        self, arms: _TerminalArms, *, run_id: str | None, step_id: int,
    ) -> tuple[list[SessionEvent], tuple[str, ...]]:
        """在**任何原因的暂停**收尾前，把"副作用未证"的 Operation 提升到 `NEED_RECONCILE`。

        判据只在 `storage.needs_reconcile` 一处（非终态，或带"副作用未证"标记）——
        典型来源是 MUTATING 工具超时：执行域在收尾那一刻就把行留成 `UNKNOWN` 并打上
        标记（`ToolExecutor._settle_state`），因为那次尝试**给不出**"没落地"的证据。

        为什么在**本方法**做、而不在执行域顺手做完：执行域只如实记录"我不知道"；
        "要不要因此不再续跑"是稳定边界的决定（`03 §5`：对账优先于恢复；ADR-0044 D4：
        不得落一个暗示可安全续跑的暂停）。放在这里让整条链只有一个决定点：
        `暂停 ⇒ 有未证副作用则同时升 NEED_RECONCILE`。

        三件事，顺序固定（Ledger 先于事件——durable 状态先于对它的公告）：
        1. Ledger 推进到 `NEED_RECONCILE`（`RUNNING → UNKNOWN → NEED_RECONCILE`
           两步链由状态机强制，`07 §4`；已在 NEED_RECONCILE 的行不动）；
        2. 落 `operation/reconcile-required`（形状与 `RecoveryCoordinator._prepare_
           reconcile` 的**同一份**——客户端/CLI 只认一种形状；已存在则跳过，重复
           触发不重复落）；
        3. 返回（本次新增的事件，blockers 文案）：事件由调用方镜像、blockers 进
           continuation。

        Ledger 缺席（没注入 OperationLedger 的部署，或纯对话 session 没有账本行）⇒
        返回空：没有账本就没有"未证的副作用"这件事——**不**伪造一个对账要求。
        """
        ledger = self.executor.operation_ledger
        if ledger is None or run_id is None:
            return [], ()
        session = arms.session
        appended: list[SessionEvent] = []
        blockers: list[str] = []
        for operation in await ledger.list_for_session(session.session_id):
            if operation.run_id != run_id or not needs_reconcile(operation):
                continue
            if operation.state is not OperationState.NEED_RECONCILE:
                if operation.state is OperationState.RUNNING:
                    # 两步链：状态机不允许 RUNNING 直达 NEED_RECONCILE（07 §4），
                    # 也不允许跳过"不知道"这一档——那正是这次提升要说的事实。
                    await ledger.update_state(
                        session.session_id, operation.tool_call_id,
                        OperationState.UNKNOWN,
                    )
                await ledger.update_state(
                    session.session_id, operation.tool_call_id,
                    OperationState.NEED_RECONCILE,
                )
            blockers.append(
                f"工具 '{operation.tool_name}'（tool_call_id={operation.tool_call_id}）"
                "的副作用状态未证：已进入 NEED_RECONCILE，对账解除前本 run 不可恢复"
            )
            if not any(
                event.type == OPERATION_RECONCILE_REQUIRED
                and event.data.get("tool_call_id") == operation.tool_call_id
                for event in session.events
            ):
                appended.append(session.append(
                    OPERATION_RECONCILE_REQUIRED,
                    {
                        "tool_call_id": operation.tool_call_id,
                        "tool_name": operation.tool_name,
                        "args_identity": operation.args_identity,
                        "state": OperationState.NEED_RECONCILE.value,
                    },
                    run_id=run_id, step_id=step_id,
                ))
        return appended, tuple(blockers)

    async def _closeout_continuation(
        self, arms: _TerminalArms, *, trigger_dimension: str,
        consumed: BudgetConsumed, limits: RunLimits, step_id: int,
        blocked_by: tuple[str, ...] = (),
        reason: str | None = None, stuck: Mapping[str, Any] | None = None,
        session_snapshot: SessionBudgetSnapshot | None = None,
    ) -> tuple[dict[str, Any], str, list[SessionEvent]]:
        """产出 continuation、它的来源（`model` / `deterministic`）与本次落盘的事件。

        第三个返回值是**必须镜像**的：closeout 是一次真实的 Provider 请求，它落一条
        `model/request`（`02 §5.1`），而"流帧 = 落盘日志的前缀"是 golden 钉住的不变量
        ——落盘却不镜像会让暂停场景的帧序列在中间缺一格。

        **有界**的含义是双重的：预算上最多一次调用（且必须还有容量），形状上只接受
        契约里的四个键（见 `normalize_continuation`）。任何一步不合契约都**回落到**
        确定性组装——这条路必须永远可用（它是暂停能够成立的前提）。

        `reason` / `stuck`（`#317`）：stuck 暂停的确定性兜底与 closeout 指令都要换文案
        （它缺的不是额度，见 `_stuck_continuation` / `_closeout_instruction`）；
        模型给的产出仍过同一份 `normalize_continuation` + `apply_blocked_by`。
        """
        fallback = deterministic_continuation(
            events=arms.session.since(0), run_id=arms.run_id or "",
            trigger_dimension=trigger_dimension, limits=limits, consumed=consumed,
            blocked_by=blocked_by, reason=reason, stuck=stuck,
        )
        # 到点后**连 closeout 也不发**：它是真实 Provider 请求，`04 §9.1` /
        # ADR-0044 D4 把"deadline 过后不启动任何新工作"写死（`closeout_capacity`
        # 里那一句是判据本身，这里只是把"现在"传进去）。
        if not closeout_capacity(
            consumed=consumed, run_limits=limits, now=self._now(),
        ):
            return fallback, CLOSEOUT_DETERMINISTIC, []
        # session 作用域的容量（`#318`）：session ceiling 里任何一维已到线 / 账目
        # 未知 / deadline 到点，closeout 同样不发（它花的是树的钱；判据与 run 作用域
        # 同构，见 `session_closeout_capacity`）。`None` = 未接 session 账，不设限。
        if session_snapshot is not None and not session_closeout_capacity(
            consumed=session_snapshot.consumed,
            session_limits=session_snapshot.limits, now=self._now(),
        ):
            return fallback, CLOSEOUT_DETERMINISTIC, []
        try:
            messages = await self._context_builder.build(arms.session)
        except Exception as error:  # noqa: BLE001 - closeout 是尽力而为，绝不能反噬暂停
            logger.warning(
                "closeout 上下文装配失败（%s）——回落确定性 continuation",
                type(error).__name__,
            )
            return fallback, CLOSEOUT_DETERMINISTIC, []
        try:
            response = await self._raw_model.ainvoke(
                [*messages, HumanMessage(content=_closeout_instruction(
                    trigger_dimension=trigger_dimension, consumed=consumed, limits=limits,
                    reason=reason, stuck=stuck,
                ))],
            )
        except Exception as error:  # noqa: BLE001 - 同上：模型 closeout 不可用不是失败
            logger.warning(
                "closeout 模型调用失败（%s）——回落确定性 continuation",
                type(error).__name__,
            )
            # 请求发出去过（只是没拿到响应）⇒ 照样落账，usage / cost 缺席
            # （`11 §6.1`：不可得 ≠ 0）。
            return fallback, CLOSEOUT_DETERMINISTIC, [self._append_model_request(
                arms.session, role=PROVIDER_ROLE_CLOSEOUT,
                outcome=REQUEST_OUTCOME_FAILED, run_id=arms.run_id, step=step_id,
            )]
        usage = _usage_from_response(response)
        cost = cost_usd_from_response(response)
        request_event = self._append_model_request(
            arms.session, role=PROVIDER_ROLE_CLOSEOUT,
            outcome=REQUEST_OUTCOME_COMPLETED, run_id=arms.run_id, step=step_id,
            usage=usage, cost=cost, model=_model_name_from_response(response),
        )
        # closeout 的 usage 也要进本执行的 token 账（原来只有主循环的响应入账）。
        # 同口径收口（`#552` C1）与主循环累加点共用 `_accumulate_usage`。
        _accumulate_usage(arms.usage_total, usage)
        arms.terminal.add_cost(cost)
        parsed = _parse_closeout_json(_extract_text(response.content))
        normalized = normalize_continuation(parsed)
        if normalized is None:
            logger.warning("closeout 产出不合契约——回落确定性 continuation")
            return fallback, CLOSEOUT_DETERMINISTIC, [request_event]
        # 模型给的 continuation 也过 `apply_blocked_by`（`#315`；2026-09-26 两轴审查
        # 那个 P1 的后半）。**为什么不能在模型分支上省掉**：`blocked_by` 是已确证的事实
        # （Ledger 行 + 事件都已落盘），而预算暂停的 closeout 走的是**这一支**
        # （deadline 那支没有容量 ⇒ 恒走确定性 fallback，所以光看 deadline 用例看不出
        # 这一处缺口）。省掉的话，预算暂停的载荷会留着模型写的"提高 ceiling 后恢复"，
        # 而那次恢复必被开工前的 409 挡死——`03 §5` / ADR-0044 D4 禁止的
        # "暗示可安全续跑"。
        return (
            apply_blocked_by(normalized, blocked_by), CLOSEOUT_MODEL, [request_event],
        )

    async def _terminal_context_exceeded(
        self, arms: _TerminalArms, *, launch: LaunchRunBudget, steps: int,
        error: ContextWindowExceededError,
    ) -> AsyncIterator[AgentEvent]:
        """context 超限臂（W-04 #348）：模型在本轮从未被调用，走既有暂停生命周期收口。

        历史上这条臂直接落终态 `run/failed(reason=context_window_exceeded)`
        （ADR-0033 归因面的一行）；#348 起改为**非终态** `run/paused`：
        `trigger_dimension="max_context_tokens"`（config.py 的配置字段路径）经
        `reason_for_dimension` 自动映射为 `reason=budget_exhausted`——不新增
        reason 值（`03 §3.4` 词表冻结）。`STATUS_CONTEXT_WINDOW_EXCEEDED` 常量
        保留（历史会话可读）。

        ctx span 收口保持"裸调 + 收口即清口"（#285 / 残余 R1）：先收口再委托暂停臂
        ——暂停臂 closeout 里的二次 build 失败不碰 telemetry，本句柄不会被第二条
        收集臂再收一次（R2/R3 的两条出口各有仓库内用例）。"""
        if isinstance(error, ProtectedFactBudgetExceededError):
            # #430（W-02.1）：预算超限的成因明细从 logger-only 提升为任务可见
            # durable 事件（MEMORY_DEGRADED 先例）。fail-closed 暂停本身不变，
            # 冻结词表不动；载荷只装预算读数与 fact 的 id/type/尺寸，不装 value
            # （脱敏纪律——value 已在源事件里，事件不复制第二份）。
            # 本臂每次主循环 build 失败进一次；closeout 的二次 build 失败不进
            # 本臂（见 docstring），发射恰一次。
            arms.session.append(
                CONTEXT_PROTECTED_FACTS_EXCEEDED,
                {
                    "budget_tokens": error.budget_tokens,
                    "estimated_tokens": error.estimated_tokens,
                    "facts": error.facts,
                },
                run_id=arms.run_id,
            )
        arms.telemetry.context_build_completed()
        # error 的正文只装本项目的拒绝文案（compactor 侧已按类型名脱敏），进诊断
        # 日志不进事件——暂停载荷的字段清单是 03 §3.4 的契约，不多不少。
        logger.warning("context 超限，走暂停收口（%s）", error)
        async for streamed in self._terminal_paused(
            arms, launch=launch, steps=steps,
            trigger_dimension=TRIGGER_MAX_CONTEXT_TOKENS,
        ):
            yield streamed

    async def _terminal_completed(
        self, arms: _TerminalArms, *, steps: int, final: str,
    ) -> AsyncIterator[AgentEvent]:
        """正常完成臂：终态事件 → 记忆抽取（V1 提交 / V2 入队）→ 镜像 → FINAL_COMPLETED
        边界 → 结果。

        镜像（yield）**夹在记忆工作与 checkpoint 之间**：先后顺序是基线冻结的事实
        （checkpoint 失败被 _save_checkpoint 吞掉，但记忆抽取失败会走异常臂）。

        `_notify_memory_formation` 与 `_write_memories` 同在镜像**之前**（#298 T7b）：
        它是"作业在运行时交出这一轮之前落盘"那一条 Must Do 的落点，本身只多一次
        INSERT——"可见答复不等形成跑完"由形成被丢进后台任务来兑现。
        """
        arms.telemetry.run_completed(final, usage_total=dict(arms.usage_total) or None)
        end_event = arms.session.end_run(
            arms.run_id, status="completed", final_text=final,
            usage_total=dict(arms.usage_total) or None,
            # `#313`：成本只来自 Provider 的**归属**报账（`response_metadata["cost"]`），
            # 每次都自报才有值；任何一次缺失 ⇒ `None`（不可得 ≠ 0）。生产链路
            # （`model/accounting.py`：`reports_cost=False`）因此恒为 `None` —— 那是
            # 实情，不是"这个 run 不要钱"（费率表未定义 ⇒ 不臆造，`02 §5.1`）。
            cost_usd=arms.terminal.cost_total,
            trace_id=arms.telemetry.trace_id,
            trace_url=arms.telemetry.trace_url,
        )
        arms.terminal.mark_terminal_written()
        self._write_memories(arms.session, arms.memory_event_start)
        await self._notify_memory_formation(arms, terminal_status=STATUS_COMPLETED)
        yield to_agent_event(end_event)
        # FINAL_COMPLETED 稳定边界：Run 正常结束事件已持久化。
        await self._save_checkpoint(arms.session, CheckpointBoundary.FINAL_COMPLETED)
        arms.result_holder.append(
            AgentRunResult(status=STATUS_COMPLETED, final_text=final, steps=steps),
        )

    async def _quiescence_report(self, arms: _TerminalArms) -> QuiescenceReport:
        """完成闸门的输入聚合（`#316` / `02 §5.4`）：事件 + 账本行 + "本轮没请求工具"。

        本方法是 Runtime 与纯函数 `collect_quiescence_report` 之间**唯一**的适配层
        （存储读取只在这里发生）。账本只读一次 `list_for_session`；没有 Ledger 的
        部署（`executor.operation_ledger is None`）传空序列——那是"账本里没有欠账"的
        空真，不是豁免（ADR-0047 D1）。

        `new_tool_calls=False` 是**结构性**的：本闸门只在第 5 步 `if not tool_calls`
        那一支里被调用，"最新模型决策不再请求工具"由那个分支位置证成，不靠再查一遍。
        """
        ledger = self.executor.operation_ledger
        operations = (
            await ledger.list_for_session(arms.session.session_id)
            if ledger is not None
            else ()
        )
        return collect_quiescence_report(
            events=arms.session.events, operations=operations, new_tool_calls=False,
        )

    async def _terminal_quiescence_blocked(
        self, arms: _TerminalArms, *, steps: int, report: QuiescenceReport,
        source: str, reason: str | None,
    ) -> None:
        """完成闸门未通过臂（`#316` / `02 §5.4` / ADR-0047 D3）：**本臂零写入**。

        契约（都在票面 AC 上，代码本身看不出来）：
        - 不 append 任何 SessionEvent、不落终态、不推 reconcile、不改 Ledger、不写
          Checkpoint、不做记忆形成——拒绝的同时改状态会让"重入即重试"变成非幂等；
        - 观测在途句柄（`_Telemetry`）**不**在这里收口：逻辑 run 还没结束，同一 run_id
          的后续执行会接着写这一段 trace；
        - 走本臂之前，本轮已按既有稳定边界落了 `model/completed` 与对应 Checkpoint
          （那一支恰好就是"模型不再请求工具"这一步）——"零写入"说的是本臂。

        为什么不落终态 / 不落 `run/paused`、为什么选"保持未解 owner 活动"这条出口：
        ADR-0047 D3。状态与理由进 `AgentRunResult`，另落一条 `agent_decision` 诊断行
        （`blockers` 只含 kind 与 id，不含任何凭证或参数值）。
        """
        self._log(
            "agent_decision", "完成闸门未通过：未解工作仍然活动，本次执行不落终态",
            span_id=new_span_id(), step=steps,
            decision="blocked", outcome="blocked",
            source=source, reason=reason, quiescence=report.as_projection(),
        )
        arms.result_holder.append(
            AgentRunResult(
                status=STATUS_QUIESCENCE_BLOCKED, final_text="", steps=steps,
                reason=reason,
            ),
        )

    async def _terminal_failed_run(
        self, arms: _TerminalArms, *, steps: int, reason: str, message: str | None = None,
    ) -> AsyncIterator[AgentEvent]:
        """受控失败终态（`#312` 起只剩同错熔断硬触发一条路径）。

        走 failure_terminal（终态字段的唯一 owner）：本路径原先自己拼 end_run，
        于是 #222 之前它**一个归因键都没有**（字段集中供给被绕过 = 下一次加字段
        还会漏它）。`reason` 同时是 AgentRunResult.status，调用点只传一次。

        `#312`（T4）起回合预算到顶走的是 `_terminal_paused`（非终态暂停），
        不再是本臂——所以 `max_steps_exceeded` 这个受控失败终态已不可达并随之删除
        （行为裁决见 `02 §5.2`：命中预算 ⇒ 暂停，不是失败）。

        #298 T7b：`reason` 同时就是交给记忆形成的终态——它在
        `eligibility.ELIGIBLE_TERMINAL_STATUSES` 里（现在只剩
        `completed` + `identical_tool_failure_loop` 两项），取消类与 `failed` 不在。
        ⚠ 准确说：**取消 / 上下文超限 / 异常三条臂压根不调 `_notify_memory_formation`**
        （只有本臂与正常完成臂调），所以 AC1 的"被排除终态一个都不建"在生产上由"不通知"
        兑现；`eligibility` 的白名单与 `CANCELLED` / `UNSUPPORTED_TERMINAL_FAILURE` 两条是
        **第二道纯函数层的兜底**（T8 两轴审查 P3 指出原文读起来像这两条可达）。
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
        await self._notify_memory_formation(arms, terminal_status=reason)
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
        # #551 M10-8：同一决策里 primary 与 fallback 都失败时，两级错误**类型名**
        # 一并进 run/failed（首因不再只存在于 model/fallback 事件里）。
        double_failure = ctx.model_coord.double_failure_errors
        end_event = arms.terminal.failure_terminal(
            steps=arms.envelope_step(steps),
            reason=provider_reason or type(error).__name__,
            message=terminal_message,
            trace_id=arms.telemetry.trace_id,
            trace_url=arms.telemetry.trace_url,
            primary_error=double_failure[0] if double_failure else None,
            fallback_error=double_failure[1] if double_failure else None,
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
        self, session: Session, run_id: str, user_input: str | None, turn_index: int,
    ) -> Tracer:
        """观测实现选择（#249）：全 run 唯一一处"观测是否存在"的判据。

        未注入 / 未启用 sink → NullTracer（零外部副作用）；启用 → RunTracer
        （Langfuse adapter，ADR-0018 D5/D7）。返回的实现外面一律包
        ``_GuardedTracer``——调用点既不判空、也不各自兜异常。

        ``user_input=None``（同 run 续跑无新任务文本）在 trace 输入位落空串：
        这一次执行**没有**用户新输入，而不是"用户说了空话"（`RunTracer` 的
        input 位是 str，不为它扩一个 None 语义）。
        """
        sink = self._observability_sink
        if sink is None or not sink.enabled:
            return _GuardedTracer(NullTracer())
        return _GuardedTracer(RunTracer(
            sink,
            session_id=session.session_id,
            run_id=run_id,
            agent_id=self._agent_id,
            user_input=user_input or "",
            turn_index=turn_index,
            metadata_only=self._memory_formation is not None,
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

    async def _notify_memory_formation(
        self, arms: _TerminalArms, *, terminal_status: str,
    ) -> None:
        """把这一轮交给 V2 记忆形成（#298 T7b，AC1 的生产入口）。

        **为什么是"先入队，再镜像终态"**：ticket 同时要求"作业必须在运行时交出这一轮
        之前落盘"与"可见答复不能等记忆形成跑完"。本方法只 `await` 一次入队（一次
        SQLite INSERT），形成交给宿主的后台任务——两句因此都成立。

        反过来把入队放在镜像**之后**（写起来更"不挡用户"）会开一个静默的丢失窗口：
        消费方收到 `run/completed` 就断连时生成器被关闭，镜像之后的行根本不会执行，
        而那一轮记忆没有任何地方报错。所以在"多等一次 INSERT"与"可能整轮丢失"之间
        选前者。

        **为什么这里不筛事件类型**：本调用传的是**资格判定的输入**，不是执行输入——
        执行时的事件切片由宿主按 `(session_id, run_id)` 从会话日志重建（新鲜路径与
        恢复路径共用同一段代码，见 `memory.v2.runner` 的模块 docstring）。所以这里
        不抄 `_write_memories` 那份 V1 排除清单：V2 侧由 `projection` 的**允许清单**
        把关，抄第二份只会多一处要跟着事件词表更新的地方，而漏更新的那一次是静默的。

        **失败隔离**：`run/completed` 此刻已经落盘，这里抛异常会被顶层异常臂接住并
        补一条 `run/failed`——一个 run 出现双终态，历史不可对账（`_save_checkpoint`
        里是同一类论证）。记忆是旁路：它坏了只落日志。

        `run_id` 为 `None` 说明 run 从未开始：没有 run 就没有可切片的轮次，直接跳过
        （`_terminal_failed_run` 在 begin_run 之前失败时是这个形状）。
        """
        notifier = self._memory_formation
        if notifier is None or arms.run_id is None:
            return
        try:
            await notifier.notify_run_finished(
                session_id=arms.session.session_id,
                run_id=arms.run_id,
                terminal_status=terminal_status,
                events=arms.session.since(arms.memory_event_start),
            )
        except Exception:
            # 旁路故障边界：见上——绝不毒化已落盘的 run 结果。
            logger.exception(
                "把 run %s 交给记忆形成失败（已落盘的 run 事实不受影响）",
                arms.run_id,
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
            note_checkpoint_save_failure()
            logger.exception(
                "checkpoint 保存失败（boundary=%s, session=%s）：不影响 run 结果",
                boundary_type.value,
                session.session_id,
            )
            # 机器可检索信号（#515 BUG-02）：logger.exception 只进人类日志，稳定的
            # outcome 让 JSONL / 健康面能回答"这次恢复为什么回到更旧的稳定边界"。
            # 不发 SessionEvent——checkpoint 失败不是会话真相（ADR-0004 Round 5）。
            if logger.hasHandlers():
                log_event(
                    logger,
                    "system_log",
                    "checkpoint 保存失败：恢复辅助降级",
                    level="error",
                    exc_info=True,
                    component="checkpoint",
                    outcome="checkpoint_save_failed",
                    boundary=boundary_type.value,
                    session_id=session.session_id,
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
