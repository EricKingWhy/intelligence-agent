"""ToolResult 后处理：先完整保存，再返回摘要；不参与 Tool retry。"""

import json
from abc import ABC, abstractmethod

from agent_harness.session import Session
from agent_harness.session.event import ARTIFACT_CREATED
from agent_harness.storage.artifact import ArtifactStore
from agent_harness.tooling.result import ToolResult


class OverflowHandler(ABC):
    @abstractmethod
    async def maybe_overflow(
        self, session: Session, tool_call_id: str, tool_name: str, result: ToolResult,
    ) -> ToolResult:
        """完整保存大输出并返回摘要；未溢出时返回原对象。"""


class ArtifactOverflowHandler(OverflowHandler):
    def __init__(self, store: ArtifactStore, overflow_chars: int = 2000) -> None:
        if overflow_chars <= 0:
            raise ValueError("overflow_chars must be positive")
        self._store = store
        self._overflow_chars = overflow_chars

    async def maybe_overflow(
        self, session: Session, tool_call_id: str, tool_name: str, result: ToolResult,
    ) -> ToolResult:
        data = result.data or {}
        outputs = {**{key: data.get(key) for key in ("output", "content", "stdout", "stderr")},
                   "message": result.message}
        oversized = {key: value for key, value in outputs.items()
                     if isinstance(value, str) and len(value) > self._overflow_chars}
        if not oversized:
            return result
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
        session.append(ARTIFACT_CREATED, {
            "artifact_id": artifact.artifact_id, "session_id": session.session_id,
            "source_tool": tool_name, "tool_call_id": tool_call_id,
            "size": artifact.size, "mime_type": artifact.mime_type,
        })
        message = summaries.pop("message", result.message)
        return result.model_copy(update={
            "artifact_ref": artifact.artifact_id, "message": message,
            "data": {**data, **summaries} if summaries else result.data,
        })

    def _summarize(self, content: str, artifact_id: str) -> str:
        lines = content.splitlines()
        marker = (f"... [truncated, {len(lines)} lines total, "
                  f"use inspect_artifact({artifact_id}) to view]")
        # 行数限制之外再限制字符数，避免单行日志本身撑爆 Context。
        budget = max(0, (self._overflow_chars - len(marker) - 2) // 2)
        head = "\n".join(lines[:10])[:budget]
        tail = "\n".join(lines[-10:])[-budget:] if budget else ""
        return f"{head}\n{marker}\n{tail}"
