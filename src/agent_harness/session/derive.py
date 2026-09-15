"""derive_messages：从 SessionEvent 序列投影出模型可见 messages 列表。

纯函数，无副作用。负责：
    1. user/message → HumanMessage
    2. model/completed → AIMessage（含 tool_calls）
    3. tool/result → ToolMessage（按 tool_call_id 配对到 AIMessage）
    4. dangling tool_call 检测 → 注入合成 ToolMessage
    5. ADR-0030（#196）：`message/superseded` 区间剔除（投影级"编辑替换问句"）

本模块同时是**事件流派生**的集散地：`collect_dangling` / `detect_dangling` /
`undelivered_inputs` 都是同一种东西——只读事件、产出领域事实的纯函数，供
runtime / recovery / service 复用，避免各调用点各写一遍扫描逻辑。

配对算法：以 AIMessage 为单位。一条 model/completed 带多个 tool_calls 时，
投影成一条 AIMessage(tool_calls=[...])，后续 tool/result 按 tool_call_id
匹配成各自 ToolMessage（符合 OpenAI / Anthropic / LangChain 标准消息格式）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from langchain_core.messages import (
    AIMessage,
    AnyMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)

from agent_harness.session.event import (
    COMPACTION_END,
    COMPACTION_START,
    CONTEXT_COMPACTED,
    MESSAGE_QUEUED,
    MESSAGE_SUPERSEDED,
    MODEL_COMPLETED,
    QUEUE_CANCELLED,
    QUEUE_CONSUMED,
    STEER_APPLIED,
    STEER_REQUESTED,
    TOOL_CALL,
    TOOL_RESULT,
    USER_MESSAGE,
    SessionEvent,
)

logger = logging.getLogger("agent_harness.session.derive")

#: 合成 dangling ToolMessage 的固定内容（模型可见，引导自主决策）
DANGLING_TOOL_CONTENT = "工具执行被中断，结果未知"

#: bracket 元数据事件——不投影成消息，仅标记 shadowed 区间。
_BRACKET_META_TYPES = frozenset({
    COMPACTION_START, CONTEXT_COMPACTED, COMPACTION_END,
})


def _normalize_tool_calls_for_projection(
    raw: object,
) -> list[dict[str, object]]:
    """把 MODEL_COMPLETED.tool_calls 规整成投影安全的 list[dict]。

    单事件层容错：tool_calls 可能被旧日志、手工写入、序列化路径污染成非 list
    （dict / str / None）。derive_messages 是恢复链的必经节点，一行坏数据不能
    brick 整个 session 的恢复（违反存储模块"一行坏数据只损失该行"的契约）。
    非法形状降级为无 tool_calls（只丢该事件的工具调用，不让整个投影抛错）；
    单条非 dict 元素跳过。

    缺 id 在投影层保留空串（与事件原始形状一致，仅做消息重建），不在投影层
    合成占位 id；Tool Runtime 边界的 ToolCall.normalize 才升级为 gen_<hex> 占位
    （那是 Ledger 主键与 ToolMessage 配对的关键）。两层策略刻意不同：投影只读、
    Runtime 才赋身份。详见 contract.py:ToolCall.normalize。
    """
    if not isinstance(raw, list):
        if raw:
            logger.warning(
                "MODEL_COMPLETED.tool_calls 形状非法（%s），降级为无 tool_calls",
                type(raw).__name__,
            )
        return []
    normalized: list[dict[str, object]] = []
    for tc in raw:
        if not isinstance(tc, dict):
            logger.warning(
                "tool_call 元素非 dict（%s），跳过该项", type(tc).__name__
            )
            continue
        normalized.append(
            {
                "id": tc.get("id", ""),
                "name": tc.get("name", ""),
                "args": tc.get("args", {}),
            }
        )
    return normalized


def derive_messages(events: list[SessionEvent]) -> list[AnyMessage]:
    """从事件序列投影出 messages 列表。

    纯函数：不修改输入 events，不产生副作用。
    dangling tool_call（有 tool/call 无匹配 tool/result）会注入合成 ToolMessage。

    T4 (#134)：识别 4-event compaction bracket。bracket 标记的 source_seq 区间
    内的原始投影事件被 shadowed（跳过），CONTEXT_COMPACTED 的 summary 投影成
    SystemMessage 替代被压缩段。

    ADR-0030 (#196)：识别 `message/superseded`——被取代的 user/message 及其整轮
    （答 + tool_call/result）走同一条 shadowed 跳过路径，所以"编辑了问句"在模型可见
    上下文里表现为"旧问句那一轮整段消失、只剩新问句"。dangling 合成发生在 shadow
    之后，被取代轮里的 tool_call 不会被补一条合成 ToolMessage。
    """
    # 第一遍：收集所有 bracket 的 shadowed seq 区间 + 对应 summary。
    # 每个 bracket 由 COMPACTION_START(source_seq_start..source_seq_end) 标记，
    # CONTEXT_COMPACTED 携带 summary，COMPACTION_END 关闭 bracket。
    shadowed_ranges: list[tuple[int, int]] = []
    bracket_summaries: list[str] = []
    for event in events:
        if event.type == COMPACTION_START:
            start = event.data.get("source_seq_start", 0)
            end = event.data.get("source_seq_end", 0)
            shadowed_ranges.append((start, end))
        elif event.type == CONTEXT_COMPACTED:
            bracket_summaries.append(event.data.get("summary", ""))

    # ADR-0030 §4.2（#196）：supersede 区间——被取代的那个问句**连同它的整轮**
    # （答、tool_call、tool_result、delta）都不进模型可见投影。
    #
    # 区间 = `[s, n)`，s = 被取代的 user/message seq，n = s 之后第一条**未被取代**的
    # user/message seq；没有这样一条就一直 shadow 到末尾（该轮之后不再有用户输入）。
    # 判据只看 seq 与"该 seq 是否也被取代"，与事件到达顺序无关 ⇒ 纯函数性质不破。
    #
    # ⚠ 刻意与 compaction 的 `shadowed_ranges` **分成两个列表**：下面吐 summary 的循环用
    # `enumerate(shadowed_ranges)` 的下标去索引 `bracket_summaries`（一个 bracket 一条
    # summary）；把 supersede 区间并进去会让下标错位，summary 落到错误的位置甚至不吐。
    # 跳过逻辑仍然只有一处（`is_shadowed`），没有第二套跳过实现。
    superseded_ranges: list[tuple[int, int]] = []
    superseded_seqs: set[int] = set()
    for event in events:
        if event.type != MESSAGE_SUPERSEDED:
            continue
        raw = event.data.get("superseded_seq")
        # 一行坏数据只损失该行（存储模块契约）：非 int 就跳过并警告，不 brick 恢复。
        if isinstance(raw, int) and not isinstance(raw, bool):
            superseded_seqs.add(raw)
        elif raw is not None:
            logger.warning(
                "MESSAGE_SUPERSEDED.superseded_seq 形状非法（%s），忽略该条",
                type(raw).__name__,
            )
    if superseded_seqs:
        user_seqs = [e.seq for e in events if e.type == USER_MESSAGE]
        last_seq = max((e.seq for e in events), default=0)
        for seq in sorted(superseded_seqs):
            following = next(
                (u for u in user_seqs if u > seq and u not in superseded_seqs),
                None,
            )
            # n = 下一条未被取代的 user 消息 ⇒ 区间末尾（含）是 n - 1；没有则到末尾。
            end = (following - 1) if following is not None else last_seq
            if end >= seq:
                superseded_ranges.append((seq, end))

    def is_shadowed(seq: int) -> bool:
        if any(start <= seq <= end for start, end in shadowed_ranges):
            return True
        return any(start <= seq <= end for start, end in superseded_ranges)

    # 第二遍：从事件按顺序投影 messages（不含 dangling 合成）。
    #
    # summary SystemMessage 必须在被压缩段的**原位置**注入——即遇到第一个
    # shadowed 事件时插入 summary，而不是在 CONTEXT_COMPACTED 事件的位置
    # 插入。原因：bracket 事件可能排在当前用户消息之后（compaction 在
    # context build 阶段触发，此时 user/message 已经 append），如果按
    # CONTEXT_COMPACTED 的位置投影 summary，summary 会落到当前用户消息
    # 之后，破坏"摘要在前、当前请求在后"的语义。
    messages: list[AnyMessage] = []
    summary_emitted = [False] * len(bracket_summaries)

    for event in events:
        # 遇到某个 bracket 的第一个 shadowed 事件时，先吐 summary
        for bi, (s, _e) in enumerate(shadowed_ranges):
            if not summary_emitted[bi] and event.seq == s:
                if bi < len(bracket_summaries):
                    summary = bracket_summaries[bi]
                    if summary:
                        messages.append(SystemMessage(content=summary))
                summary_emitted[bi] = True
                break

        # CONTEXT_COMPACTED / COMPACTION_START / COMPACTION_END 不投影成消息
        if event.type in _BRACKET_META_TYPES:
            continue

        # 跳过被 bracket shadowed 的原始事件
        if is_shadowed(event.seq):
            continue

        if event.type == USER_MESSAGE:
            content = event.data.get("content", "")
            messages.append(HumanMessage(content=content))

        elif event.type == MODEL_COMPLETED:
            content = event.data.get("content", "")
            tool_calls = _normalize_tool_calls_for_projection(
                event.data.get("tool_calls", [])
            )
            if tool_calls:
                messages.append(AIMessage(content=content, tool_calls=tool_calls))
            else:
                messages.append(AIMessage(content=content))

        elif event.type == TOOL_RESULT:
            tool_call_id = event.data.get("tool_call_id", "")
            content = event.data.get("content", "")
            messages.append(ToolMessage(content=content, tool_call_id=tool_call_id))

    # 第二遍：为 dangling tool_call 注入合成 ToolMessage。
    # 只在 AIMessage 的某个 tool_call 在紧跟的 ToolMessage 块中没有对应结果时注入。
    # 合成消息插入在该 AIMessage 后紧跟的 ToolMessage 块的末尾。
    result: list[AnyMessage] = []
    i = 0
    while i < len(messages):
        msg = messages[i]
        result.append(msg)

        if isinstance(msg, AIMessage) and msg.tool_calls:
            # 收集这条 AIMessage 之后紧跟的连续 ToolMessage 块
            block_ids: set[str] = set()
            block_end = i + 1
            while block_end < len(messages) and isinstance(
                messages[block_end], ToolMessage
            ):
                block_ids.add(messages[block_end].tool_call_id)
                result.append(messages[block_end])
                block_end += 1

            # 为块中缺失的 tool_call 追加合成 ToolMessage
            for tc in msg.tool_calls:
                tc_id = tc.get("id", "")
                if tc_id and tc_id not in block_ids:
                    logger.warning(
                        "dangling tool_call 检测：tool_call_id=%s 无匹配 tool/result，"
                        "注入合成 ToolMessage",
                        tc_id,
                    )
                    result.append(
                        ToolMessage(content=DANGLING_TOOL_CONTENT, tool_call_id=tc_id)
                    )
            i = block_end
        else:
            i += 1

    return result


def collect_dangling(
    events: list[SessionEvent],
) -> tuple[set[str], set[str]]:
    """单遍扫描事件，返回 (dangling tool_call_ids, 已有 tool/call 事件的 ids)。

    dangling 的判定必须同时覆盖两个事实源（此前 RecoveryCoordinator 与
    detect_dangling 各维护一份近重复实现，2026-09-09 统一到这里）：
    - tool/call 事件（resume 一致性用）；
    - model/completed 的 tool_calls 字段（derive_messages 的 AIMessage 投影用）。

    MODEL_COMPLETED.tool_calls 经 ``_normalize_tool_calls_for_projection``
    容错（非 list / 非 dict 元素降级跳过——恢复链必经节点，一行坏数据不能
    brick 整个 session 的恢复）。
    """
    requested: set[str] = set()
    call_event_ids: set[str] = set()
    resolved: set[str] = set()
    for event in events:
        if event.type == MODEL_COMPLETED:
            for tc in _normalize_tool_calls_for_projection(
                event.data.get("tool_calls", [])
            ):
                tc_id = tc.get("id", "")
                if tc_id:
                    requested.add(tc_id)
        elif event.type == TOOL_CALL:
            tc_id = event.data.get("tool_call_id", "")
            if tc_id:
                requested.add(tc_id)
                call_event_ids.add(tc_id)
        elif event.type == TOOL_RESULT:
            tc_id = event.data.get("tool_call_id", "")
            if tc_id:
                resolved.add(tc_id)
    return requested - resolved, call_event_ids


def detect_dangling(events: list[SessionEvent]) -> list[str]:
    """返回事件序列中 dangling 的 tool_call_id 列表（有请求无结果）。

    供 Session.append 在 resume 时决定是否需要合成 tool/result 事件。

    真相源与 derive_messages 对齐：请求侧同时看 TOOL_CALL 事件和
    MODEL_COMPLETED.tool_calls。崩溃可能发生在 MODEL_COMPLETED 已持久化但
    TOOL_CALL 还没写的窗口——只看 TOOL_CALL 会让这种 dangling 静默漏掉，
    Session.resume 不合成 tool/result，历史永久悬空（derive_messages 每次
    投影都重复触发 dangling 警告）。
    """
    dangling, _ = collect_dangling(events)
    return sorted(dangling)


#: 未投递输入的两种载体（ADR-0030 §2 术语表）。
KIND_QUEUE = "queue"
KIND_STEER = "steer"


@dataclass(frozen=True)
class UndeliveredInput:
    """一条尚未变成 run 的用户输入（queue 项或未生效的 steer 请求）。

    `seq` 是**到达顺序**（ADR-0030 D7）：queue 与 steer 混排时按它排序，而不是
    "先队列后 steer"——两种载体的差别只在投递边界，不在优先级。
    """

    kind: str  # KIND_QUEUE / KIND_STEER
    input_id: str  # queue_id（queue）或 steer_id（steer）
    content: str
    seq: int
    created_at: str
    #: steer 的注入目标 run（重建内存注册表时还原）。queue 恒 None。
    run_id: str | None = None


def undelivered_inputs(events: list[SessionEvent]) -> list[UndeliveredInput]:
    """未投递输入，按到达顺序（seq）升序——D4 驱动点与 `GET /queue` 的唯一判据。

    纯函数（与 derive_messages 同族：只读事件、不碰内存态）。判据：

    - `message/queued` 中未被 `queue/cancelled` 取消、未被 `queue/consumed` 消费的；
    - `steer/requested` 中未被 `steer/applied` 收口的。

    这是"队列跨崩溃存活"（D5）的落点：未投递 = **事件流上的事实**，与进程内那
    份缓存无关，所以重启后按它重建即可（ADR-0030 §4.8）。一行坏数据（缺 id /
    id 非法形状）只损失该行，不让整个派生抛错——与 collect_dangling 的容错口径一致。
    """
    cancelled: set[str] = set()
    consumed: set[str] = set()
    applied: set[str] = set()
    for event in events:
        if event.type == QUEUE_CANCELLED or event.type == QUEUE_CONSUMED:
            target = cancelled if event.type == QUEUE_CANCELLED else consumed
        elif event.type == STEER_APPLIED:
            target = applied
        else:
            continue
        value = event.data.get("steer_id" if event.type == STEER_APPLIED else "queue_id")
        if isinstance(value, str) and value:
            target.add(value)

    items: list[UndeliveredInput] = []
    for event in events:
        if event.type == MESSAGE_QUEUED:
            input_id = event.data.get("queue_id")
            if not isinstance(input_id, str) or not input_id:
                continue
            if input_id in cancelled or input_id in consumed:
                continue
            items.append(
                UndeliveredInput(
                    kind=KIND_QUEUE,
                    input_id=input_id,
                    content=str(event.data.get("content", "")),
                    seq=event.seq,
                    created_at=event.time,
                )
            )
        elif event.type == STEER_REQUESTED:
            input_id = event.data.get("steer_id")
            if not isinstance(input_id, str) or not input_id:
                continue
            if input_id in applied:
                continue
            run_id = event.data.get("run_id")
            items.append(
                UndeliveredInput(
                    kind=KIND_STEER,
                    input_id=input_id,
                    content=str(event.data.get("content", "")),
                    seq=event.seq,
                    created_at=event.time,
                    run_id=run_id if isinstance(run_id, str) else None,
                )
            )
    items.sort(key=lambda item: item.seq)
    return items
