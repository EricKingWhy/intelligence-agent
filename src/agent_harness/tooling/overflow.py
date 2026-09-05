"""ToolResult 后处理：先完整保存，再返回摘要；不参与 Tool retry。"""

import json
from abc import ABC, abstractmethod
from typing import Any

from agent_harness.session import Session
from agent_harness.session.event import ARTIFACT_CREATED
from agent_harness.storage.artifact import ArtifactStore
from agent_harness.tooling.result import ToolResult


class OverflowHandler(ABC):
    @abstractmethod
    async def maybe_overflow(
        self, session: Session, tool_call_id: str, tool_name: str, result: ToolResult,
    ) -> tuple[ToolResult, list[tuple[str, dict[str, Any]]]]:
        """完整保存大输出并返回摘要 + 待延迟追加的会话事件；未溢出时原对象返回。

        返回 (result, deferred_events)，deferred_events 是 (event_type, data) 列表。
        事件不在这里 append：Runtime 在 tool/call 落盘之后才追加（R6-7）——
        handler 在 execute_batch 内运行，直接 append 会让 artifact/created
        （含 tool_call_id）先于其 tool/call 持久化，事件日志出现前向引用。
        """


class ArtifactOverflowHandler(OverflowHandler):
    def __init__(self, store: ArtifactStore, overflow_chars: int = 2000) -> None:
        if overflow_chars <= 0:
            raise ValueError("overflow_chars must be positive")
        self._store = store
        self._overflow_chars = overflow_chars
        # 构造期预算下界校验：截断 marker（含总行数与 artifact_id）不受
        # _summarize 的 head/tail 预算约束——overflow_chars 若小于 marker
        # 本身，head/tail 被压成 0 也压不住它，摘要必然超出预算、悄悄污染
        # Context。用"空内容 + 8 字符代表 artifact_id"算 marker 长度下界
        # （_summarize 对空内容返回的就是 marker 加换行），再留 8 字符余量
        # 覆盖真实 id（16 字符 hash）与行数位数的浮动；配置错误在构造期
        # 快速失败，而不是等到运行时产出超预算摘要。
        marker_floor = len(self._summarize("", "0" * 8)) + 8
        if overflow_chars < marker_floor:
            raise ValueError(
                f"overflow_chars={overflow_chars} 小于截断摘要的最小长度"
                f"（{marker_floor}），摘要必然超出预算；请调大 overflow_chars。"
            )

    async def maybe_overflow(
        self, session: Session, tool_call_id: str, tool_name: str, result: ToolResult,
    ) -> tuple[ToolResult, list[tuple[str, dict[str, Any]]]]:
        data = result.data or {}
        outputs = {**{key: data.get(key) for key in ("output", "content", "stdout", "stderr")},
                   "message": result.message}
        oversized = {key: value for key, value in outputs.items()
                     if isinstance(value, str) and len(value) > self._overflow_chars}
        if not oversized:
            return result, []
        # 单字段保持原始文本；多个大字段共用一个可完整还原的 JSON Artifact。
        content = (next(iter(oversized.values())) if len(oversized) == 1 else
                   json.dumps(oversized, ensure_ascii=False, indent=2))
        mime_type = "text/plain" if len(oversized) == 1 else "application/json"
        artifact = await self._store.save(
            session.session_id, content, mime_type=mime_type,
            source_tool=tool_name, tool_call_id=tool_call_id,
        )
        summaries = {key: self._summarize(value, artifact.artifact_id)
                     for key, value in oversized.items()}
        deferred = [(ARTIFACT_CREATED, {
            "artifact_id": artifact.artifact_id, "session_id": session.session_id,
            "source_tool": tool_name, "tool_call_id": tool_call_id,
            "size": artifact.size, "mime_type": artifact.mime_type,
        })]
        message = summaries.pop("message", result.message)
        return result.model_copy(update={
            "artifact_ref": artifact.artifact_id, "message": message,
            "data": {**data, **summaries} if summaries else result.data,
        }), deferred

    def _summarize(self, content: str, artifact_id: str) -> str:
        lines = content.splitlines()
        marker = (f"... [truncated, {len(lines)} lines total, "
                  f"use inspect_artifact({artifact_id}) to view]")
        # 行数限制之外再限制字符数，避免单行日志本身撑爆 Context。
        budget = max(0, (self._overflow_chars - len(marker) - 2) // 2)
        head = "\n".join(lines[:10])[:budget]
        tail = "\n".join(lines[-10:])[-budget:] if budget else ""
        return f"{head}\n{marker}\n{tail}"
