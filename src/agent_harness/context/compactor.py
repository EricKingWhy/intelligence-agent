"""压缩运行期投影，保留完整 tool interaction 与当前用户 turn。"""

import asyncio
import json
import logging
import re
from dataclasses import dataclass
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
from agent_harness.prompt import DEFAULT_REGISTRY
from agent_harness.session.event import SessionEvent

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
_PATH_FIELDS = {"path", "file", "filename", "filepath", "file_path", "source_file"}
_EXACT_FIELDS = {"command", "cmd", "shell", "error", "error_message"}


class ContextWindowExceededError(RuntimeError):
    """无法构造安全的模型上下文，调用方必须停止当前 run。"""


@dataclass
class CompactionResult:
    messages: list[AnyMessage]
    compacted_turn_count: int
    token_estimate: int
    fallback_used: bool
    # T4 (#134)：bracket 元数据——被压缩段的 seq 区间 + 唯一 bracket_id。
    source_seq_start: int | None = None
    source_seq_end: int | None = None
    bracket_id: str | None = None
    summary: str | None = None


#: T4 (#134)：摘要 prompt 的正文已迁到 `agent_harness.prompt.builtin`
#: （section `aux:compaction`）——改文案开那一个文件。


class ContextCompactor:
    def __init__(self, model_provider: Any, *, max_context_tokens: int = 200_000,
                 auto_compact_threshold: float = 0.80,
                 hard_guard_threshold: float = 0.90,
                 keep_recent_tokens: int = 20_000,
                 summary_timeout_seconds: float = 30.0) -> None:
        if max_context_tokens <= 0 or not 0 < auto_compact_threshold <= hard_guard_threshold <= 1:
            raise ValueError("invalid context budget")
        if summary_timeout_seconds <= 0:
            raise ValueError("summary_timeout_seconds must be positive")
        self._model = model_provider
        self.auto_compact_threshold = auto_compact_threshold
        self.hard_guard_threshold = hard_guard_threshold
        self.keep_recent_tokens = keep_recent_tokens
        self._hard_limit = max_context_tokens * hard_guard_threshold
        self._auto_limit = max_context_tokens * auto_compact_threshold
        self._summary_timeout = summary_timeout_seconds
        self._max_context_tokens = max_context_tokens
        self.reserve = max(int(max_context_tokens * 0.15), 16384)

    async def compact(
        self,
        messages: list[AnyMessage],
        token_estimate: int,
        *,
        events: list[SessionEvent] | None = None,
    ) -> CompactionResult:
        _validate_tool_blocks(messages)
        prefix_end = 0
        while prefix_end < len(messages) and isinstance(messages[prefix_end], SystemMessage):
            prefix_end += 1
        prefix = messages[:prefix_end]
        cut = max((i for i, message in enumerate(messages)
                   if isinstance(message, HumanMessage)), default=prefix_end)
        early, recent = messages[prefix_end:cut], messages[cut:]
        if not early:
            count = estimate_message_tokens(messages)
            if count > self._hard_limit:
                raise ContextWindowExceededError("No complete early turn can be compacted")
            return CompactionResult(list(messages), 0, count, False)
        prompt = SystemMessage(
            content=DEFAULT_REGISTRY.assemble("aux:compaction").system_text
        )
        transcript = HumanMessage(content=json.dumps(
            [message.model_dump(mode="json") for message in early], ensure_ascii=False,
        ))
        request = [prompt, transcript]
        summary_text: str
        try:
            if estimate_message_tokens(request) > self._hard_limit:
                raise ContextWindowExceededError("Summary request exceeds hard guard")
            async with asyncio.timeout(self._summary_timeout):
                response = await self._model.ainvoke(request)
            if not isinstance(response, AIMessage) or response.tool_calls:
                raise ValueError("Summary must be text without tool calls")
            if not isinstance(response.content, str):
                raise TypeError("Summary must be plain text")
            model_sections = _parse_summary_sections(
                response.content, _MODEL_SUMMARY_HEADINGS,
            )
            summary_text = _assemble_summary(early, model_sections)
            _validate_summary(summary_text, early)
            early_tokens = estimate_message_tokens(early)
            summary_tokens = estimate_message_tokens([SystemMessage(content=summary_text)])
            if summary_tokens >= early_tokens:
                raise ContextWindowExceededError(
                    f"Summary ({summary_tokens} tokens) is not smaller than "
                    f"compressed segment ({early_tokens} tokens)"
                )
            compacted = [*prefix, SystemMessage(content=summary_text), *recent]
            if estimate_message_tokens(compacted) >= self._auto_limit:
                raise ContextWindowExceededError("LLM summary does not reach compaction target")
        except Exception as exc:  # noqa: BLE001
            # 摘要失败时不能只在当前轮使用 fallback、却把空摘要写入事件流。
            # 保留原投影；超出硬护栏时安全停止，CancelledError 仍向调用方传播。
            logger.warning(
                "Context compaction rejected; keeping original projection (%s)",
                type(exc).__name__,
            )
            if token_estimate > self._hard_limit:
                raise ContextWindowExceededError(
                    "Summary validation failed and original context exceeds hard guard"
                ) from None
            return CompactionResult(list(messages), 0, token_estimate, False)
        count = estimate_message_tokens(compacted)
        if count > self._hard_limit:
            raise ContextWindowExceededError(
                f"Compaction cannot fit context: {token_estimate} -> {count} tokens; "
                f"hard guard {self._hard_limit:g}"
            )
        # T4 (#134)：从 events 计算 source_seq 区间。
        # projecting events 一一对应 messages（derive_messages 的投影集合），
        # 但 prefix（开头的 SystemMessages）不是由投影事件产生的——
        # 所以产生 early 消息的事件是 projecting[prefix_end:cut]。
        source_seq_start: int | None = None
        source_seq_end: int | None = None
        if events is not None:
            from agent_harness.session.event import (
                MODEL_COMPLETED,
                TOOL_RESULT,
                USER_MESSAGE,
            )
            projecting = [e for e in events if e.type in {
                USER_MESSAGE, MODEL_COMPLETED, TOOL_RESULT,
            }]
            # 如果 dangling 合成注入导致计数失配，放弃 source_seq 计算
            if len(projecting) == len(messages):
                early_events = projecting[prefix_end:cut]
                if early_events:
                    source_seq_start = early_events[0].seq
                    source_seq_end = early_events[-1].seq
        if events is not None and (source_seq_start is None or source_seq_end is None):
            logger.warning(
                "Context compaction rejected; source event range is unavailable",
            )
            if token_estimate > self._hard_limit:
                raise ContextWindowExceededError(
                    "Cannot persist compaction without a source event range"
                )
            return CompactionResult(list(messages), 0, token_estimate, False)
        return CompactionResult(
            compacted,
            sum(isinstance(message, HumanMessage) for message in early),
            count, False,
            source_seq_start=source_seq_start,
            source_seq_end=source_seq_end,
            bracket_id=str(uuid4()),
            summary=summary_text,
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


def _programmatic_summary_sections(messages: list[AnyMessage]) -> dict[str, str]:
    user_messages = [message.content for message in messages
                     if isinstance(message, HumanMessage)]
    identifiers: list[str] = []
    file_paths: list[str] = []

    def add_once(target: list[str], value: str) -> None:
        if value and value not in target:
            target.append(value)

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
                visit(child)
            error_content = value.get("content")
            if is_error and isinstance(error_content, str):
                add_once(identifiers, error_content)
        elif isinstance(value, list):
            for child in value:
                visit(child)
        elif isinstance(value, str):
            for match in _IDENTIFIER_PATTERN.finditer(value):
                add_once(identifiers, match.group())
            for match in _FILE_PATH_PATTERN.finditer(value):
                add_once(file_paths, match.group())
                add_once(identifiers, match.group())

    for message in messages:
        visit(message.model_dump(mode="json"))

    return {
        _SUMMARY_HEADINGS[0]: json.dumps(user_messages, ensure_ascii=False)
        if user_messages else "(none)",
        # #346 adds the durable protected-fact registry; until then its exact value is empty.
        _SUMMARY_HEADINGS[1]: "(none)",
        _SUMMARY_HEADINGS[6]: json.dumps(identifiers, ensure_ascii=False)
        if identifiers else "(none)",
        _SUMMARY_HEADINGS[7]: json.dumps(file_paths, ensure_ascii=False)
        if file_paths else "(none)",
    }


def _assemble_summary(
    messages: list[AnyMessage], model_sections: list[str],
) -> str:
    programmatic = _programmatic_summary_sections(messages)
    bodies = [programmatic.get(heading, "") for heading in _SUMMARY_HEADINGS]
    for index, body in enumerate(model_sections, start=2):
        bodies[index] = body
    return "\n\n".join(
        f"{heading}\n{body}" for heading, body in zip(_SUMMARY_HEADINGS, bodies)
    )


def _validate_summary(summary: str, messages: list[AnyMessage]) -> None:
    sections = _parse_summary_sections(summary, _SUMMARY_HEADINGS)
    expected = _programmatic_summary_sections(messages)
    for index in _PROGRAMMATIC_SUMMARY_HEADINGS:
        if sections[index] != expected[_SUMMARY_HEADINGS[index]]:
            raise ValueError("Programmatic summary section failed exact comparison")
    if not summary.strip():
        raise ValueError("Summary must not be empty")


#: mechanical 摘要单字段截断预算（与 human/tool content 的 200/100 同级）。
_MECHANICAL_CAP = 200


def _mechanical_summary(messages: list[AnyMessage]) -> str:
    rows = []
    for message in messages:
        if isinstance(message, HumanMessage):
            rows.append({"type": "human", "content": message.text[:200]})
        elif isinstance(message, AIMessage):
            # tool_call.args 必须截断：args 是模型自由生成的（write 大文件等），
            # 原样嵌入会让摘要本身超硬护栏——历史从不裁剪 + 投影每轮重建，
            # 该 session 从此每次 run 都 context_window_exceeded，永久 brick。
            rows.append({"type": "ai", "tool_calls": [
                {"id": call.get("id"), "name": call.get("name"),
                 "args": _cap_text(json.dumps(call.get("args", {}), ensure_ascii=False))}
                for call in message.tool_calls
            ]})
        elif isinstance(message, ToolMessage):
            rows.append({"type": "tool", "tool_call_id": message.tool_call_id,
                         "content": message.text[:100]})
        elif isinstance(message, SystemMessage):
            rows.append({"type": "system", "content": message.text[:_MECHANICAL_CAP]})
        else:
            # 不丢弃系统约束或未知类型的消息；dump 整体截断成字符串，保证摘要有界。
            rows.append(_cap_text(
                json.dumps(message.model_dump(mode="json"), ensure_ascii=False), 300,
            ))
    return json.dumps({"mechanical_extract": rows}, ensure_ascii=False)


def _cap_text(text: str, cap: int = _MECHANICAL_CAP) -> str:
    if len(text) <= cap:
        return text
    return text[:cap] + "…[truncated]"


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
