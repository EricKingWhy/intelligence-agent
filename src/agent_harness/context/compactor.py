"""压缩运行期投影，保留完整 tool interaction 与当前用户 turn。"""

import asyncio
import json
import logging
import re
from contextlib import AbstractAsyncContextManager, nullcontext
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from itertools import pairwise
from time import monotonic
from typing import Any

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
    _USER_GOAL_VALUE_MAX_CHARS,
    COMPACTION_SUMMARY_MESSAGE_NAME,
    ProtectedFact,
    serialize_protected_facts,
)
from agent_harness.session.event import CONTEXT_COMPACTED, SessionEvent
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
    r"[A-Za-z0-9-]*|[A-Za-z][A-Za-z0-9]*(?:_[A-Za-z0-9]+)+"
    # #640（T2-X/Y）：标准 8-4-4-4-12 十六进制 UUID。数字开头的形态不满足前两个
    # 分支的首字符 A-Za-z：此前整条漏配、或只从第二段起截出尾部伪片段
    #（`123e4567-e89b-…` → `e89b-12d3-a456-…`）。新分支排在既有两分支**之后**，
    # 既有分支的命中起点优先权不变。数字开头的裸 UUID 与连字符尾缀（`…-extra`）
    # 由新分支整条命中、尾缀不再并入条目（字母开头的既有连字符贪婪形态不受影响，
    # 仍把 `…-extra1` 并入同一 token）；字母数字黏连（`…0000`）被尾 lookahead 挡下，
    # 仍按既有语义落到分支1 自第二段起的尾部片段（可含黏连尾缀，非 8-4-4-4-12 形状）。
    # 边界沿用同一对 lookaround ⇒ 不从更长连续字母数字串里切出 UUID 形状伪标识。
    r"|[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}"
    r")(?![A-Za-z0-9_])"
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
#: timeout / transport_error 覆盖调用面；其余逐一对应校验闸门与各条拒绝。
#: 以 `attempt=0` 标记的**非摘要尝试**记录有四条——T12h (#647) 的
#: `source_range_unavailable`（**生成后**来源拒绝）、#639 的
#: `preflight_request_exceeds_hard_limit`（**尝试前**预检拒绝：摘要请求本身超 hard）、
#: #648 的 `no_compactable_early_turn`（**尝试前**预检拒绝：无可压缩完整早期轮）、
#: #1026 修回的 `chunk_bound_released`（**尝试前**留痕：超界原子块如实放行）。
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
    # T12h (#647)：来源区间不可用（生成后拒绝，非摘要尝试失败）——摘要已过全部
    # 校验闸门但无 source event range 可持久化，弃用候选、保留原投影，拒绝原因
    # 经既有有界失败通道落任务可见状态。保持有界：词表逐项对应一条拒绝面。
    "source_range_unavailable",
    # #639 阶段3a：摘要请求**本身**超 hard 的预检拒绝（尝试前，非摘要尝试失败）。
    # 此前该分支静默（零事件零状态）；对标 #647 的 attempt=0 纪律，把"为什么拒绝、
    # 差多少"（request_token_estimate vs hard_limit）落成任务可见诊断，不伪造摘要尝试。
    "preflight_request_exceeds_hard_limit",
    # #1026 裁决 A：摘要双失败 + 有效用量超硬护栏时的确定性截断降级标记
    # （attempt=0，非摘要尝试失败——截断是降级产物不是被拒尝试）。对标
    # #639/#648 的"只加诊断"纪律：词表有界、每项对应一条拒绝/降级面。
    "deterministic_truncation_fallback",
    # #1026 修回（审查 P2）：content 已不可再裁的超界原子块被 `_clip_block_to_budget`
    # 如实放行——「分块单请求 token 有界」（票面验收 3）的显式逃逸口，留痕使其
    # 事后可观测（attempt=0，非摘要尝试；分块决策处逐放行块记一条）。
    "chunk_bound_released",
    # #648 选项 B（用户 2026-10-06 批准）：无可压缩完整早期轮的预检拒绝
    # （尝试前，非摘要尝试失败）。此前该分支刻意"保持为空表"（见
    # ContextWindowExceededError 注释），本次是用户批准的刻意反转——只加诊断：
    # 对标 #639 阶段3a，attempt=0 + 有界词条 + 经 #348 落任务可见。
    "no_compactable_early_turn",
)

#: #1264（Shape A）：`_SUMMARY_ERROR_CLASSES` 的**瞬时/结构性**二分中"瞬时"的那一半——
#: 模型侧抖动（超时、传输、输出不成摘要、校验闸门拒绝），**重试有意义**。
#: 补集即结构性失败（`summary_not_smaller` / `target_not_reached` /
#: `source_range_unavailable` / `preflight_request_exceeds_hard_limit` /
#: `deterministic_truncation_fallback` / `chunk_bound_released` /
#: `no_compactable_early_turn`）：再压一次必然同结果，**不得**重试。
#: 与 `_SUMMARY_ERROR_CLASSES` 同处一个 owner：词表加项时这里**必须**同步二分。
#: 守卫强度如实交代（#1264 双轴审查 P2）：`tests/context/
#: test_1264_transient_failure_classes.py` 只机械钉住「子集 + 不重叠 + 无重复项」
#: 与两侧的写死样例；**新增**一个词条而忘了归类时，它会静默落到"结构性"侧
#: （保守方向：不重试，不会误烧请求），测试不会失败。
_TRANSIENT_ERROR_CLASSES = frozenset({
    "timeout",
    "transport_error",
    "empty_summary",
    "tool_calls_in_response",
    "non_text",
    "heading_mismatch",
    "empty_section",
    "programmatic_mismatch",
    "plan_section_mismatch",
})

#: 失败记录里 message 的长度上限（有界载荷；我们的拒绝文案远短于此，截断只是防御）。
_FAILURE_MESSAGE_LIMIT = 300
_SUMMARY_MODEL_ID_LIMIT = 256

#: #639 阶段 A：preflight 超限时缩小 early 段的有界重试。从 early 段尾部向前累计
#: `estimate_message_tokens`，保留尾部该比例的 recent（逐字留在投影、不摘要），
#: 只对更小的前缀段重新发起摘要。照 deepseek-harness `selectCompactableRange` 的
#: retainRatio=0.16（`refs/dsh` @ `5badb150`：`config.ts:25` 默认值、
#: `region.ts:117-` 保留 priced recent 尾且不拆 tool pair）。PORT DESIGN。
_RETAIN_RATIO = 0.16

#: #648 选项 B（用户 2026-10-06 批准）：无可压缩完整早期轮拒绝的显式失败文案
#: （附中文恢复指引，对标 #639 阶段 B 的 thrashing guard 文案结构）。
#: 英文首句与旧文案逐字一致（既有调用方按前缀匹配不受影响）；只加诊断，
#: 判定式（`>`）与抛点语义一字不动。
_NO_EARLY_TURN_MESSAGE = (
    "No complete early turn can be compacted. "
    "恢复指引：① 手动执行 /compact 检查当前窗口构成；"
    "② 调大 max_context_tokens（硬上限随之放宽）；"
    "③ 把任务转交 subagent 分段处理，避免单个原子工具块独占窗口。"
)

#: #1026：分块摘要的递进携带上限（字符）。中间块摘要在下一块请求里作为
#: 「前段摘要」携带——不封顶则请求数增长时 final 请求退化成无界（spike
#: 发现 5 的第二条界）。截断只影响携带参考文本，闸门只看 final 输出。
_CHUNK_CARRY_CHAR_CAP = 4000


def _clip_block_to_budget(
    block: list[AnyMessage], bound_tokens: int,
) -> list[AnyMessage]:
    """#1026：单条超大原子消息的确定性 head/tail 裁剪（spike 发现 5）。

    tool 原子块是消息级不可分单位（`_tool_block_boundaries`），块内单消息超过
    单块预算时无法按块边界再切——按"裁最大 content、保留 head/tail 半长 + 截断
    标记"迭代到入界。仓内同形先例：`_carrier_instruction_line` 的截断标记、
    `_PROG_SECTION_MAX_ENTRY_CHARS` 的有界投影。**不 mutate 原消息**（投影被
    调用方复用）；tool_call/tool_call_id 等结构字段保留（配对完整性不受影响）。
    content 已不可再裁（空/非文本/超短文本——替换串不再变短）而块仍超界时
    如实放行（结构面有界）；放行经 `chunk_bound_released` 诊断留痕（见
    `compact` 的分块决策处）。
    """
    clipped = [message.model_copy() for message in block]
    while estimate_message_tokens(clipped) > bound_tokens:
        target = max(
            clipped,
            key=lambda m: len(m.content) if isinstance(m.content, str) else 0,
        )
        if (not isinstance(target.content, str) or len(target.content) < 4):
            break
        keep = len(target.content) // 4
        replacement = (
            target.content[:keep]
            + "…［已截断：摘要输入超单块上限，head/tail 保留］…"
            + target.content[-keep:]
        )
        if len(replacement) >= len(target.content):
            # 修回（#1026 审查 P1）：content 落在 4~58 字符区间时 head/tail 加
            # 截断标记反而**更长**，旧码会收敛到固定点后 while 恒真、永不退出
            #（挂死而非报错，连降级路径都到不了）。替换串不再变短 ⇒ 已不可再裁，
            # 与空/非文本同按「如实放行（结构面有界）」退出。
            break
        target.content = replacement
    return clipped


def _chunk_early(
    early: list[AnyMessage], bound_tokens: int,
) -> list[list[AnyMessage]]:
    """#1026：early 段按 tool 原子块边界贪心分块（每块估算 ≤ bound）。

    切分吸附 `_tool_block_boundaries` 的块起点（与 `narrow_early_window` 同一
    完整性来源）：AI tool_call + ToolMessage 串永不跨块。单块自身超预算的
    原子块走 `_clip_block_to_budget` 确定性裁剪。贪心从 early 头部向后累积，
    保序（递进归并的块序 = 时间序）。
    """
    if not early:
        return []
    boundaries = _tool_block_boundaries(early, 0, len(early))
    spans = [*pairwise(boundaries), (boundaries[-1], len(early))]
    chunks: list[list[AnyMessage]] = []
    current: list[AnyMessage] = []
    current_tokens = 0
    for start, end in spans:
        block = early[start:end]
        block_tokens = estimate_message_tokens(block)
        if block_tokens > bound_tokens:
            if current:
                chunks.append(current)
                current, current_tokens = [], 0
            chunks.append(_clip_block_to_budget(block, bound_tokens))
            continue
        if current and current_tokens + block_tokens > bound_tokens:
            chunks.append(current)
            current, current_tokens = [], 0
        current.extend(block)
        current_tokens += block_tokens
    if current:
        chunks.append(current)
    return chunks


def _plan_block_text(plan_items: tuple[PlanItem, ...]) -> str | None:
    """#1026：分块 final 请求的确定性计划块（spike 发现 2）。

    W-29 #383 的计划闸要求第 5 节逐字包含 in_progress 项内容；分块递进携带
    下中间转述不可靠（spike 变体 A/B/C 实证闸红），把 `derive_plan` 窗口产物
    逐字注入 final 请求是确定性缓解。零未完成项不注入（prompt 语义：第 5 节
    写 (none)，与 `_validate_plan_section` 的 A-sem 放宽一致）。
    """
    unfinished = [item for item in plan_items if item.status != "completed"]
    if not unfinished:
        return None
    lines = "\n".join(
        f"- [{item.status}] {item.content}（{item.active_form}）"
        for item in unfinished
    )
    return (
        "进行中计划项（摘要第 5 节必须逐字包含以下条目的 content 文本）：\n"
        + lines
    )


def _deterministic_truncation(
    messages: list[AnyMessage],
    prefix_end: int,
    cut: int,
    budget_tokens: int,
) -> list[AnyMessage] | None:
    """#1026 裁决 A（修法③）：摘要彻底失败时的确定性截断降级。

    规格依据：spec 06 §5 明文 deterministic fallback（ADR-0007 第 2 层原始
    设计；#348 的"摘要失败 ≠ 压缩发生"冻结语义经用户 2026-10-12 裁决部分
    回调）。语义：保留 recent（当前用户轮）与前导系统前缀**逐字**，early 段
    按 tool 原子块**从尾部向前**尽量多保留（偏好最近），压回预算（auto_limit
    − reserved）内；不发送超窗请求（spec 06 §8 hard guard 不违反）。截断
    不产生摘要、不写 bracket（compacted_turn_count=0 + fallback_used=True），
    失败面经 attempt=0 的 `deterministic_truncation_fallback` 诊断落任务可见。

    prefix + recent 自身就超预算 ⇒ None（完全不可截断），调用方落既有
    ContextWindowExceededError 兜底。
    """
    prefix, recent = messages[:prefix_end], messages[cut:]
    base = [*prefix, *recent]
    kept_tokens = estimate_message_tokens(base)
    if kept_tokens >= budget_tokens:
        return None
    boundaries = _tool_block_boundaries(messages, prefix_end, cut)
    spans = [*pairwise(boundaries), (boundaries[-1], cut)]
    kept: list[AnyMessage] = []
    for start, end in reversed(spans):
        block = messages[start:end]
        block_tokens = estimate_message_tokens(block)
        if kept_tokens + block_tokens >= budget_tokens:
            break
        kept = [*block, *kept]
        kept_tokens += block_tokens
    return [*prefix, *kept, *recent]


def _fallback_message_budget(
    auto_limit: int, reserved_tokens: int, token_estimate: int,
    messages: list[AnyMessage],
) -> int:
    """#1026 修回（审查 P3）：降级截断的 messages 预算——非 messages 开销**单扣**。

    reserved_tokens（builder 在构造点声明的加回份额：pf/sys/rt/plan/paths，见
    builder.py 的 reserved_tokens 组装）与 overhead（调用方 token_estimate 的实测
    非 messages 份额 = token_estimate − messages 估算）描述的是**同一笔**加回，
    不是两笔——旧码 `auto − reserved − overhead` 把 sys/rt/plan/paths/pf 五项
    扣了两次，方向保守（只多截不超窗）但会多丢 early 轮。取二者较大者扣一次：
    两个口径任一成立时「截断产物 + 加回 ≤ auto_limit」都守得住，且保留 B1 修正
    的防线（有效用量远超 auto 时预算为负 ⇒ 不可截断 ⇒ 落既有异常兜底，不会
    假成功放行超窗请求）。
    """
    overhead = max(token_estimate - estimate_message_tokens(messages), 0)
    return auto_limit - max(reserved_tokens, overhead)


@dataclass(frozen=True)
class CompactionFailure:
    """一次摘要尝试的失败记录（W-04 #348）。

    `message` 只装本项目自己的拒绝文案或异常**类型名**——绝不透传 provider
    回显原文（与 ADR-0033 边界 1 同一条脱敏纪律）。`auto_limit` / `hard_limit`
    取触发时的整型阈值读数；`token_estimate` 是调用方进入压缩时的估算。
    例外（`attempt=0` 标记**非摘要尝试**，摘要尝试是 1/2）：T12h (#647) 的生成后
    来源拒绝（`source_range_unavailable`）、#639 的预检请求超限拒绝
    （`preflight_request_exceeds_hard_limit`）、#648 的无可压缩早期轮拒绝
    （`no_compactable_early_turn`）——三者都是 0 次尝试，不伪造摘要尝试。
    """

    attempt: int
    error_class: str
    message: str
    auto_limit: int
    hard_limit: int
    token_estimate: int
    summary_model_id: str | None
    duration_ms: int
    request_token_estimate: int
    request_budget_tokens: int
    #: #639 阶段 A：该诊断是否为**缩小段**判定（False = 全段预检）。仅预检拒绝
    #: （attempt=0）会置位；其余诊断/摘要尝试失败保持默认假值。有界布尔，不撑载荷。
    narrowed: bool = False
    #: #1041（spec 06 §8）：本轮失败保存的**原始 early 窗口全文** raw artifact 的 ref
    #: （内容寻址 `artifact_id` = sha256(content)[:16]）。**由调用方（builder）在落盘
    #: 后回填**——compactor 纯变换、不接触 store，故 compactor 直调结果恒为 None；
    #: store 缺席或未落盘时同样保持 None（绝不伪造 ref）。有界 16hex。
    artifact_ref: str | None = None


class _SummaryRejected(ValueError):
    """摘要尝试内的单次拒绝（#348）：携带 `_SUMMARY_ERROR_CLASSES` 里的一个类。

    ValueError 子类：与既有校验 helper 的 ValueError 族在同一 except 面内被收拢，
    直接调用这些 helper 的既有测试不受影响。
    """

    def __init__(self, error_class: str, message: str) -> None:
        super().__init__(message)
        self.error_class = error_class


class ContextWindowExceededError(RuntimeError):
    """无法构造安全的模型上下文，调用方必须停止当前 run。

    语义上覆盖"写前"（预检 / 校验 / 源区间不可用）与"写后"（bracket 已落盘但复核
    未过）两类；区分二者用子类 `CompactionPostWriteError`——只有"写前"失败才允许
    被手动路径吞成"水位过低、未改动"，"写后"失败必须响亮（历史已多出 bracket）。
    """

    def __init__(
        self, message: str, *,
        failures: list[CompactionFailure] | None = None,
        raw_artifact_content: str | None = None,
    ) -> None:
        super().__init__(message)
        #: W-04 (#348)：双次摘要尝试的失败记录（最后一次在末尾）。
        #: #639 的"摘要请求本身超限"与 #648 的"无可压缩早期轮"两类预检超限是
        #: 例外——各携带一条 attempt=0 的诊断（非摘要尝试），经抛错路径的
        #: failures 由调用方落任务可见状态。#648 之前"保持为空表"是刻意设计，
        #: 2026-10-06 用户批准选项 B 后刻意反转（只加诊断，判定与抛点不动）。
        self.failures = list(failures or [])
        #: #1041（spec 06 §8）：摘要失败出口（双失败不可截断 / 预检拒绝无法缩小）时，
        #: **原始 early 窗口 `messages[prefix_end:cut]` 的完整转录文本**（与摘要请求
        #: 同形；含"截断保留 + 逐出"两部分，非仅被逐出集）。由 builder 落盘成 raw
        #: artifact 并回填 ref（§8「摘要失败保存 raw artifact」）。**纯文本搬运**，
        #: compactor 不接触 store；其余抛点保持 None（非"摘要失败"语义）。
        #: 落盘另需**存在可存的 early 原文**：会话虽因失败受损、但 early 为空、无原文可存
        #: 的出口（如 `no_compactable_early_turn`：无可压缩完整早期轮）同样保持 None、不落盘。
        self.raw_artifact_content = raw_artifact_content


class CompactionPostWriteError(ContextWindowExceededError):
    """bracket 三事件**已落盘**之后的复核失败（F2 #635）。

    两个抛点都在 `ContextBuilder.compact_now` 的 bracket 写入**之后**：重投影确认
    不一致、或重投影仍越硬护栏。历史里已有 bracket（append-only，不删除），因此
    这不是"未改动"——调用方**不得**把它吞成 below-floor DTO，必须响亮失败（Web
    500 / CLI exit 1），否则会谎报"未改动"而历史实际已变。

    `bracket_id` 指向已写入的那一对 bracket 事件，供调用方在回执/诊断里指认。
    """

    def __init__(self, message: str, *, bracket_id: str) -> None:
        super().__init__(message)
        self.bracket_id = bracket_id


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
    # #647 T11f：bracket_id 是"持久化身份"——ContextCompactor.compact()
    # 永不铸造（成功也返回 None，对标 Pi/DSH 的无 id 摘要结果）；只有
    # ContextBuilder 在决定持久化时才铸造并回填。调用方不得把 compactor
    # 直调结果（bracket_id 恒为 None）当作可持久化/可信任的 bracket。
    source_seq_start: int | None = None
    source_seq_end: int | None = None
    bracket_id: str | None = None
    summary: str | None = None
    summary_model_id: str | None = None
    duration_ms: int | None = None
    request_token_estimate: int | None = None
    request_budget_tokens: int | None = None
    # W-04 (#348)：本次 compact 里失败过的摘要尝试（成功尝试之前的都在内；
    # 双失败安全继续时是两条）。调用方（builder）负责把它们落成任务可见状态。
    failures: list[CompactionFailure] = dataclass_field(default_factory=list)
    #: #639 阶段 A：本次压缩是否**缩小了 early 段**（preflight 超限后缩小重试成功）。
    #: 成功走缩小段时置位，并在 `CONTEXT_COMPACTED` 事件里持久化；其余路径默认假值，
    #: 逐字节等价。`source_seq_*` 与八节摘要此时只覆盖缩小段（非全 early 段）。
    narrowed: bool = False
    #: #1026：本次摘要是否走分块路径（请求超分块阈值且可切 >1 块）。成功时置位，
    #: 经 `CONTEXT_COMPACTED` 事件持久化（验收 3 的事件可观测）；其余路径默认假值。
    chunked: bool = False
    #: #1041（spec 06 §8，R1 裁决）：摘要双失败后走**确定性截断降级**（fallback_used
    #: =True）时，**原始 early 窗口 `messages[prefix_end:cut]` 的完整转录文本**（与摘要
    #: 请求同形；含"截断保留 + 逐出"两部分，是"逐出集"的超集）。由 builder 落盘成 raw
    #: artifact 并回填 `CompactionFailure.artifact_ref`。**纯文本搬运**——compactor 不
    #: 接触 store；成功压缩 / 低水位 / 安全继续等路径保持 None。
    raw_artifact_content: str | None = None


#: T4 (#134)：摘要 prompt 的正文已迁到 `agent_harness.prompt.builtin`
#: （section `aux:compaction`）——改文案开那一个文件。


def _render_transcript(messages: list[AnyMessage]) -> str:
    """把一段消息渲染成摘要请求 / 失败快照共用的 JSON 转录文本。

    这是摘要请求与失败快照共用的**唯一**格式——摘要请求三处（单请求摘要
    `compact`、缩小段重试、分块逐块）+ 失败快照两处（#1041 的预检拒绝抛出、
    确定性截断降级），共 5 个调用点。抽成单点避免"同形靠复制"漂移（#1041 审查
    P4-2）——任一处改格式必须全体一致，否则摘要与快照会静默分叉。
    """
    return json.dumps(
        [message.model_dump(mode="json") for message in messages],
        ensure_ascii=False,
    )


def compactable_early_window(messages: list[AnyMessage]) -> tuple[int, int]:
    """early 压缩窗口的判据：返回 `(prefix_end, cut)`。

    - `prefix_end`：跳过的前导 **非摘要** `SystemMessage` 数。前导摘要
      （`_is_compaction_summary`，含旧版 SystemMessage 形态）是 early 窗口的**起点**，
      不是可跳过的前缀——遇它即停。
    - `cut`：最后一条 `HumanMessage` 的下标（无 HumanMessage 时回落到 `prefix_end`）。

    `[prefix_end:cut]` 即待摘要的 early 段、`[cut:]` 是保留的 recent 段。
    `ContextCompactor.compact`（自动路径）与 dry-run 预览（`_has_compactable_early_turn`）
    共用本函数——判据只有一份，此前两处各抄一遍会漂移（G3 #635）。
    """
    prefix_end = 0
    while (prefix_end < len(messages)
           and isinstance(messages[prefix_end], SystemMessage)
           and not _is_compaction_summary(messages[prefix_end])):
        prefix_end += 1
    cut = max((i for i, message in enumerate(messages)
               if isinstance(message, HumanMessage)), default=prefix_end)
    return prefix_end, cut


def _tool_block_boundaries(
    messages: list[AnyMessage], start: int, end: int,
) -> list[int]:
    """`[start:end)` 内每个 tool 原子块 / 独立消息的**起点**下标（升序）。

    块定义与 `_validate_tool_blocks` 同源：`AIMessage(tool_calls)` + 其后连续
    `ToolMessage` 串为一个块；其余消息各自为一块。用于把缩小切点吸附到块边界。
    """
    boundaries: list[int] = []
    index = start
    while index < end:
        boundaries.append(index)
        message = messages[index]
        if isinstance(message, AIMessage) and message.tool_calls:
            index += 1
            while index < end and isinstance(messages[index], ToolMessage):
                index += 1
        else:
            index += 1
    return boundaries


def narrow_early_window(
    messages: list[AnyMessage], prefix_end: int, cut: int,
) -> int | None:
    """preflight 超限时的缩小切点：返回缩小段右端 `narrow_cut`，或 `None`。

    `compactable_early_window` 给出 `[prefix_end:cut]` 的 early 段后，本函数**只动
    区间选择**：从 early 段尾部向前累计 `estimate_message_tokens`，保留尾部约
    `_RETAIN_RATIO`（16%）的 recent（`[narrow_cut:cut]` 逐字留在投影、不摘要），
    只把前缀段 `[prefix_end:narrow_cut]` 作为新摘要段。切点**吸附到 tool 块起点**
    （`_tool_block_boundaries`），使缩小段与保留尾段各自都是完整 tool 块序列
    （`_validate_tool_blocks` 可过）。

    吸附方向选**向前吸附到块起点**（即只可能比 16% 切点更靠前）：它保证缩小段的
    摘要请求只缩不增（单调不回退），这正是重试的目的——向后吸附会把半个块并入
    摘要段、反而可能再次越 hard。

    无可缩小段（`narrow_cut == prefix_end`，整个 early 都被保留）时返回 `None`，
    调用方跳过重试、走既有两条出口——保证重试**最多一次**且不改判定式。
    """
    if cut <= prefix_end:
        return None
    early = messages[prefix_end:cut]
    target = _RETAIN_RATIO * estimate_message_tokens(early)
    accumulated = 0
    narrow_cut = cut
    for index in range(cut - 1, prefix_end - 1, -1):
        accumulated += estimate_message_tokens([messages[index]])
        narrow_cut = index
        if accumulated >= target:
            break
    boundaries = _tool_block_boundaries(messages, prefix_end, cut)
    boundary = max(
        (candidate for candidate in boundaries if candidate <= narrow_cut),
        default=None,
    )
    if boundary is None or boundary <= prefix_end or boundary >= cut:
        return None
    return boundary


class ContextCompactor:
    def __init__(self, model_provider: Any, *, max_context_tokens: int = 200_000,
                 auto_compact_threshold: float = 0.70,
                 hard_guard_threshold: float = 0.85,
                 keep_recent_tokens: int = 20_000,
                 summary_timeout_seconds: float = 30.0,
                 summary_timeout_per_kilotoken_seconds: float = 0.25,
                 summary_chunk_request_ratio: float = 0.5,
                 summary_chunk_max_tokens: int = 24_000,
                 summary_model: Any | None = None,
                 model_call_gate: ModelCallGate | None = None) -> None:
        if max_context_tokens <= 0 or not 0 < auto_compact_threshold <= hard_guard_threshold <= 1:
            raise ValueError("invalid context budget")
        if summary_timeout_seconds <= 0:
            raise ValueError("summary_timeout_seconds must be positive")
        # #1026：墙钟斜率（候选①线性缩放）——0 = 固定墙钟（候选②的退化形态）。
        if summary_timeout_per_kilotoken_seconds < 0:
            raise ValueError("summary_timeout_per_kilotoken_seconds must be non-negative")
        if not 0 < summary_chunk_request_ratio <= 1:
            raise ValueError("summary_chunk_request_ratio must be in (0, 1]")
        if summary_chunk_max_tokens <= 0:
            raise ValueError("summary_chunk_max_tokens must be positive")
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
        # #1026：墙钟缩放斜率与分块参数（Settings 透传链见 builder/assembly）。
        self._timeout_per_kilotoken = summary_timeout_per_kilotoken_seconds
        self._chunk_ratio = summary_chunk_request_ratio
        self._chunk_max_tokens = summary_chunk_max_tokens
        self._max_context_tokens = max_context_tokens
        self.reserve = max(int(max_context_tokens * 0.15), 16384)

    def _slot(self) -> AbstractAsyncContextManager[None]:
        """取一个**新**的摘要槽位（每次尝试各取一次；None = 不过闸）。

        `ModelCallGate.slot()` 是 `@asynccontextmanager` 产物，**一次性**（同
        fallback coordinator 的教训，见 model/fallback.py `_slot`）；取消路径
        由 slot 的 finally 归还 permit。
        """
        return self._gate.slot() if self._gate is not None else nullcontext(None)

    def _summary_timeout_for(self, request_token_estimate: int) -> float:
        """#1026：摘要请求的墙钟随**本次请求规模**线性缩放（消融候选①）。

        `base + kt × slope`：base = `summary_timeout_seconds`（Settings，默认 30——
        小请求维持既有墙钟，不因大请求的容忍而陪等）；slope =
        `summary_timeout_per_kilotoken_seconds`（默认 0.25s/kt）。锚点（阶段 A
        实测）：同路径 167k 请求 16.8s 成功、30s 恒败 ⇒ 167k → 71.75s（≈4.3×
        实测延迟）、30k → 37.5s。消融候选②（固定 90s）= slope 0 的配置形态：
        对 30k 小请求也等 90s，真实失败重试成本翻三倍，落选（#1026 消融记录；
        两候选的公式/锚点断言锁定在 tests/context/test_1026_compaction_timeout.py
        的 `test_timeout_formula_scales_with_request_size`）。
        每次摘要尝试用**当时**的请求规模取墙钟——两次 attempt / 分块逐块不再
        共用同一恒定值（票面修法①）。
        """
        return (self._summary_timeout
                + request_token_estimate / 1000 * self._timeout_per_kilotoken)

    async def _summarize_chunked(
        self, chunks: list[list[AnyMessage]], prompt: SystemMessage,
        plan_block: str | None, *, request_sizes: list[int],
    ) -> AIMessage:
        """#1026：分块递进归并摘要（spike 验证的首选③变体，PORT DESIGN 依据见下）。

        按序逐块请求：每块 = prompt + [前段摘要携带（围栏包裹，防保留标题行
        裸露——spike 变体 D2 实证裸露打红结构闸、D3/D4 实证围栏经 `_fenced_mask`
        解析器成立）] + [确定性计划块] + 本块转录；上一块的**原始输出**作为下一块
        的携带参考，最后一块的输出即覆盖全 early 的最终四节产物，交回调用方走
        既有八节组装与校验闸门（spike 变体 D/D3/D4 ALL_PASS）。每次请求各自取
        槽位与按块规模的墙钟（`_summary_timeout_for`）——单请求 token 有界
        （验收 3）。中间块产物不进闸门，只做携带（≤ `_CHUNK_CARRY_CHAR_CAP`
        确定性截断）；中间块响应形状违规按单请求面同判（tool_calls / 非文本 /
        空文本即拒，不把坏携带传染给后续块）。每次实际发起的块请求规模经
        `request_sizes` 回传调用方（失败诊断的逐块口径，#1026 修回）。

        来源：aider `summarize_real`（Apache-2.0，`Aider-AI/aider` @
        `5dc9490b`，`aider/history.py:33-95`）的递归有界分块 + LangChain
        SummarizationMiddleware（MIT，`langchain-ai/langchain` @ `e4b143729`）
        `trim_tokens_to_summarize` 的"摘要输入有界"原则。PORT DESIGN。
        """
        carry = ""
        response: AIMessage | None = None
        for index, chunk in enumerate(chunks):
            parts: list[AnyMessage] = [prompt]
            if carry:
                parts.append(HumanMessage(content=(
                    "已生成的前段摘要（供合并参考；其中的标题行不是输出格式，"
                    "最终只输出四节）：\n```\n" + carry + "\n```"
                )))
            if plan_block is not None:
                parts.append(HumanMessage(content=plan_block))
            parts.append(HumanMessage(content=_render_transcript(chunk)))
            chunk_request_tokens = estimate_message_tokens(parts)
            # #1026 修回（审查 P4）：逐块请求的**实际**规模回传给调用方——失败
            # 诊断的 request_token_estimate 在分块路径记逐块口径，不再记全 early
            # 单请求估算（两口径可区分，见 attempt 循环处注释）。
            request_sizes.append(chunk_request_tokens)
            async with self._slot(), asyncio.timeout(
                self._summary_timeout_for(chunk_request_tokens),
            ):
                response = await self._model.ainvoke(parts)
            if not isinstance(response, AIMessage) or response.tool_calls:
                raise _SummaryRejected(
                    "tool_calls_in_response",
                    "Summary must be text without tool calls",
                )
            if not isinstance(response.content, str):
                raise _SummaryRejected("non_text", "Summary must be plain text")
            if not response.content.strip():
                raise _SummaryRejected("empty_summary", "Summary must not be empty")
            if index < len(chunks) - 1:
                carry = response.content
                if len(carry) > _CHUNK_CARRY_CHAR_CAP:
                    carry = (
                        carry[:_CHUNK_CARRY_CHAR_CAP]
                        + "…［已截断：携带参考超上限］"
                    )
        assert response is not None
        return response

    async def compact(
        self,
        messages: list[AnyMessage],
        token_estimate: int,
        *,
        events: list[SessionEvent] | None = None,
        protected_facts: list[ProtectedFact] | None = None,
        reserved_tokens: int = 0,
        source_ranges: list[tuple[int, int] | None] | None = None,
        supports_vision: bool = False,
    ) -> CompactionResult:
        if reserved_tokens < 0:
            raise ValueError("reserved_tokens must be non-negative")
        _validate_tool_blocks(messages)
        prefix_end, cut = compactable_early_window(messages)
        prefix = messages[:prefix_end]
        early, recent = messages[prefix_end:cut], messages[cut:]
        if not early:
            count = estimate_message_tokens(messages)
            # 硬护栏比对用**有效用量**（调用方入参，含 sys+rt+plan），与下方其余
            # 三处 `token_estimate > self._hard_limit` 同一口径——messages-only
            # 比对会在 (messages, 有效用量) 落入 (count≤hard<effective) 缝隙时
            # 放行越窗请求。
            if token_estimate > self._hard_limit:
                # #648 选项 B（用户 2026-10-06 批准）：本拒绝加 #639 同等诊断待遇。
                # 此前"保持为空表"是刻意设计，本次是刻意的反转——**只加诊断**：
                # 判定式（`>`）、抛点、异常类型一字不动；诊断挂异常的 failures 上，
                # 由 builder 既有 except 经 #348 落任务可见状态。切分逻辑与 hard
                # 行为不碰（票面铁律）。
                raise ContextWindowExceededError(
                    _NO_EARLY_TURN_MESSAGE,
                    failures=[self._no_early_turn_rejection(
                        token_estimate=token_estimate, atomic_tokens=count,
                    )],
                )
            return CompactionResult(list(messages), 0, count, False)
        # C-6 #642：early 段每条消息的来源区间（提前到摘要尝试之前，与成功后
        # 的 source_seq 区间计算共用同一次推导；语义与原成功路径逐字一致）。
        # #710 方向 C：同一份 early_ranges 同时供第 0 节承载位的来源指针
        # （_assemble_summary/_validate_summary）复用。
        # W-03 (#347)：裁剪后的投影消息与 derive 产物**内容不再逐条相等**
        # （ToolMessage.content 原位替换），但消息数与顺序不变——调用方
        # 传来的 ranges 与 messages 位置一一对应，直接采用、跳过相等对齐。
        # 长度不符视同区间不可用（走既有拒绝路径），不做静默截断。
        early_ranges = _early_source_ranges(
            messages, events, source_ranges, prefix_end, cut,
            supports_vision=supports_vision,
        )
        trusted_summaries = _trusted_summary_indices(early, early_ranges, events)
        prompt = SystemMessage(
            content=DEFAULT_REGISTRY.assemble("aux:compaction").system_text
        )
        transcript = HumanMessage(content=_render_transcript(early))
        request = [prompt, transcript]
        request_token_estimate = estimate_message_tokens(request)
        # 摘要尝试的失败记录（W-04 #348；成功尝试之前的都在内）。预检拒绝的诊断
        # （#639）也挂在这里——它在任何摘要尝试之前，但契约同样要求带给调用方。
        failures: list[CompactionFailure] = []
        narrowed = False
        # Preflight rejection is not a summary attempt: it emits one bounded,
        # task-visible failure record with attempt=0 (#639) and never calls the
        # summary model. The `>` comparison and both exits below are unchanged.
        if request_token_estimate > self._hard_limit:
            logger.warning(
                "Context compaction rejected; summary request exceeds hard guard",
            )
            # #639 阶段3a：预检拒绝也走 #348 有界失败通道（attempt=0 = 非摘要尝试），
            # 把"为什么拒绝、差多少"（request_token_estimate vs hard_limit）落成
            # 任务可见状态。**只加诊断**：判定式、两档阈值与两条出口语义一字不动。
            failures.append(self._preflight_rejection(
                narrowed=False, request_token_estimate=request_token_estimate,
                token_estimate=token_estimate,
            ))
            # #639 阶段 A：走两条出口**之前**，只缩一次区间重试（有界）。照
            # deepseek-harness `selectCompactableRange`（PORT DESIGN）：保留尾部
            # 16% recent、不拆 tool pair。判定式一字不动——缩小段用**同一判定式**
            # 重新 preflight 一次；通过则改用它走正常摘要流程，仍超则回落出口。
            narrow_cut = narrow_early_window(messages, prefix_end, cut)
            if narrow_cut is not None:
                narrowed_early = messages[prefix_end:narrow_cut]
                narrowed_request = [
                    prompt, HumanMessage(content=_render_transcript(narrowed_early)),
                ]
                narrowed_token_estimate = estimate_message_tokens(narrowed_request)
                if narrowed_token_estimate > self._hard_limit:
                    # 缩小后仍超限：补一条 narrowed 诊断，回落既有两条出口。
                    failures.append(self._preflight_rejection(
                        narrowed=True,
                        request_token_estimate=narrowed_token_estimate,
                        token_estimate=token_estimate,
                    ))
                else:
                    # 缩小段放行：改用它重组摘要请求（attempt 1/2 语义不变），
                    # 保留尾段 `[narrow_cut:]`（含原 recent）逐字留在投影里。
                    early = narrowed_early
                    early_ranges = (
                        early_ranges[:narrow_cut - prefix_end]
                        if early_ranges is not None else None
                    )
                    trusted_summaries = _trusted_summary_indices(
                        early, early_ranges, events,
                    )
                    request = narrowed_request
                    request_token_estimate = narrowed_token_estimate
                    recent = messages[narrow_cut:]
                    narrowed = True
            if not narrowed:
                if token_estimate > self._hard_limit:
                    # #1041（spec 06 §8，R1 裁决）：预检拒绝 + 无法缩小 + 有效用量仍
                    # 超 hard ⇒ 本出口 raise（会话 run/paused、早期原文离开模型视野），
                    # 与"双失败不可截断"同属失败面 ⇒ 同样带出**原始 early 窗口**全文，
                    # 由 builder 落盘成 raw artifact 并回填 ref（F2 修回）。此处 `early`
                    # 未被缩小（`narrowed` 为假）⇒ `messages[prefix_end:cut]` 即原始窗口。
                    raise ContextWindowExceededError(
                        "Summary validation failed and original context exceeds hard guard",
                        failures=failures,
                        raw_artifact_content=_render_transcript(messages[prefix_end:cut]),
                    ) from None
                # P2-4：token_estimate 回传 messages-only（契约见 CompactionResult），
                # 不原样回传调用方入参（其已含 sys+rt+plan，builder 会再补一次）。
                return CompactionResult(
                    list(messages), 0, estimate_message_tokens(messages), False,
                    failures=failures,
                )
        # W-04 (#348)：摘要生成至多两次尝试。一次尝试 = ainvoke → 解析 → 程序化组装
        # → 校验闸门 → shrink 全链；预检（上面与 tool 块检查）不计入。每次失败记一条
        # 有界 CompactionFailure，由调用方落成任务可见状态。
        summary_text = ""
        compacted: list[AnyMessage] | None = None
        summary_started_at = monotonic()
        summary_model_id: str | None = None
        # W-29 (#383) + #844 A-vis：计划窗口投影**前移**到摘要尝试之前——#1026
        # 分块请求要在尝试内注入确定性计划块（spike 发现 2），窗口推导只依赖
        # early_ranges（此处置定），与原先在闸门处内联推导同一数据源、同一口径，
        # 闸门行为逐字节等价。摘要只覆盖 early 段 `[prefix_end:cut]`（含 #639
        # 缩小后的实际段），落在 cut 之后的计划更新对摘要器不可见，不应要求其
        # 逐字出现。窗口右界取既有 `early_ranges` 里**可用来源区间**的最大 seq
        # （与成功路径 `source_seq_start/source_seq_end` 同一数据源；缩小路径下
        # early_ranges 已被同步截断，天然一致）。单条消息没有来源区间（对齐失败／
        # derive 注入的 synthetic dangling ToolMessage）时该条不参与取 max，但不
        # 放弃整段——仅当**一条可用区间都没有**（early_ranges 为 None／为空／
        # 全是 None）时才回落全量行为。
        #
        # 与下方成功路径的 source-seq 判据**故意分道**：那里决定能否持久化，要求
        # 区间**完整**（有一条 None 即拒绝并给 source_range_unavailable 诊断）；
        # 此处只决定比较基准，尽力取可用窗口即可。两处口径不同是有意的，勿强行
        # 合并成一个 helper。
        #
        # 不变量（P4 复审提示）：计划更新只经 update_plan 工具调用写入，其事件
        # 投影为一条真实消息、必然带来源区间，故「early 窗口内可见的计划更新 ⊆
        # 本处 seq 过滤保留的集合」。若日后出现非投影式的计划更新写入口，需重新
        # 核对本窗口推导。
        plan_items: tuple[PlanItem, ...] = ()
        if events is not None:
            plan_events = events
            window_ranges = (
                [r for r in early_ranges if r is not None]
                if early_ranges else []
            )
            if window_ranges:
                window_end_seq = max(r[1] for r in window_ranges)
                plan_events = [
                    event for event in events if event.seq <= window_end_seq
                ]
            plan_items = derive_plan(plan_events).items
        # #1026（票面修法②）：分块决策——摘要请求超阈值（hard × ratio，票面
        # "如 > hard_limit 一半"）且可切出 **>1 块**才走分块；单块情形与单请求
        # 等价，不值得多跳。阈值以内维持单请求（既有路径逐字节等价）。
        chunks: list[list[AnyMessage]] | None = None
        plan_block: str | None = None
        chunked = False
        if request_token_estimate > self._hard_limit * self._chunk_ratio:
            candidate_chunks = _chunk_early(early, self._chunk_max_tokens)
            if len(candidate_chunks) > 1:
                chunks = candidate_chunks
                chunked = True
                plan_block = _plan_block_text(plan_items)
                # #1026 修回（审查 P2）：`_clip_block_to_budget` 如实放行的超界
                # 原子块在此逐条留痕（既有 failures 诊断通道，attempt=0）——
                # 「单请求 token 有界」的逃逸口必须事后可观测，不新造事件类型。
                for candidate_chunk in candidate_chunks:
                    candidate_tokens = estimate_message_tokens(candidate_chunk)
                    if candidate_tokens > self._chunk_max_tokens:
                        failures.append(self._chunk_bound_release(
                            chunk_tokens=candidate_tokens,
                            token_estimate=token_estimate,
                        ))
        for attempt in (1, 2):
            attempt_started_at = monotonic()
            attempt_model_id = _summary_model_id(self._model, None)
            # #1026 修回（审查 P4）：本次 attempt 逐块请求的实际规模（分块路径由
            # `_summarize_chunked` 回传）。失败诊断的 `request_token_estimate`
            # 口径随之分道：单请求路径记全 early 单请求估算，分块路径记已发起
            # 块请求的最大实际规模（≤ 单块预算 + 携带封顶，远小于全 early 转录
            # ——两口径可区分）。尚未发起任何块请求就失败时无逐块读数，回落
            # 全 early 估算。
            chunk_request_sizes: list[int] = []
            try:
                # #559：槽位在 timeout 外面取——排队等闸不计入摘要预算（墙钟是
                # 单次调用的预算，不是排队的）；取消/失败由 slot 的 finally 归还。
                # #1026：墙钟按**本次请求规模**取（`_summary_timeout_for`），两次
                # attempt / 分块逐块不再共用同一恒定值（票面修法①）。
                if chunks is not None:
                    response = await self._summarize_chunked(
                        chunks, prompt, plan_block,
                        request_sizes=chunk_request_sizes,
                    )
                else:
                    async with self._slot(), asyncio.timeout(
                        self._summary_timeout_for(request_token_estimate),
                    ):
                        response = await self._model.ainvoke(request)
                attempt_model_id = _summary_model_id(self._model, response)
                summary_model_id = attempt_model_id
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
                    early, model_sections, protected_facts, trusted_summaries,
                    source_ranges=early_ranges,
                )
                _validate_summary(
                    candidate_summary_text, early, protected_facts,
                    trusted_summaries, source_ranges=early_ranges,
                )
                # W-29 (#383)：摘要第 5 节与进度清单一致性闸门（PRD §6.1 表行 5，
                # 落盘前校验 = §4.4 闸门语义）。events 为 None（直连 compactor 的
                # 既有调用面）或会话无清单时 plan_items 为空 ⇒ 闸门不启用——空接缝
                # 语义保留，无清单会话的行为逐字节等价。窗口推导见尝试循环之前
                # （#1026 前移：分块请求注入计划块与闸门共用同一投影）。
                if events is not None:
                    _validate_plan_section(candidate_summary_text, plan_items)
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
                    summary_model_id=attempt_model_id,
                    duration_ms=max(round((monotonic() - attempt_started_at) * 1000), 0),
                    # #1026 修回（审查 P4）：分块路径记逐块请求实际规模的最大值
                    #（口径与单请求路径可区分，见循环顶部注释）；无逐块读数时
                    # 回落全 early 估算。
                    request_token_estimate=(
                        max(chunk_request_sizes)
                        if chunk_request_sizes else request_token_estimate
                    ),
                    request_budget_tokens=int(self._hard_limit),
                ))
                logger.warning(
                    "Context compaction attempt %s rejected (%s)",
                    attempt, failures[-1].error_class,
                )
        if compacted is None:
            # 两次尝试都失败：旧投影仍在硬护栏内则安全继续（下一稳定边界再评估）；
            # 已达硬护栏而仍无核验过的上下文 ⇒ 先走 #1026 裁决 A 的确定性截断
            # 降级（spec 06 §5 deterministic fallback），截断也救不了才停止。
            logger.warning(
                "Context compaction rejected; keeping original projection (%s)",
                failures[-1].error_class if failures else "unknown",
            )
            if token_estimate > self._hard_limit:
                # #1026 裁决 A（修法③）：分块/缩墙钟之后仍彻底失败 ⇒ 按 AIMessage
                # 原子边界确定性截断（保留 recent 逐字 + 尽量多 early，压回
                # auto_limit − reserved 内），带 fallback 标记产出 CompactionResult，
                # 不再必然 run/paused。诊断经 attempt=0 的
                # `deterministic_truncation_fallback` 落任务可见（既有 #348 通道）。
                # 预算必须按**有效用量**收敛：调用方入参 token_estimate 含
                # sys/rt/plan 等不在 messages 里的开销（builder 重建时会原样补回），
                # 截断只缩 messages ⇒ 预算要扣掉这笔开销份额，否则"messages 压回
                # 线内、有效用量仍超护栏"的假成功会把超窗请求放出去。开销**单扣**
                # （修回口径见 `_fallback_message_budget`）——reserved 与 overhead
                # 是同一笔加回的两种计量，不叠加。
                # #1041（spec 06 §8，R1 裁决）：**原始 early 窗口 `messages[prefix_end:cut]`
                # 的完整转录文本**（含被截断保留与逐出两部分——是"逐出集"的超集），带
                # 出给 builder 落盘成 raw artifact（截断降级成功分支同样保存）。用**原始**
                # 窗口（不受缩小段/截断影响），保证快照完整、格式与摘要转录同形。
                raw_early_text = _render_transcript(messages[prefix_end:cut])
                truncated = _deterministic_truncation(
                    messages, prefix_end, cut,
                    _fallback_message_budget(
                        self._auto_limit, reserved_tokens, token_estimate,
                        messages,
                    ),
                )
                if truncated is not None:
                    failures.append(CompactionFailure(
                        attempt=0,
                        error_class="deterministic_truncation_fallback",
                        message=(
                            "Summary failed twice; deterministically truncated "
                            "early turns to fit the auto limit"
                        ),
                        auto_limit=int(self._auto_limit),
                        hard_limit=int(self._hard_limit),
                        token_estimate=token_estimate,
                        summary_model_id=summary_model_id,
                        duration_ms=max(
                            round((monotonic() - summary_started_at) * 1000), 0),
                        request_token_estimate=request_token_estimate,
                        request_budget_tokens=int(self._hard_limit),
                    ))
                    logger.warning(
                        "Context compaction fell back to deterministic "
                        "truncation (%s -> %s tokens)",
                        token_estimate, estimate_message_tokens(truncated),
                    )
                    return CompactionResult(
                        truncated, 0, estimate_message_tokens(truncated), True,
                        failures=failures,
                        raw_artifact_content=raw_early_text,
                    )
                raise ContextWindowExceededError(
                    "Summary validation failed and original context exceeds hard guard",
                    failures=failures,
                    raw_artifact_content=raw_early_text,
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
        # T4 (#134)：从投影映射计算 source_seq 区间（区间推导已在压缩前完成，
        # 见 `early_ranges`）。旧摘要带有原 bracket 的完整来源范围，因此下一次
        # 压缩可以覆盖并替代之前的摘要。
        source_seq_start: int | None = None
        source_seq_end: int | None = None
        if events is not None and early_ranges and all(
            source_range is not None for source_range in early_ranges
        ):
            source_seq_start = min(source_range[0] for source_range in early_ranges)
            source_seq_end = max(source_range[1] for source_range in early_ranges)
        if events is not None and (source_seq_start is None or source_seq_end is None):
            logger.warning(
                "Context compaction rejected; source event range is unavailable",
            )
            # T12h (#647)：来源拒绝发生在摘要生成**之后**（候选已过全部校验闸门，
            # 但无 source event range 可持久化）——补一条有界失败记录，使调用方
            # 可区分"无须压缩"（预检 0 次尝试 ⇒ failures 空）与"生成后来源失配"。
            # attempt=0 标记"非摘要尝试"；不为此伪造第 3 次摘要尝试。
            failures.append(CompactionFailure(
                attempt=0,
                error_class="source_range_unavailable",
                message="Cannot persist compaction without a source event range",
                auto_limit=int(self._auto_limit),
                hard_limit=int(self._hard_limit),
                token_estimate=token_estimate,
                summary_model_id=summary_model_id,
                duration_ms=max(round((monotonic() - summary_started_at) * 1000), 0),
                request_token_estimate=request_token_estimate,
                request_budget_tokens=int(self._hard_limit),
            ))
            if token_estimate > self._hard_limit:
                raise ContextWindowExceededError(
                    "Cannot persist compaction without a source event range",
                    failures=failures,
                )
            # P2-4：messages-only（契约见 CompactionResult），不再原样回传入参。
            return CompactionResult(
                list(messages), 0, estimate_message_tokens(messages), False,
                failures=failures,
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
            # #647 T11f：身份不在此铸造（照搬成熟产品）——
            # Pi（earendil-works/pi @28dcce2b）compaction.ts:104：
            # "Result from compact() - SessionManager adds uuid/parentUuid when saving"，
            # 身份只在 session-manager.ts appendCompaction（L1262-1288）持久化时
            # 由 generateId（L277）铸造；
            # DeepSeek Harness（@5badb150）summarizer.ts:87-106 的 SummaryResult
            # 无 id 字段，compactionId 在 region.ts:204
            # session.append('compaction/start') 前一刻铸造（types.ts:95：
            # "Stable identity shared by this compaction's complete durable
            # lifecycle."）。
            # 无溯源 ⇒ 无身份；有溯源结果的身份也由持久化方（builder）铸造。
            bracket_id=None,
            summary=summary_text,
            summary_model_id=summary_model_id,
            duration_ms=max(round((monotonic() - summary_started_at) * 1000), 0),
            request_token_estimate=request_token_estimate,
            request_budget_tokens=int(self._hard_limit),
            # W-29 (#383) 补课：成功前的失败尝试也要带给调用方落任务可见状态——
            # CompactionResult.failures 的契约（"成功尝试之前的都在内"）与冻结
            # PRD §4.5（"每次失败留诊断与任务可见状态"）都要求这一条；此前成功
            # 路径漏传，首试被拒、重试成功时失败记录被静默丢弃（本票新闸门使
            # 该路径成为常态，判据测试暴露）。
            failures=failures,
            # #639 阶段 A：成功走缩小段时置位（marker 落 CONTEXT_COMPACTED 事件）。
            narrowed=narrowed,
            # #1026：成功走分块路径时置位（marker 落 CONTEXT_COMPACTED 事件）。
            chunked=chunked,
        )

    def _no_early_turn_rejection(
        self, *, token_estimate: int, atomic_tokens: int,
    ) -> CompactionFailure:
        """#648 选项 B：无可压缩完整早期轮拒绝的有界诊断记录。

        照搬 #639 `_preflight_rejection` 的结构：`attempt=0`（非摘要尝试）、
        `error_class` 收进既有有界词表。`request_token_estimate` 取卡住的原子块
        自身 token 数（判别式左值语义：这是压不动的东西），`token_estimate` 取
        调用方有效用量（与 hard 比对的左操作数，"差多少"从二者读出）。
        """
        return CompactionFailure(
            attempt=0,
            error_class="no_compactable_early_turn",
            message="No complete early turn can be compacted",
            auto_limit=int(self._auto_limit),
            hard_limit=int(self._hard_limit),
            token_estimate=token_estimate,
            summary_model_id=None,
            duration_ms=0,
            request_token_estimate=atomic_tokens,
            request_budget_tokens=int(self._hard_limit),
        )

    def _preflight_rejection(
        self, *, narrowed: bool, request_token_estimate: int, token_estimate: int,
    ) -> CompactionFailure:
        """#639：预检拒绝（任何摘要尝试之前）的有界诊断记录。

        `narrowed` 标记该判定是否为**缩小段**（阶段 A）——False = 全段预检，
        True = 缩小段仍超限。`attempt=0`（非摘要尝试）、`error_class` 收在既有
        有界词表里，`request_token_estimate` 是本次判定用的请求令牌数（判别式左值）。
        """
        return CompactionFailure(
            attempt=0,
            error_class="preflight_request_exceeds_hard_limit",
            message="Summary request exceeds hard guard before any summary attempt",
            auto_limit=int(self._auto_limit),
            hard_limit=int(self._hard_limit),
            token_estimate=token_estimate,
            summary_model_id=None,
            duration_ms=0,
            request_token_estimate=request_token_estimate,
            request_budget_tokens=int(self._hard_limit),
            narrowed=narrowed,
        )

    def _chunk_bound_release(
        self, *, chunk_tokens: int, token_estimate: int,
    ) -> CompactionFailure:
        """#1026 修回（审查 P2）：超界原子块如实放行的有界诊断记录。

        `_clip_block_to_budget` 对 content 已不可再裁（非文本/超短）的原子块
        「如实放行」——票面验收 3 的「分块单请求 token 有界」在此有逃逸口
        （墙钟按实际规模缩放兜住超时面，但有界承诺破）。照搬 `_preflight_rejection`
        结构使放行事后可观测：`attempt=0`（非摘要尝试）、error_class 收进既有
        有界词表；`request_token_estimate` 取放行块自身 token 数（判别式左值
        语义，同 `_no_early_turn_rejection` 的 atomic_tokens），`request_budget_tokens`
        取单块预算——被放行突破的那条线，与单请求路径的 hard 口径可区分。
        """
        return CompactionFailure(
            attempt=0,
            error_class="chunk_bound_released",
            message="Oversize atomic block released above the chunk bound",
            auto_limit=int(self._auto_limit),
            hard_limit=int(self._hard_limit),
            token_estimate=token_estimate,
            summary_model_id=None,
            duration_ms=0,
            request_token_estimate=chunk_tokens,
            request_budget_tokens=int(self._chunk_max_tokens),
        )


def _summary_model_id(model: Any, response: Any) -> str | None:
    """Prefer the provider-reported model id, then the configured client id."""
    metadata = getattr(response, "response_metadata", None)
    if isinstance(metadata, dict):
        for key in ("model_name", "model", "model_id"):
            value = metadata.get(key)
            if isinstance(value, str) and value.strip():
                candidate = value.strip()
                if len(candidate) <= _SUMMARY_MODEL_ID_LIMIT:
                    return candidate
    for attribute in ("model_name", "model"):
        value = getattr(model, attribute, None)
        if isinstance(value, str) and value.strip():
            candidate = value.strip()
            if len(candidate) <= _SUMMARY_MODEL_ID_LIMIT:
                return candidate
    return None


#: #649 结构隔离：正文里**逐字等于保留节标题**的行以反斜杠转义后落盘
#: （与 CommonMark 0.31.2 的反斜杠转义同源），解析时解码回原值；#699 转义
#: 碰撞：转义符本身也要转义（``\`` + 保留标题的行再加一层 ``\``），否则
#: 原文自带的 ``\##`` 行会被误解码。正文里的**非保留** Markdown 标题
#: （如 `## embedded heading`）与保留标题不同文，不转义、原样保留。
#: 节边界只由「逐字等于某一保留标题、不在围栏代码块内、且未被转义」的行
#: 定义——正文 Markdown 二级标题不再被误判为节边界。
_HEADING_ESCAPE = "\\"
_FENCE_CHARS = ("`", "~")


def _fence_delimiter(line: str) -> tuple[str, int, str] | None:
    """把一行识别为围栏代码块定界符，返回 (字符, 长度, 余下文本)。

    对齐 CommonMark 0.31.2 §4.5 的最小子集：≤3 个前导空格后至少 3 个连续的
    `` ` `` 或 ``~``；反引号围栏的 info string 不得再含反引号。纯确定性函数，
    不依赖模型：围栏内（含定界行）的一律按字面正文处理，不参与节边界判定。
    """
    indent = len(line) - len(line.lstrip(" "))
    if indent > 3:
        return None
    stripped = line[indent:]
    if not stripped or stripped[0] not in _FENCE_CHARS:
        return None
    char = stripped[0]
    length = len(stripped) - len(stripped.lstrip(char))
    if length < 3:
        return None
    rest = stripped[length:]
    if char == "`" and "`" in rest:
        return None
    return char, length, rest


def _fenced_mask(lines: list[str]) -> list[bool]:
    """逐行标记「是否处于围栏代码块内部」（含定界行本身），确定性纯函数。"""
    mask: list[bool] = []
    opened: tuple[str, int] | None = None
    for line in lines:
        delimiter = _fence_delimiter(line)
        if opened is None:
            mask.append(delimiter is not None)
            if delimiter is not None:
                opened = (delimiter[0], delimiter[1])
        else:
            mask.append(True)
            if (delimiter is not None and delimiter[0] == opened[0]
                    and delimiter[1] >= opened[1] and not delimiter[2].strip()):
                opened = None
    return mask


def _escape_section_body(body: str, headings: tuple[str, ...]) -> str:
    """把正文中与保留标题同文的行转义，避免组装后被误当成节边界（#649）。

    #699 转义碰撞：转义符本身也要转义（escape-the-escape，与 CommonMark
    0.31.2 §6.1 / RFC 8259 §7 同源）。行满足 ``^(\\\\*)(## 保留标题)$``
    （n≥0 个前导反斜杠 + 逐字等于保留标题）即在前面再加一层 ``\\``；
    解析时逐层解回，round-trip 保真。

    只处理围栏代码块**之外**的行：围栏内的 ``##`` 行由 `_fenced_mask` 保护，
    改写会破坏代码原文，故不动。
    """
    if not body:
        return body
    lines = body.split("\n")
    fenced = _fenced_mask(lines)
    reserved = set(headings)
    escaped = []
    for index, line in enumerate(lines):
        if fenced[index]:
            escaped.append(line)
            continue
        backslashes = len(line) - len(line.lstrip("\\"))
        if line[backslashes:] in reserved:
            escaped.append(_HEADING_ESCAPE + line)
        else:
            escaped.append(line)
    return "\n".join(escaped)


def _unescape_section_body(body: str, headings: tuple[str, ...]) -> str:
    """把 `_escape_section_body` 的转义解码回原值（#649；#699 转义碰撞）。

    与组装侧互逆：``^(\\\\+)(## 保留标题)$`` 去掉一层前导 ``\\``。
    """
    lines = body.split("\n")
    fenced = _fenced_mask(lines)
    reserved = set(headings)
    unescaped = []
    for index, line in enumerate(lines):
        if fenced[index]:
            unescaped.append(line)
            continue
        backslashes = len(line) - len(line.lstrip("\\"))
        if backslashes >= 1 and line[backslashes:] in reserved:
            unescaped.append(line[1:])
        else:
            unescaped.append(line)
    return "\n".join(unescaped)


def _parse_summary_sections(text: str, headings: tuple[str, ...]) -> list[str]:
    lines = text.strip().splitlines()
    fenced = _fenced_mask(lines)
    reserved = set(headings)
    heading_positions = [
        index for index, line in enumerate(lines)
        if not fenced[index]
        and not line.startswith(_HEADING_ESCAPE)
        and line in reserved
    ]
    if tuple(lines[index] for index in heading_positions) != headings:
        raise ValueError("Summary section headings do not match the contract")
    if not heading_positions or heading_positions[0] != 0:
        raise ValueError("Summary contains text outside its sections")
    contents = []
    for position, line_number in enumerate(heading_positions):
        end = (heading_positions[position + 1]
               if position + 1 < len(heading_positions) else len(lines))
        content = "\n".join(lines[line_number + 1:end]).strip()
        if not content:
            raise ValueError("Empty summary section must use (none)")
        contents.append(_unescape_section_body(content, headings))
    return contents


def _early_source_ranges(
    messages: list[AnyMessage],
    events: list[SessionEvent] | None,
    source_ranges: list[tuple[int, int] | None] | None,
    prefix_end: int,
    cut: int,
    *,
    supports_vision: bool = False,
) -> list[tuple[int, int] | None] | None:
    """early 窗口（messages[prefix_end:cut]）的「消息 → 来源 seq range」对齐。

    W-03 (#347)：裁剪后的投影消息与 derive 产物**内容不再逐条相等**
    （ToolMessage.content 原位替换），但消息数与顺序不变——调用方传来的
    ranges 与 messages 位置一一对应，直接采用、跳过相等对齐。长度不符视同
    区间不可用（走既有拒绝路径），不做静默截断。未传 ranges 时从 events
    重投影并逐条对齐，对齐失败同样视为不可用。

    `supports_vision`（#823 / MM-02）：重推导时必须用**与压缩输入同一口径**的
    视觉判定。带图会话的 `user/message` 在视觉模型下投影为图片块列表，在非视觉
    模型下投影为占位符字符串——若调用方按视觉口径构造 `messages`（图片块）而此处
    按默认 `False` 重推导（占位符），`projected == supplied` 必不成立 ⇒ 对齐失败
    ⇒ 来源区间不可用 ⇒ 手动压缩静默 no-op。默认 False 保持既有调用方行为不变。
    """
    if events is None:
        return None
    if source_ranges is not None:
        return (
            list(source_ranges[prefix_end:cut])
            if len(source_ranges) == len(messages)
            else None
        )
    from agent_harness.session.derive import derive_messages_with_source_ranges

    mapped = derive_messages_with_source_ranges(
        events, supports_vision=supports_vision,
    )
    aligned = len(mapped) == len(messages) and all(
        projected == supplied
        for (projected, _source_range), supplied in zip(mapped, messages)
    )
    return (
        [source_range for _message, source_range in mapped[prefix_end:cut]]
        if aligned
        else None
    )


def _carrier_instruction_line(
    messages: list[AnyMessage],
    source_ranges: list[tuple[int, int] | None] | None,
) -> str:
    """#710 方向 C：第 0 节的「当前生效指令」承载位（确定性纯函数，可重放）。

    选取：本压缩覆盖段（early 窗口）内**倒序第一条非摘要 HumanMessage**。
    compact() 收到的 messages 是事件投影（builder 的注入发生在压缩之后），
    因此投影里的 HumanMessage 非摘要即真实用户消息，这条规则是精确的——
    触发压缩的当前轮仍在 recent 逐字保留，不会进入本窗口。

    口径对齐 `_current_goal_body`：JSON 字符串编码（ensure_ascii=False）、
    复用 `_USER_GOAL_VALUE_MAX_CHARS`（2000）截断上限 + 超长截断标记（含
    来源 seq 回读指针）。选中消息时用同下标的 range 写指针（用户消息的
    range 是 (seq, seq)，取 [0]）；range 为 None（来源区间不可用的直连调用
    面）时不写指针行、只写 JSON。无候选用户消息（early 全是模型消息等退化
    形态）写 ``(none)``，对齐八节摘要既有空值约定，不抛错。

    已知边界（#710 §6 非目标，如实记录）：「活跃」按消息侧可判定的最大
    口径 = 非摘要 HumanMessage；事件侧的 ``injected_by`` 等注入元数据在
    消息投影上不可见，**不做语义过滤**——被注入的提示性用户消息同样可能
    成为承载位；归档计数按本次压缩段计，不跨压缩累积。
    """
    carrier_index = next(
        (
            index
            for index in range(len(messages) - 1, -1, -1)
            if isinstance(messages[index], HumanMessage)
            and not _is_compaction_summary(messages[index])
        ),
        None,
    )
    if carrier_index is None:
        return "当前生效指令：(none)"
    raw = messages[carrier_index].content
    raw_text = raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False)
    source_range = source_ranges[carrier_index] if source_ranges is not None else None
    content = raw_text
    if len(raw_text) > _USER_GOAL_VALUE_MAX_CHARS:
        pointer = (
            f"，见来源事件 seq={source_range[0]}" if source_range is not None else ""
        )
        content = (
            raw_text[:_USER_GOAL_VALUE_MAX_CHARS]
            + f"…［已截断：显示前{_USER_GOAL_VALUE_MAX_CHARS}字符，"
            + f"全文共{len(raw_text)}字符{pointer}］"
        )
    body = json.dumps(content, ensure_ascii=False)
    if source_range is not None:
        body += f"（来源事件 seq={source_range[0]}）"
    return f"当前生效指令：{body}"


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


def _trusted_summary_indices(
    early: list[AnyMessage],
    early_ranges: list[tuple[int, int] | None] | None,
    events: list[SessionEvent] | None,
) -> frozenset[int]:
    """C-6 #642：结构化继承的信任判据 = bracket 事件身份，不是文本前缀。

    derive 投影的合法摘要在 `derive_messages_with_source_ranges` 输出里带被
    替代段的完整来源区间（summary 投影到首个被 shadow 事件的原位置，range =
    bracket 的 (source_seq_start, source_seq_end)）。据此，消息被授予结构化
    继承特权当且仅当：

    ① `events` 在场——无事件流的直连调用面没有可核对的来源；
    ② 该消息的 source_range 非空且逐字命中 events 里某条 CONTEXT_COMPACTED
       事件的 bracket 区间——该区间内的原始事件已被 shadow，投影中不存在
       同区间的其他消息，因此命中即证明来源（区间外的消息不可能误中）；
    ③ `_is_compaction_summary` 认可（marker name / 旧前缀——兼容层四个
       消费点语义保留，见 `docs/agents/642-evidence.md` §3）。

    文本前缀本身（`## 原始目标与用户约束\n` 开头）不再构成信任——与
    X-Content-Type-Options: nosniff 同构：不从内容推断身份，身份只来自
    结构化载体。方案依据与兼容策略见 `docs/agents/642-fix-research.md`。
    """
    if events is None or not early_ranges:
        return frozenset()
    bracket_ranges: set[tuple[int, int]] = set()
    for event in events:
        if event.type != CONTEXT_COMPACTED:
            continue
        start = event.data.get("source_seq_start")
        end = event.data.get("source_seq_end")
        if (isinstance(start, int) and not isinstance(start, bool)
                and isinstance(end, int) and not isinstance(end, bool)):
            bracket_ranges.add((start, end))
    return frozenset(
        index for index, message in enumerate(early)
        if _is_compaction_summary(message)
        and early_ranges[index] is not None
        and (early_ranges[index][0], early_ranges[index][1]) in bracket_ranges
    )


def _programmatic_summary_sections(
    messages: list[AnyMessage],
    protected_facts: list[ProtectedFact] | None = None,
    source_ranges: list[tuple[int, int] | None] | None = None,
    trusted_summaries: frozenset[int] = frozenset(),
) -> dict[str, str]:
    """四个程序化节（#556 裁决 C：全部**有界**，规则为确定性纯函数）。

    - 目标节（#710 方向 C 三段式）：第一行仍是当前生效目标原文
      （`_current_goal_body`，protected_facts 通道，逐字节不动）；其后新增
      「当前生效指令」承载位（`_carrier_instruction_line`——本压缩段内最新
      活跃用户消息，确定性派生、不链式继承旧摘要文本）与更早用户指令的
      归档计数行（原文由 SessionEvent 持久历史回读，不变量 #6：完整保存
      ≠ 完整注入）。叙述性历史用户消息**不再**逐字进节（旧实现 `extend`
      无界是 F-COMP-1 的根因）；跨窗口刚性读回走 ProtectedFact 通道
      （§1 独立预算）与 SessionEvent 持久历史。
    - 标识/文件节：继承 + 提取逻辑不变，套确定性窗口（`_capped_entries`，
      保留最近 N 条 + 单条截断）——同票消除 `:477-481` 的同构无界。
    - 保护事实节：`serialize_protected_facts` 契约不动（sort_keys 逐字节稳定、
      独立预算 8192）。

    `source_ranges` 与 `messages` 位置一一对应（缺省 None = 不写来源指针，
    存量直接调用面行为不变）。
    """

    # #707：`value not in target` 对 list 是 O(n) 扫描，visit 逐条调用即 O(n²)
    # （30k 标识实测单次 build 8.8s）；配套 seen 集合做 O(1) 成员检查，
    # 去重语义（首次出现顺序）不变。
    def add_once(target: list[str], seen: set[str], value: str) -> None:
        if value and value not in seen:
            seen.add(value)
            target.append(value)

    def decode_summary_values(value: str) -> list[str]:
        if value == "(none)":
            return []
        parsed = json.loads(value)
        if not isinstance(parsed, list) or not all(isinstance(item, str) for item in parsed):
            raise ValueError("Previous summary programmatic section is not a string list")
        return parsed

    identifiers: list[str] = []
    identifier_seen: set[str] = set()
    file_paths: list[str] = []
    file_path_seen: set[str] = set()

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            is_error = value.get("status") == "error" or value.get("is_error") is True
            for key, child in value.items():
                normalized = key.lower().replace("-", "_")
                if isinstance(child, str):
                    if normalized in _PATH_FIELDS:
                        add_once(file_paths, file_path_seen, child)
                        add_once(identifiers, identifier_seen, child)
                    if (normalized in _EXACT_FIELDS or normalized == "id"
                            or normalized.endswith("_id")):
                        add_once(identifiers, identifier_seen, child)
                    for match in _NUMBER_PATTERN.finditer(child):
                        add_once(identifiers, identifier_seen, match.group())
                elif isinstance(child, (int, float)) and not isinstance(child, bool):
                    add_once(identifiers, identifier_seen, str(child))
                visit(child)
            error_content = value.get("content")
            if is_error and isinstance(error_content, str):
                add_once(identifiers, identifier_seen, error_content)
        elif isinstance(value, list):
            for child in value:
                visit(child)
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            add_once(identifiers, identifier_seen, str(value))
        elif isinstance(value, str):
            for match in _IDENTIFIER_PATTERN.finditer(value):
                add_once(identifiers, identifier_seen, match.group())
            for match in _NUMBER_PATTERN.finditer(value):
                add_once(identifiers, identifier_seen, match.group())
            for match in _FILE_PATH_PATTERN.finditer(value):
                add_once(file_paths, file_path_seen, match.group())
                add_once(identifiers, identifier_seen, match.group())

    for index, message in enumerate(messages):
        # C-6 #642：结构化继承只对**有来源身份**的摘要开放（`trusted_summaries`
        # 由 `_trusted_summary_indices` 从 bracket 事件身份算出）。无来源的
        # 八节前缀消息降级为普通文本开采（不拒绝）；旧六节摘要因内容前缀不
        # 满足解析契约，本就不走此分支（T12g 语义：只开采、不迁移）。
        if (
            index in trusted_summaries
            and isinstance(message.content, str)
            and message.content.startswith(f"{_SUMMARY_HEADINGS[0]}\n")
        ):
            previous = _parse_summary_sections(message.content, _SUMMARY_HEADINGS)
            # 裁决 4（#642）：pf=None 直连面不再从先前摘要继承 [1]——
            # `protected_facts=None` 一律按"无事实"投影 (none)（fail-closed）。
            # 生产路径恒传非 None（builder.py），生产行为逐字不变。
            previous_identifiers = decode_summary_values(previous[6])
            previous_paths = decode_summary_values(previous[7])
            for value in previous_identifiers:
                add_once(identifiers, identifier_seen, value)
            for value in previous_paths:
                add_once(file_paths, file_path_seen, value)
            continue
        # #556 裁决 C：HumanMessage 原文不再逐字进任何程序化节——目标走
        # `_current_goal_body`（protected_facts 通道），叙述性历史靠 Event 回读。
        # 裁决 3（#642）：消息自身的内部 marker（`context_compaction_summary`，
        # snake_case 恰好命中 `_IDENTIFIER_PATTERN`）不是用户真实标识——开采前
        # 从消息 metadata 里剥掉，只去假阳性，正文开采语义不变。
        dump = message.model_dump(mode="json")
        if dump.get("name") == COMPACTION_SUMMARY_MESSAGE_NAME:
            dump["name"] = None
        visit(dump)

    # #710 方向 C：第 0 节固定三段式——目标行（_current_goal_body 逐字节不动）
    # + 当前生效指令承载位 + 条件归档计数行（仅本段内除承载位外的更早活跃
    # 用户消息 > 0 时；按段计，不跨压缩累积，与"不链式继承"一致）。
    section0 = (
        f"{_current_goal_body(protected_facts)}\n"
        f"{_carrier_instruction_line(messages, source_ranges)}"
    )
    archived_turns = (
        sum(
            1
            for message in messages
            if isinstance(message, HumanMessage) and not _is_compaction_summary(message)
        )
        - 1
    )
    if archived_turns > 0:
        section0 += (
            f"\n（更早 {archived_turns} 条用户指令已归档："
            "原文可由 SessionEvent 持久历史回读）"
        )

    return {
        _SUMMARY_HEADINGS[0]: section0,
        # 裁决 4（#642）：pf=None 与 pf=[] 同判——[1] 只承载 protected_facts
        # 通道，不再有任何"从先前摘要继承"的宽松分支（fail-closed）。
        _SUMMARY_HEADINGS[1]: (
            serialize_protected_facts(protected_facts)
            if protected_facts
            else "(none)"
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
    trusted_summaries: frozenset[int] = frozenset(),
    source_ranges: list[tuple[int, int] | None] | None = None,
) -> str:
    programmatic = _programmatic_summary_sections(
        messages, protected_facts,
        source_ranges=source_ranges, trusted_summaries=trusted_summaries,
    )
    bodies = [programmatic.get(heading, "") for heading in _SUMMARY_HEADINGS]
    for index, body in enumerate(model_sections, start=2):
        bodies[index] = body
    return "\n\n".join(
        f"{heading}\n{_escape_section_body(body, _SUMMARY_HEADINGS)}"
        for heading, body in zip(_SUMMARY_HEADINGS, bodies)
    )


def _validate_summary(
    summary: str, messages: list[AnyMessage],
    protected_facts: list[ProtectedFact] | None = None,
    trusted_summaries: frozenset[int] = frozenset(),
    source_ranges: list[tuple[int, int] | None] | None = None,
) -> None:
    sections = _parse_summary_sections(summary, _SUMMARY_HEADINGS)
    expected = _programmatic_summary_sections(
        messages, protected_facts,
        source_ranges=source_ranges, trusted_summaries=trusted_summaries,
    )
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
    每个进行中项的 `content` 或 `activeForm` 必须逐字出现在第 5 节内；清单**零
    未完成项**（`status != "completed"`，即无 `pending` 且无 `in_progress`）⇒
    第 5 节必须是 `(none)`。

    #844 A-sem：`(none)` 触发条件由「零 `in_progress`」放宽为「零未完成项」——
    对齐 `aux:compaction` prompt「列出尚未完成的工作……无则写 (none)」
    （`prompt/builtin.py:102`）的语义；`pending` 也是尚未完成的工作。有未完成项
    但无 `in_progress` 项时既不强制 `(none)`、也无逐字要求（只放宽不收紧）。
    `in_progress` 项的逐字校验（PRD §6.1 表行 5）一字不动。无清单（items 为空）
    不启用——未用清单的会话第 5 节本就是自由文本，保持 W-04 之前的既有行为。

    判据刻意用逐字子串而非语义比对：PRD 执行约束要求确定性机制兜底，弱模型
    重述不能靠"觉得差不多"。清单表本身就在转录的 update_plan 工具调用里，
    模型有能力逐字带上。
    """
    if not plan_items:
        return
    section = _parse_summary_sections(summary, _SUMMARY_HEADINGS)[4]
    unfinished = [item for item in plan_items if item.status != "completed"]
    if not unfinished:
        if section != "(none)":
            raise _SummaryRejected(
                "plan_section_mismatch",
                "Plan section must be (none): no plan item is unfinished",
            )
        return
    in_progress = [item for item in plan_items if item.status == "in_progress"]
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
    *, summary_model_id: str | None, duration_ms: int,
    request_token_estimate: int, request_budget_tokens: int,
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
        summary_model_id=summary_model_id,
        duration_ms=duration_ms,
        request_token_estimate=request_token_estimate,
        request_budget_tokens=request_budget_tokens,
    )
