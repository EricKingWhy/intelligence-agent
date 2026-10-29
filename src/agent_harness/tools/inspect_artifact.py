"""InspectArtifactTool：模型按需局部读取已溢出 Artifact 的 Coding Tool。

第 10 个 Coding Tool，但与其余 9 个不同：
- 操作的是 Runtime 域存储（ArtifactStore）而非 Sandbox 内的 Workspace；
- 构造注入 ArtifactStore（不是 Sandbox）；
- READ_ONLY 但不经 Sandbox 权限检查。

模型通过 artifact_ref（从 ToolResult 获得）按行局部读取大输出细节，
大 Artifact 永远不完整灌回 Context（spec 06 §4）。
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from agent_harness.tooling.reconcile import ReconcileHint
from agent_harness.tools._artifact_read import _ArtifactArgs, _ArtifactReadTool


class _InspectArgs(_ArtifactArgs):
    artifact_ref: str = Field(..., description="要读取的 artifact ID（从 ToolResult.artifact_ref 获得）")

class InspectArtifactTool(_ArtifactReadTool):
    """inspect_artifact：从 ArtifactStore 局部读取大输出的细节。"""

    _artifact_label = "读取 artifact"
    _artifact_result_key = "artifact_id"

    @property
    def name(self) -> str:
        return "inspect_artifact"

    @property
    def description(self) -> str:
        return (
            "读取之前被自动保存为 artifact 的大输出（如 bash 的长 stdout）的局部内容。"
            "参数：artifact_ref（从工具结果的 artifact_ref 获得），"
            "start_line/end_line 按行范围读取，keyword 按关键词过滤，"
            "max_lines 返回行数上限（默认 200），"
            "max_chars_per_line 单行字符上限（默认 2000，单行超限时可放宽）。"
        )

    @property
    def args_schema(self) -> type[BaseModel]:
        return _InspectArgs

    @property
    def reconcile_hint(self) -> ReconcileHint:
        return ReconcileHint(
            verifiable=True,
            suggested_action="重新执行同样的 inspect_artifact 调用，核对返回的行内容是否一致（只读操作，重跑安全）。",
        )
