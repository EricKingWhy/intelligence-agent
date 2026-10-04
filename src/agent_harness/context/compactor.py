"""压缩运行期投影，保留完整 tool interaction 与当前用户 turn。"""

import asyncio
import json
import logging
import re
from contextlib import AbstractAsyncContextManager, nullcontext
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from typing import Any
from uuid import uuid4

from langchain_core.messages import (
    AIMessage,
    AnyMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)

from agent_harness.context.tokens import estimate_message_tokens
from agent_harness.model.concurrency import ModelCallGate
from agent_harness.prompt import DEFAULT_REGISTRY
from agent_harness.session.derive import (
    COMPACTION_SUMMARY_MESSAGE_NAME,
    ProtectedFact,
    serialize_protected_facts,
)
from agent_harness.session.event import SessionEvent
from agent_harness.session.plan import PlanItem, derive_plan

logger = logging.getLogger("agent_harness.context.compactor")


_SUMMARY_HEADINGS = (
    "## 原始目标与用户约束",
    "## 保护事实表",
    "## 已完成工作与关键决策",
    "## 失败方案",
    "## 当前进行中状态",
    "## Next Step",
    "## 精确标识清单",
    "## 文件清单",
)
_MODEL_SUMMARY_HEADINGS = _SUMMARY_HEADINGS[2:6]
_PROGRAMMATIC_SUMMARY_HEADINGS = (0, 1, 6, 7)
_IDENTIFIER_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_])(?:[A-Za-z][A-Za-z0-9]*(?:-[A-Za-z0-9]+)*-\d+"
    r"[A-Za-z0-9-]*|[A-Za-z][A-Za-z0-9]*(?:_[A-Za-z0-9]+)+)(?![A-Za-z0-9_])"
)
_FILE_PATH_PATTERN = re.compile(
    r"(?<![\w])(?:[A-Za-z]:[\\/]|/)?"
    r"[\w.@+-]+(?:[\\/][\w.@+-]+)*\.[A-Za-z0-9]{1,12}(?![\w])"
)
_NUMBER_PATTERN = re.compile(
    r"(?<![\w.-])[-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?%?(?![\w.-])"
)
_PATH_FIELDS = {"path", "file", "filename", "filepath", "file_path", "source_file"}
_EXACT_FIELDS = {"command", "cmd", "shell", "error", "error_message"}
#: #556 裁决 C（分流承载）：程序化标识/文件节的**有界**投影上限——无界累积的
#: 唯一事实源。保留**最后** N 条（encounter 顺序的尾部 = 偏向最近：继承的旧摘要在
#: 前、当前窗口在后，截尾淘汰最旧），与 Anthropic Context Editing 的 keep-N 同形。
_PROG_SECTION_MAX_ENTRIES = 50
#: 单条 entry 字符上限：错误全文等巨串不再撑爆节（截断 + 省略号，确定性纯函数）。
_PROG_SECTION_MAX_ENTRY_CHARS = 200


#: W-04 (#348)：一次摘要尝试失败的**有界** error_class 词表（诊断分类，不是新事件类型）。
#: timeout / transport_error 覆盖调用面；其余八类逐一对应校验闸门与 shrink 的各条拒绝。
_SUMMARY_ERROR_CLASSES = (
    "timeout",
    "transport_error",
    "empty_summary",
    "tool_calls_in_response",
    "non_text",
    "heading_mismatch",
    "empty_section",
    "programmatic_mismatch",
    # W-29 (#383)：摘要第 5 节与进度清单一致性闸门的拒绝类。
    "plan_section_mismatch",
    "summary_not_smaller",
    "target_not_reached",
)

#: 失败记录里 message 的长度上限（有界载荷；我们的拒绝文案远短于此，截断只是防御）。
_FAILURE_MESSAGE_LIMIT = 300


@dataclass(frozen=True)
class CompactionFailure:
    """一次摘要尝试的失败记录（W-04 #348）。

    `message` 只装本项目自己的拒绝文案或异常**类型名**——绝不透传 provider
    回显原文（与 ADR-0033 边界 1 同一条脱敏纪律）。`auto_limit` / `hard_limit`
    取触发时的整型阈值读数；`token_estimate` 是调用方进入压缩时的估算。
    """

    attempt: int
    error_class: str
    message: str
    auto_limit: int
    hard_limit: int
    token_estimate: int


class _SummaryRejected(ValueError):
    """摘要尝试内的单次拒绝（#348）：携带 `_SUMMARY_ERROR_CLASSES` 里的一个类。

    ValueError 子类：与既有校验 helper 的 ValueError 族在同一 except 面内被收拢，
    直接调用这些 helper 的既有测试不受影响。
    """

    def __init__(self, error_class: str, message: str) -> None:
        super().__init__(message)
        self.error_class = error_class


class ContextWindowExceededError(RuntimeError):
    """无法构造安全的模型上下文，调用方必须停止当前 run。"""

    def __init__(
        self, message: str, *,
        failures: list[CompactionFailure] | None = None,
    ) -> None:
        super().__init__(message)
        #: W-04 (#348)：双次摘要尝试的失败记录（最后一次在末尾）。预检类超限
        #: （tool 块 / 无完整早期轮 / 请求本身超限）没有尝试记录，保持为空表。
        self.failures = list(failures or [])


@dataclass
class CompactionResult:
    messages: list[AnyMessage]
    compacted_turn_count: int
    # 契约（P2-4 双计修正后统一）：**只含返回的 messages 本身**的估算，不含
    # 调用方（builder）另行记账的 system_prompt / 运行时快照 / 清单锚块——
    # 压缩成功路径本来就是 messages-only（test_builder_system_prompt 的回归钉），
    # 三条直通路径（预检放行 / 双失败安全继续 / 源区间不可用）已从
    # "原样回传调用方入参（含 sys+rt+plan）"统一到本契约，否则 builder 的
    # 补回项会二次计入，provider 预算被虚扣（方向安全但账目失真）。
    token_estimate: int
    fallback_used: bool
    # T4 (#134)：bracket 元数据——被压缩段的 seq 区间 + 唯一 bracket_id。
    source_seq_start: int | None = None
    source_seq_end: int | None = None
    bracket_id: str | None = None
    summary: str | None = None
    # W-04 (#348)：本次 compact 里失败过的摘要尝试（成功尝试之前的都在内；
    # 双失败安全继续时是两条）。调用方（builder）负责把它们落成任务可见状态。
    failures: list[CompactionFailure] = dataclass_field(default_factory=list)


#: T4 (#134)：摘要 prompt 的正文已迁到 `agent_harness.prompt.builtin`
#: （section `aux:compaction`）——改文案开那一个文件。


class ContextCompactor:
    def __init__(self, model_provider: Any, *, max_context_tokens: int = 200_000,
                 auto_compact_threshold: float = 0.70,
                 hard_guard_threshold: float = 0.85,
                 keep_recent_tokens: int = 20_000,
                 summary_timeout_seconds: float = 30.0,
                 summary_model: Any | None = None,
                 model_call_gate: ModelCallGate | None = None) -> None:
        if max_context_tokens <= 0 or not 0 < auto_compact_threshold <= hard_guard_threshold <= 1:
            raise ValueError("invalid context budget")
        if summary_timeout_seconds <= 0:
            raise ValueError("summary_timeout_seconds must be positive")
        # W-04 (#348)：摘要模型接缝——None 缺省 = 主模型（装配前的既有行为逐字节
        # 等价）。便宜档选择本身留在配置面（后续票），本层只负责"用谁摘要"。
        self._model = summary_model if summary_model is not None else model_provider
        # #559：摘要调用与主循环同闸（进程级在飞 ≤N）。None = 不过闸（既有行为
        # 逐字节等价）。槽位在 timeout **外面**取——排队等闸的时间不计入摘要
        # 预算，与主循环"闸包在看门狗外面"同一原则（model/concurrency.py）。
        self._gate = model_call_gate
        self.auto_compact_threshold = auto_compact_threshold
        self.hard_guard_threshold = hard_guard_threshold
        self.keep_recent_tokens = keep_recent_tokens
        self._hard_limit = max_context_tokens * hard_guard_threshold
        self._auto_limit = max_context_tokens * auto_compact_threshold
        self._summary_timeout = summary_timeout_seconds
        self._max_context_tokens = max_context_tokens
        self.reserve = max(int(max_context_tokens * 0.15), 16384)

    def _slot(self) -> AbstractAsyncContextManager[None]:
        """取一个**新**的摘要槽位（每次尝试各取一次；None = 不过闸）。

        `ModelCallGate.slot()` 是 `@asynccontextmanager` 产物，**一次性**（同
        fallback coordinator 的教训，见 model/fallback.py `_slot`）；取消路径
        由 slot 的 finally 归还 permit。
        """
        return self._gate.slot() if self._gate is not None else nullcontext(None)

    async def compact(
        self,
        messages: list[AnyMessage],
        token_estimate: int,
        *,
        events: list[SessionEvent] | None = None,
        protected_facts: list[ProtectedFact] | None = None,
        reserved_tokens: int = 0,
        source_ranges: list[tuple[int, int] | None] | None = None,
    ) -> CompactionResult:
        if reserved_tokens < 0:
            raise ValueError("reserved_tokens must be non-negative")
        _validate_tool_blocks(messages)
        prefix_end = 0
        while (prefix_end < len(messages)
               and isinstance(messages[prefix_end], SystemMessage)
               and not _is_compaction_summary(messages[prefix_end])):
            prefix_end += 1
        prefix = messages[:prefix_end]
        cut = max((i for i, message in enumerate(messages)
                   if isinstance(message, HumanMessage)), default=prefix_end)
        early, recent = messages[prefix_end:cut], messages[cut:]
        if not early:
            count = estimate_message_tokens(messages)
            # 硬护栏比对用**有效用量**（调用方入参，含 sys+rt+plan），与下方其余
            # 三处 `token_estimate > self._hard_limit` 同一口径——messages-only
            # 比对会在 (messages, 有效用量) 落入 (count≤hard<effective) 缝隙时
            # 放行越窗请求。
            if token_estimate > self._hard_limit:
                raise ContextWindowExceededError("No complete early turn can be compacted")
            return CompactionResult(list(messages), 0, count, False)
        prompt = SystemMessage(
            content=DEFAULT_REGISTRY.assemble("aux:compaction").system_text
        )
        transcript = HumanMessage(content=json.dumps(
            [message.model_dump(mode="json") for message in early], ensure_ascii=False,
        ))
        request = [prompt, transcript]
        # Preflight rejection is not a summary attempt and therefore emits no failure event.
        if estimate_message_tokens(request) > self._hard_limit:
            logger.warning(
                "Context compaction rejected; summary request exceeds hard guard",
            )
            if token_estimate > self._hard_limit:
                raise ContextWindowExceededError(
                    "Summary validation failed and original context exceeds hard guard"
                ) from None
            # P2-4：token_estimate 回传 messages-only（契约见 CompactionResult），
            # 不原样回传调用方入参（其已含 sys+rt+plan，builder 会再补一次）。
            return CompactionResult(
                list(messages), 0, estimate_message_tokens(messages), False,
            )
        # W-04 (#348)：摘要生成至多两次尝试。一次尝试 = ainvoke → 解析 → 程序化组装
        # → 校验闸门 → shrink 全链；预检（上面与 tool 块检查）不计入。每次失败记一条
        # 有界 CompactionFailure，由调用方落成任务可见状态。
        failures: list[CompactionFailure] = []
        summary_text = ""
        compacted: list[AnyMessage] | None = None
        for attempt in (1, 2):
            try:
                # #559：槽位在 timeout 外面取——排队等闸不计入摘要预算（30s 是
                # 单次调用的预算，不是排队的）；取消/失败由 slot 的 finally 归还。
                async with self._slot(), asyncio.timeout(self._summary_timeout):
                    response = await self._model.ainvoke(request)
                if not isinstance(response, AIMessage) or response.tool_calls:
                    raise _SummaryRejected(
                        "tool_calls_in_response",
                        "Summary must be text without tool calls",
                    )
                if not isinstance(response.content, str):
                    raise _SummaryRejected("non_text", "Summary must be plain text")
                if not response.content.strip():
                    raise _SummaryRejected("empty_summary", "Summary must not be empty")
                model_sections = _parse_summary_sections(
                    response.content, _MODEL_SUMMARY_HEADINGS,
                )
                candidate_summary_text = _assemble_summary(
                    early, model_sections, protected_facts,
                )
                _validate_summary(candidate_summary_text, early, protected_facts)
                # W-29 (#383)：摘要第 5 节与进度清单一致性闸门（PRD §6.1 表行 5，
                # 落盘前校验 = §4.4 闸门语义）。events 为 None（直连 compactor 的
                # 既有调用面）或会话无清单时闸门不启用——空接缝语义保留，无清单
                # 会话的行为逐字节等价。
                if events is not None:
                    _validate_plan_section(
                        candidate_summary_text, derive_plan(events).items,
                    )
                early_tokens = estimate_message_tokens(early)
                summary_message = HumanMessage(
                    content=candidate_summary_text,
                    name=COMPACTION_SUMMARY_MESSAGE_NAME,
                )
                summary_tokens = estimate_message_tokens([summary_message])
                if summary_tokens >= early_tokens:
                    raise ContextWindowExceededError(
                        f"Summary ({summary_tokens} tokens) is not smaller than "
                        f"compressed segment ({early_tokens} tokens)"
                    )
                candidate_messages = [*prefix, summary_message, *recent]
                if estimate_message_tokens(candidate_messages) + reserved_tokens >= self._auto_limit:
                    raise ContextWindowExceededError(
                        "LLM summary does not reach compaction target"
                    )
                # Commit candidate state only after every validation gate passes. A rejected
                # first attempt must not leak into the result if the retry also fails.
                summary_text = candidate_summary_text
                compacted = candidate_messages
                break
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                # 摘要失败不能把空摘要写入事件流；先记录、再决定重试或保留原投影。
                # CancelledError 不在这里吞（上面显式重抛）。
                failures.append(_record_failure(
                    attempt, exc, token_estimate, self._auto_limit, self._hard_limit,
                ))
                logger.warning(
                    "Context compaction attempt %s rejected (%s)",
                    attempt, failures[-1].error_class,
                )
        if compacted is None:
            # 两次尝试都失败：旧投影仍在硬护栏内则安全继续（下一稳定边界再评估）；
            # 已达硬护栏而仍无核验过的上下文 ⇒ 停止，异常携带失败记录供暂停面引用。
            logger.warning(
                "Context compaction rejected; keeping original projection (%s)",
                failures[-1].error_class if failures else "unknown",
            )
            if token_estimate > self._hard_limit:
                raise ContextWindowExceededError(
                    "Summary validation failed and original context exceeds hard guard",
                    failures=failures,
                ) from None
            # P2-4：messages-only（契约见 CompactionResult），不再原样回传入参。
            return CompactionResult(
                list(messages), 0, estimate_message_tokens(messages), False,
                failures=failures,
            )
        count = estimate_message_tokens(compacted)
        if count + reserved_tokens > self._hard_limit:
            raise ContextWindowExceededError(
                f"Compaction cannot fit context: {token_estimate} -> "
                f"{count + reserved_tokens} tokens; "
                f"hard guard {self._hard_limit:g}"
            )
        # T4 (#134)：从投影映射计算 source_seq 区间。旧摘要带有原 bracket 的
        # 完整来源范围，因此下一次压缩可以覆盖并替代之前的摘要。
        source_seq_start: int | None = None
        source_seq_end: int | None = None
        if events is not None:
            if source_ranges is not None:
                # W-03 (#347)：裁剪后的投影消息与 derive 产物**内容不再逐条相等**
                # （ToolMessage.content 原位替换），但消息数与顺序不变——调用方
                # 传来的 ranges 与 messages 位置一一对应，直接采用、跳过相等对齐。
                # 长度不符视同区间不可用（走既有拒绝路径），不做静默截断。
                early_ranges = (
                    list(source_ranges[prefix_end:cut])
                    if len(source_ranges) == len(messages) else None
                )
            else:
                from agent_harness.session.derive import (
                    derive_messages_with_source_ranges,
                )

                mapped = derive_messages_with_source_ranges(events)
                aligned = len(mapped) == len(messages) and all(
                    projected == supplied
                    for (projected, _source_range), supplied in zip(mapped, messages)
                )
                early_ranges = (
                    [source_range for _message, source_range in mapped[prefix_end:cut]]
                    if aligned else None
                )
            if early_ranges and all(source_range is not None for source_range in early_ranges):
                source_seq_start = min(source_range[0] for source_range in early_ranges)
                source_seq_end = max(source_range[1] for source_range in early_ranges)
        if events is not None and (source_seq_start is None or source_seq_end is None):
            logger.warning(
                "Context compaction rejected; source event range is unavailable",
            )
            if token_estimate > self._hard_limit:
                raise ContextWindowExceededError(
                    "Cannot persist compaction without a source event range"
                )
            # P2-4：messages-only（契约见 CompactionResult），不再原样回传入参。
            return CompactionResult(
                list(messages), 0, estimate_message_tokens(messages), False,
            )
        return CompactionResult(
            compacted,
            sum(
                isinstance(message, HumanMessage)
                and not _is_compaction_summary(message)
                for message in early
            ),
            count,
            False,
            source_seq_start=source_seq_start,
            source_seq_end=source_seq_end,
            bracket_id=str(uuid4()),
            summary=summary_text,
            # W-29 (#383) 补课：成功前的失败尝试也要带给调用方落任务可见状态——
            # CompactionResult.failures 的契约（"成功尝试之前的都在内"）与冻结
            # PRD §4.5（"每次失败留诊断与任务可见状态"）都要求这一条；此前成功
            # 路径漏传，首试被拒、重试成功时失败记录被静默丢弃（本票新闸门使
            # 该路径成为常态，判据测试暴露）。
            failures=failures,
        )


def _parse_summary_sections(text: str, headings: tuple[str, ...]) -> list[str]:
    lines = text.strip().splitlines()
    heading_positions = [
        (index, line) for index, line in enumerate(lines)
        if line.startswith("## ")
    ]
    if tuple(line for _, line in heading_positions) != headings:
        raise ValueError("Summary section headings do not match the contract")
    if not heading_positions or heading_positions[0][0] != 0:
        raise ValueError("Summary contains text outside its sections")
    contents = []
    for index, (line_number, _) in enumerate(heading_positions):
        end = (heading_positions[index + 1][0]
               if index + 1 < len(heading_positions) else len(lines))
        content = "\n".join(lines[line_number + 1:end]).strip()
        if not content:
            raise ValueError("Empty summary section must use (none)")
        contents.append(content)
    return contents


def _current_goal_body(protected_facts: list[ProtectedFact] | None) -> str:
    """#556 裁决 C：目标节只保留**当前生效目标**（+ 归档计数行）。

    「当前生效目标」的判定与保护事实通道**同源**：`derive_protected_facts` 从
    全量 SessionEvent 确定性地划出 user_goal sources（首条 + 每次 supersede 前
    最近一条，`tests/session/test_derive_supersede.py`），取 `source_seq` 最大的
    一条 = 最后一次目标声明——纯函数、重放逐字节稳定。值以 JSON 字符串编码
    （旧 [0] 即 JSON，形状延续；防节边界伪造与 strip 失配），本身走 #430 的
    2000 字符投影上限（超长自带截断标记 + source_event_id 回读指针）：大首消息
    折叠为引用 + 回读 ref，原文活在事件流；历史 goal 不再逐字累积，一行归档
    计数指回 SessionEvent 持久历史（不变量 #6：完整保存 ≠ 完整注入）。
    """
    goals = [
        fact for fact in (protected_facts or [])
        if fact.type == "user_goal"
    ]
    if not goals:
        return "(none)"
    current = max(goals, key=lambda fact: fact.source_seq)
    # JSON 字符串编码（旧 [0] 即 JSON，形状延续）：节内容回读时有外层 strip
    #（`_parse_summary_sections`），裸文本的尾随空白/换行会破坏精确比对；
    # 编码同时隔离 goal 正文里 heading 样式的行（防伪造节边界）。
    body = json.dumps(current.value, ensure_ascii=False)
    archived = len(goals) - 1
    if archived > 0:
        body += (
            f"\n（更早 {archived} 条历史目标已归档："
            "原文可由 SessionEvent 持久历史回读）"
        )
    return body


def _capped_entries(entries: list[str]) -> list[str]:
    """确定性窗口：先截断、再按截断值去重（保留最后出现者）、最后保留最近 N 条。

    #614②：去重必须发生在截断**之后**——`add_once` 只按全文去重，两条仅在
    第 200 字符之后分叉的超长条目全文不同、双双入列，截断后收敛为同一值，
    投影里出现重复条目。本函数先截断再按截断值去重，与窗口的「最近偏置」
    一致：同值重复保留最后出现的那条。
    """
    trimmed = [
        entry if len(entry) <= _PROG_SECTION_MAX_ENTRY_CHARS
        else entry[:_PROG_SECTION_MAX_ENTRY_CHARS] + "…"
        for entry in entries
    ]
    deduped: list[str] = []
    seen: set[str] = set()
    for entry in reversed(trimmed):
        if entry not in seen:
            seen.add(entry)
            deduped.append(entry)
    deduped.reverse()
    return deduped[-_PROG_SECTION_MAX_ENTRIES:]


def _programmatic_summary_sections(
    messages: list[AnyMessage],
    protected_facts: list[ProtectedFact] | None = None,
) -> dict[str, str]:
    """四个程序化节（#556 裁决 C：全部**有界**，规则为确定性纯函数）。

    - 目标节：当前生效目标原文（`_current_goal_body`）——叙述性历史用户消息
      **不再**逐字进节（旧实现 `extend` 无界是 F-COMP-1 的根因）；跨窗口刚性
      读回走 ProtectedFact 通道（§1 独立预算）与 SessionEvent 持久历史。
    - 标识/文件节：继承 + 提取逻辑不变，套确定性窗口（`_capped_entries`，
      保留最近 N 条 + 单条截断）——同票消除 `:477-481` 的同构无界。
    - 保护事实节：`serialize_protected_facts` 契约不动（sort_keys 逐字节稳定、
      独立预算 8192）。
    """

    def add_once(target: list[str], value: str) -> None:
        if value and value not in target:
            target.append(value)

    def decode_summary_values(value: str) -> list[str]:
        if value == "(none)":
            return []
        parsed = json.loads(value)
        if not isinstance(parsed, list) or not all(isinstance(item, str) for item in parsed):
            raise ValueError("Previous summary programmatic section is not a string list")
        return parsed

    identifiers: list[str] = []
    file_paths: list[str] = []
    protected_facts_body = "(none)"

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            is_error = value.get("status") == "error" or value.get("is_error") is True
            for key, child in value.items():
                normalized = key.lower().replace("-", "_")
                if isinstance(child, str):
                    if normalized in _PATH_FIELDS:
                        add_once(file_paths, child)
                        add_once(identifiers, child)
                    if (normalized in _EXACT_FIELDS or normalized == "id"
                            or normalized.endswith("_id")):
                        add_once(identifiers, child)
                    for match in _NUMBER_PATTERN.finditer(child):
                        add_once(identifiers, match.group())
                elif isinstance(child, (int, float)) and not isinstance(child, bool):
                    add_once(identifiers, str(child))
                visit(child)
            error_content = value.get("content")
            if is_error and isinstance(error_content, str):
                add_once(identifiers, error_content)
        elif isinstance(value, list):
            for child in value:
                visit(child)
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            add_once(identifiers, str(value))
        elif isinstance(value, str):
            for match in _IDENTIFIER_PATTERN.finditer(value):
                add_once(identifiers, match.group())
            for match in _NUMBER_PATTERN.finditer(value):
                add_once(identifiers, match.group())
            for match in _FILE_PATH_PATTERN.finditer(value):
                add_once(file_paths, match.group())
                add_once(identifiers, match.group())

    for message in messages:
        if (
            _is_compaction_summary(message)
            and message.content.startswith(f"{_SUMMARY_HEADINGS[0]}\n")
        ):
            previous = _parse_summary_sections(message.content, _SUMMARY_HEADINGS)
            if protected_facts is None:
                protected_facts_body = previous[1]
            previous_identifiers = decode_summary_values(previous[6])
            previous_paths = decode_summary_values(previous[7])
            for value in previous_identifiers:
                add_once(identifiers, value)
            for value in previous_paths:
                add_once(file_paths, value)
            continue
        # #556 裁决 C：HumanMessage 原文不再逐字进任何程序化节——目标走
        # `_current_goal_body`（protected_facts 通道），叙述性历史靠 Event 回读。
        visit(message.model_dump(mode="json"))

    return {
        _SUMMARY_HEADINGS[0]: _current_goal_body(protected_facts),
        _SUMMARY_HEADINGS[1]: (
            serialize_protected_facts(protected_facts)
            if protected_facts
            else "(none)"
            if protected_facts is not None
            else protected_facts_body
        ),
        _SUMMARY_HEADINGS[6]: json.dumps(
            _capped_entries(identifiers), ensure_ascii=False,
        ) if identifiers else "(none)",
        _SUMMARY_HEADINGS[7]: json.dumps(
            _capped_entries(file_paths), ensure_ascii=False,
        ) if file_paths else "(none)",
    }


def _is_compaction_summary(message: AnyMessage) -> bool:
    """识别带内部标记的新摘要及旧版 SystemMessage 摘要。"""
    if isinstance(message, HumanMessage):
        return message.name == COMPACTION_SUMMARY_MESSAGE_NAME
    if not isinstance(message, SystemMessage) or not isinstance(message.content, str):
        return False
    return message.content.startswith((f"{_SUMMARY_HEADINGS[0]}\n", "## 目标\n"))


def _assemble_summary(
    messages: list[AnyMessage], model_sections: list[str],
    protected_facts: list[ProtectedFact] | None = None,
) -> str:
    programmatic = _programmatic_summary_sections(messages, protected_facts)
    bodies = [programmatic.get(heading, "") for heading in _SUMMARY_HEADINGS]
    for index, body in enumerate(model_sections, start=2):
        bodies[index] = body
    return "\n\n".join(
        f"{heading}\n{body}" for heading, body in zip(_SUMMARY_HEADINGS, bodies)
    )


def _validate_summary(
    summary: str, messages: list[AnyMessage],
    protected_facts: list[ProtectedFact] | None = None,
) -> None:
    sections = _parse_summary_sections(summary, _SUMMARY_HEADINGS)
    expected = _programmatic_summary_sections(messages, protected_facts)
    for index in _PROGRAMMATIC_SUMMARY_HEADINGS:
        if sections[index] != expected[_SUMMARY_HEADINGS[index]]:
            raise ValueError("Programmatic summary section failed exact comparison")
    if not summary.strip():
        raise ValueError("Summary must not be empty")


def _validate_plan_section(
    summary: str, plan_items: tuple[PlanItem, ...],
) -> None:
    """W-29 (#383)：摘要第 5 节 ↔ 进度清单 in_progress 项一致性闸门。

    会话**有**清单时启用（PRD §6.1 表行 5「与进度清单 in_progress 项一致」）：
    每个进行中项的 `content` 或 `activeForm` 必须逐字出现在第 5 节内；清单存在
    但零 in_progress ⇒ 第 5 节必须是 `(none)`（与 `aux:compaction` prompt 的
    「无则写 (none)」同款约定）。无清单（items 为空）不启用——未用清单的会话
    第 5 节本就是自由文本，保持 W-04 之前的既有行为。

    判据刻意用逐字子串而非语义比对：PRD 执行约束要求确定性机制兜底，弱模型
    重述不能靠"觉得差不多"。清单表本身就在转录的 update_plan 工具调用里，
    模型有能力逐字带上。
    """
    if not plan_items:
        return
    section = _parse_summary_sections(summary, _SUMMARY_HEADINGS)[4]
    in_progress = [item for item in plan_items if item.status == "in_progress"]
    if not in_progress:
        if section != "(none)":
            raise _SummaryRejected(
                "plan_section_mismatch",
                "Plan section must be (none): no plan item is in progress",
            )
        return
    missing = [
        item.id for item in in_progress
        if item.content not in section and item.active_form not in section
    ]
    if missing:
        raise _SummaryRejected(
            "plan_section_mismatch",
            "Plan section is inconsistent with the progress plan; "
            f"in-progress item(s) missing: {', '.join(missing)}",
        )


def _validate_tool_blocks(messages: list[AnyMessage]) -> None:
    """识别完整 AIMessage + ToolMessage 原子块；不接受缺失或孤立结果。"""
    index = 0
    while index < len(messages):
        message = messages[index]
        if isinstance(message, AIMessage) and message.tool_calls:
            expected = [call["id"] for call in message.tool_calls]
            actual = []
            index += 1
            while index < len(messages) and isinstance(messages[index], ToolMessage):
                actual.append(messages[index].tool_call_id)
                index += 1
            if (not all(expected) or len(set(expected)) != len(expected)
                    or sorted(expected) != sorted(actual)):
                raise ContextWindowExceededError("Invalid tool call/result block")
        elif isinstance(message, ToolMessage):
            raise ContextWindowExceededError("Orphan tool result in context")
        else:
            index += 1


def _classify_failure(exc: BaseException) -> str:
    """异常 → `_SUMMARY_ERROR_CLASSES` 里的一个有界类（W-04 #348）。

    逐字文案是本模块自己冻结的（tests 依赖其稳定性），按前缀归类；无法辨认的
    ValueError 一律归 `heading_mismatch`（同为"摘要结构不合契约"面），非 ValueError
    的调用面故障归 `transport_error`。本函数不抛。
    """
    if isinstance(exc, _SummaryRejected):
        return exc.error_class
    if isinstance(exc, ContextWindowExceededError):
        message = str(exc)
        if message.startswith("Summary ("):
            return "summary_not_smaller"
        if "compaction target" in message:
            return "target_not_reached"
        return "transport_error"
    if isinstance(exc, (TimeoutError, asyncio.TimeoutError)):
        return "timeout"
    if isinstance(exc, ValueError):
        message = str(exc)
        if message.startswith("Empty summary section"):
            return "empty_section"
        if ("Programmatic summary section" in message
                or message.startswith("Previous summary")):
            return "programmatic_mismatch"
        return "heading_mismatch"
    return "transport_error"


def _record_failure(
    attempt: int, exc: BaseException, token_estimate: int,
    auto_limit: float, hard_limit: float,
) -> CompactionFailure:
    """一次尝试的异常 → 有界 CompactionFailure。

    message 只装本项目自己的拒绝文案或异常**类型名**；正文（可能含 provider
    回显）与调用栈一样只进诊断日志，不进任务可见状态（ADR-0033 边界 1 同源）。
    """
    if isinstance(exc, (_SummaryRejected, ContextWindowExceededError, ValueError)):
        message = str(exc) or type(exc).__name__
    else:
        message = type(exc).__name__
    return CompactionFailure(
        attempt=attempt,
        error_class=_classify_failure(exc),
        message=message[:_FAILURE_MESSAGE_LIMIT],
        auto_limit=int(auto_limit),
        hard_limit=int(hard_limit),
        token_estimate=token_estimate,
    )
