"""ReadArtifactTool: model reads externalized large tool results by ref.

Phase Multiturn T5 (#135): when a tool result exceeds the overflow threshold,
the raw content is externalized and the session keeps only a truncated
summary + ``artifact_ref``. This tool lets the model read back local slices
of the externalized artifact by ``artifact_ref`` (from ``ToolResult.artifact_ref``
or the ``ARTIFACT_EXTERNALIZED`` event).

与哪个 store 配对由 `storage/artifact_select.py` 决定（选 store 的地方同时定它与哪个
读回工具配对）：本工具服务 T5 **外置链路**所用的 store —— MinIO（#135）与 Local
（#192，spec 06 §3 的默认 Provider）。

Distinct from ``InspectArtifactTool`` (which pairs with the generic
``S3ArtifactStore``): 两者最终都调 ``ArtifactStore.inspect``，所以这个分工是历史配对，
不是能力差异。
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from agent_harness.tooling.reconcile import ReconcileHint
from agent_harness.tools._artifact_read import _ArtifactArgs, _ArtifactReadTool


class _ReadArgs(_ArtifactArgs):
    artifact_ref: str = Field(
        ...,
        description="要读取的外置产物 ID（从工具结果的 artifact_ref 或 ARTIFACT_EXTERNALIZED 事件获得）",
    )

class ReadArtifactTool(_ArtifactReadTool):
    """read_artifact: 从外置对象存储局部读取大产物的细节。"""

    _artifact_label = "读取外置产物"
    _artifact_result_key = "artifact_ref"

    @property
    def name(self) -> str:
        return "read_artifact"

    @property
    def description(self) -> str:
        return (
            "读取之前被自动保存为外置产物的大输出（如 bash 的长 stdout）的局部内容。"
            "参数：artifact_ref（从工具结果的 artifact_ref 获得），"
            "start_line/end_line 按行范围读取，keyword 按关键词过滤，"
            "max_lines 返回行数上限（默认 200），"
            "max_chars_per_line 单行字符上限（默认 2000，超限可放宽）。"
        )

    @property
    def args_schema(self) -> type[BaseModel]:
        return _ReadArgs

    @property
    def reconcile_hint(self) -> ReconcileHint:
        return ReconcileHint(
            verifiable=True,
            suggested_action="重新执行同样的 read_artifact 调用，核对返回的行内容是否一致（只读操作，重跑安全）。",
        )
