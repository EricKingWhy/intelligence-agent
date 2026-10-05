"""ToolResult 后处理：先完整保存，再返回摘要；不参与 Tool retry。"""

import json
import logging
from abc import ABC, abstractmethod
from typing import Any

from agent_harness.session import Session
from agent_harness.session.event import (
    ARTIFACT_EXTERNALIZED,
)
from agent_harness.storage.artifact import ArtifactStore
from agent_harness.tooling.result import ToolResult

logger = logging.getLogger("agent_harness.tooling.overflow")


class ArtifactOverflowUnavailable(RuntimeError):
    """Raised when oversized output cannot be externalized safely."""


def _payload_size(value: Any) -> int | None:
    """顶层字段参与溢出判定的载荷大小；``None`` = 无溢出可能或不参与。

    str 按字符数；dict/list 按紧凑 JSON 序列化后的字符数（与 ``overflow_chars``
    同一口径，非字节）。其余标量与**不可 JSON 序列化**或**序列化递归爆栈**
    （深嵌套 dict/list 触发 RecursionError）的对象返回 None：保持既有穿透
    行为，不在摘要路径上制造新崩溃面。多字段路径的 artifact 以 indent=2
    落盘，保存体积略大于判定值——模型侧替身仍受 _summarize 预算约束。
    """
    if isinstance(value, str):
        return len(value)
    if isinstance(value, (dict, list)):
        try:
            return len(json.dumps(value, ensure_ascii=False))
        except (TypeError, ValueError, RecursionError):
            return None
    return None


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
    """当 tool result 超过 ``overflow_chars`` 阈值时，将原始内容外置到
    ``ArtifactStore``，并在 session 中保留截断摘要 + ``artifact_ref``。

    默认保持 T5 (#135) 的 fail-open 降级；调用方可用 ``fail_open=False``
    要求输出必须进入受控 ArtifactStore，适用于 transport audit 等不能把大输出
    写入 Operation Ledger 的边界。
    """

    def __init__(
        self,
        store: ArtifactStore | None,
        overflow_chars: int = 2000,
        *,
        read_tool_name: str = "read_artifact",
        externalize_event_type: str = ARTIFACT_EXTERNALIZED,
        fail_open: bool = True,
    ) -> None:
        if overflow_chars <= 0:
            raise ValueError("overflow_chars must be positive")
        self._store = store
        self._overflow_chars = overflow_chars
        self._externalize_event_type = externalize_event_type
        self._fail_open = fail_open
        # 摘要里的读回提示必须点名**与本 store 配对的那个工具**（#186 AC4）：
        # 配对关系由 `storage/artifact_select.py` 决定——S3 配 `inspect_artifact`，
        # MinIO / Local 配 `read_artifact`。此前这里写死 `read_artifact`，于是
        # S3 部署的提示会把模型指向一个**没有注册**（配的是另一个 store）的工具名，
        # 前端也按同一段文字提取 artifact_id，名字对不上时归档内容永远显示不出来。
        # 默认值 = **默认 Provider**（Local，spec 06 §3）配对的工具；`assembly` 是唯一
        # 的生产构造点，永远显式传选择器给出的真实名字，默认值只服务测试。
        self._read_tool_name = read_tool_name
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
        # 溢出判定覆盖 data 全部顶层字段 + message（#644：T15b/O-1 + T8e）。
        # 大小按序列化后载荷计（str 字符数 / dict/list 紧凑 JSON 字符数）：
        # 此前只认 isinstance(value, str)，白名单六键（output/content/stdout/
        # stderr/before/after——Q12=B 的 diff 视图字段，各上限 _DIFF_MAX_BYTES=
        # 50KB）里装 50KB dict 直接穿透，白名单之外的字段（如 text）则完全
        # 零预算——两条都是回灌 Context 的旁路（不变量 #15）。
        # 已知例外：data 自带 "message" 键会被下行 result.message 覆盖、不参与
        # 判定（同名合并语义的限制；仓库内无生产者写该键，外部 MCP 工具若产出
        # 该形态需另票分离判定，见 review_ledger 登记项）。
        outputs: dict[str, Any] = {**data, "message": result.message}
        oversized = {key: value for key, value in outputs.items()
                     if (size := _payload_size(value)) is not None
                     and size > self._overflow_chars}
        if not oversized:
            return result, []
        # 单字段保持可还原原文：str 存原文（既有语义）；dict/list 存紧凑
        # JSON——store.load 后 json.loads 即得原始对象，不留隐藏原文副本。
        # 多字段共用一个可完整还原的 JSON Artifact（既有行为）。
        if len(oversized) == 1:
            value = next(iter(oversized.values()))
            is_text = isinstance(value, str)
            content = value if is_text else json.dumps(value, ensure_ascii=False)
            mime_type = "text/plain" if is_text else "application/json"
        else:
            content = json.dumps(oversized, ensure_ascii=False, indent=2)
            mime_type = "application/json"

        # T5 (#135): graceful degradation — if the store is unavailable,
        # keep the original (untruncated) tool result in-session.
        if self._store is None:
            if self._fail_open:
                return result, []
            raise ArtifactOverflowUnavailable(
                "Artifact store is required for oversized tool output"
            )
        try:
            artifact = await self._store.save(
                session.session_id, content, mime_type=mime_type,
                source_tool=tool_name, tool_call_id=tool_call_id,
            )
        except Exception as error:
            if not self._fail_open:
                raise ArtifactOverflowUnavailable(
                    "Artifact store failed for oversized tool output"
                ) from error
            logger.warning(
                "Artifact store unavailable, keeping raw tool result "
                "in-session (fail-open): %s",
                error,
            )
            return result, []

        summaries = {key: self._summarize(
                         value if isinstance(value, str)
                         else json.dumps(value, ensure_ascii=False),
                         artifact.artifact_id)
                     for key, value in oversized.items()}
        deferred = [(self._externalize_event_type, {
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
                  f"use {self._read_tool_name}({artifact_id}) to view]")
        # 行数限制之外再限制字符数，避免单行日志本身撑爆 Context。
        budget = max(0, (self._overflow_chars - len(marker) - 2) // 2)
        head = "\n".join(lines[:10])[:budget]
        tail = "\n".join(lines[-10:])[-budget:] if budget else ""
        return f"{head}\n{marker}\n{tail}"
