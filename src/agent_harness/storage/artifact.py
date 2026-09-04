"""ArtifactStore：大输出的持久化边界（content-hash 寻址）。

为什么独立成 artifact.py：
- ToolResult 的主输出字段溢出时，原始内容存到这里，模型只拿截断摘要 + artifact_ref。
- 与 SessionEvent（对话历史）和 Operation Ledger（操作状态）不同种类的事实：
  SessionEvent 记"对话发生了什么"，Ledger 记"每次调用现在什么状态"，
  ArtifactStore 记"大输出本身存在哪、怎么找回"。

为什么 content-hash 寻址：
- 同一内容自动去重（两个 Tool 产出相同的 stdout 只存一份）；
- 寻址不需要额外 ID 生成器——hash 就是 ID；
- 跨 Session 理论上可共享（相同内容同 hash），但 Phase 5 按 session 隔离 key。

物理位置：Runtime 域存储，不经过 Sandbox（spec 06 §3 + ADR-0006）。
默认 Provider：S3ArtifactStore（七牛云 Kodo S3 兼容）。
测试 Provider：FakeArtifactStore（内存 dict）。
"""

from __future__ import annotations

import hashlib
from abc import ABC, abstractmethod

from pydantic import BaseModel


class Artifact(BaseModel):
    """一个 Artifact 的元数据 + 可选内容。"""

    artifact_id: str
    session_id: str
    size: int
    mime_type: str
    source_tool: str
    tool_call_id: str
    created_at: str
    content: str | None = None  # load() 时填充；inspect() 不填充


class ArtifactSlice(BaseModel):
    """inspect() 的局部读取结果。"""

    artifact_id: str
    lines: list[dict[str, int | str]]
    total_lines: int
    returned_lines: int
    truncated: bool
    query: dict[str, int | str | None]


class ArtifactStore(ABC):
    """Artifact 的持久化边界（async ABC）。"""

    @abstractmethod
    async def save(
        self,
        session_id: str,
        content: str,
        *,
        mime_type: str,
        source_tool: str,
        tool_call_id: str,
    ) -> Artifact:
        """存内容，返回带 artifact_id (content-hash) 的 Artifact 元数据。"""

    @abstractmethod
    async def load(self, artifact_id: str) -> Artifact:
        """完整加载一个 Artifact（内容 + 元数据）。"""

    @abstractmethod
    async def inspect(
        self,
        artifact_id: str,
        *,
        start_line: int | None = None,
        end_line: int | None = None,
        keyword: str | None = None,
        max_lines: int = 200,
    ) -> ArtifactSlice:
        """按行范围或关键词读取局部内容。"""


def compute_artifact_id(content: str) -> str:
    """content-hash 寻址：SHA-256 前 16 字符作为 artifact_id。"""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]


def _slice_lines(
    all_lines: list[str],
    *,
    start_line: int | None,
    end_line: int | None,
    keyword: str | None,
    max_lines: int,
) -> tuple[list[dict[str, int | str]], bool]:
    """通用切片逻辑：供 FakeArtifactStore 和 S3ArtifactStore 共享。

    返回 (行列表, truncated)。
    """
    indexed = [{"line_number": i + 1, "text": line} for i, line in enumerate(all_lines)]

    if keyword:
        filtered = [entry for entry in indexed if keyword in entry["text"]]
    else:
        s = (start_line or 1) - 1  # 转 0-based
        e = end_line if end_line is not None else len(indexed)
        filtered = indexed[s:e]

    truncated = len(filtered) > max_lines
    return filtered[:max_lines], truncated


class FakeArtifactStore(ArtifactStore):
    """内存 dict 实现——给单元测试用。不碰网络。"""

    def __init__(self) -> None:
        self._artifacts: dict[str, tuple[Artifact, str]] = {}  # id → (meta, content)

    async def save(
        self,
        session_id: str,
        content: str,
        *,
        mime_type: str,
        source_tool: str,
        tool_call_id: str,
    ) -> Artifact:
        from agent_harness.storage.sqlite import _utc_now_iso

        artifact_id = compute_artifact_id(content)
        artifact = Artifact(
            artifact_id=artifact_id,
            session_id=session_id,
            size=len(content.encode("utf-8")),
            mime_type=mime_type,
            source_tool=source_tool,
            tool_call_id=tool_call_id,
            created_at=_utc_now_iso(),
        )
        self._artifacts[artifact_id] = (artifact, content)
        return artifact

    async def load(self, artifact_id: str) -> Artifact:
        if artifact_id not in self._artifacts:
            raise KeyError(f"Artifact '{artifact_id}' does not exist")
        meta, content = self._artifacts[artifact_id]
        return meta.model_copy(update={"content": content})

    async def inspect(
        self,
        artifact_id: str,
        *,
        start_line: int | None = None,
        end_line: int | None = None,
        keyword: str | None = None,
        max_lines: int = 200,
    ) -> ArtifactSlice:
        if artifact_id not in self._artifacts:
            raise KeyError(f"Artifact '{artifact_id}' does not exist")
        _meta, content = self._artifacts[artifact_id]
        all_lines = content.splitlines()
        lines, truncated = _slice_lines(
            all_lines,
            start_line=start_line,
            end_line=end_line,
            keyword=keyword,
            max_lines=max_lines,
        )
        return ArtifactSlice(
            artifact_id=artifact_id,
            lines=lines,
            total_lines=len(all_lines),
            returned_lines=len(lines),
            truncated=truncated,
            query={
                "start_line": start_line,
                "end_line": end_line,
                "keyword": keyword,
                "max_lines": max_lines,
            },
        )
