"""Shared bounded artifact inspection behind the two public tool contracts."""

from __future__ import annotations

from pydantic import BaseModel, Field

from agent_harness.prompt import DEFAULT_REGISTRY
from agent_harness.storage.artifact import ArtifactStore
from agent_harness.tooling import Tool, ToolResult, ToolSideEffect
from agent_harness.tooling.contract import ToolPermission
from agent_harness.tooling.result import ErrorCode


class _ArtifactArgs(BaseModel):
    artifact_ref: str = Field(
        ...,
        description="要读取的外置产物 ID（从工具结果的 artifact_ref 或 ARTIFACT_EXTERNALIZED 事件获得）",
    )
    start_line: int | None = Field(default=None, ge=1, description="起始行号（1-based）")
    end_line: int | None = Field(default=None, ge=1, description="结束行（含）")
    keyword: str | None = Field(default=None, description="关键词过滤，只返回含此关键词的行")
    max_lines: int = Field(default=200, ge=1, le=1000, description="返回行数上限（默认 200，最大 1000）")
    max_chars_per_line: int = Field(
        default=2000, ge=1, le=20000,
        description="单行字符上限（默认 2000）。单行被截断时返回体携带 truncated 与 full_length，"
                    "可放宽本参数重新读取更长片段。",
    )



class _ArtifactReadTool(Tool):
    def __init__(self, artifact_store: ArtifactStore) -> None:
        self._store = artifact_store

    @property
    def side_effect(self) -> ToolSideEffect:
        return ToolSideEffect.READ_ONLY

    @property
    def permission(self) -> ToolPermission:
        return ToolPermission.READ_ONLY

    @property
    def replay_safe(self) -> bool:
        # 只读 + 幂等：重复回读同一产物无外部副作用（#357 契约 7）。
        return True

    async def execute(self, args: _ArtifactArgs) -> ToolResult:
        try:
            result = await self._store.inspect(
                args.artifact_ref,
                start_line=args.start_line,
                end_line=args.end_line,
                keyword=args.keyword,
                max_lines=args.max_lines,
                max_chars_per_line=args.max_chars_per_line,
            )
        except KeyError as e:
            return ToolResult.failure(
                message=str(e),
                error_code=ErrorCode.INVALID_ARGUMENT,
            )
        return ToolResult.success(
            # `#519` BUG-12：artifact 正文（data.lines）来自外部世界，逐条不可信标注。
            message=f"{DEFAULT_REGISTRY.assemble('frame:untrusted_tool_output').fragment_text}"
                    f"{self._artifact_label} {args.artifact_ref}：返回 {result.returned_lines} 行"
                    + ("（已截断）" if result.truncated else "")
                    + f"，共 {result.total_lines} 行。",
            data={
                self._artifact_result_key: result.artifact_id,
                "lines": result.lines,
                "total_lines": result.total_lines,
                "returned_lines": result.returned_lines,
                "truncated": result.truncated,
                "query": result.query,
            },
        )
