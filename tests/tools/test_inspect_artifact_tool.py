"""InspectArtifactTool 单元测试。

验证：
- 构造注入 ArtifactStore（不是 Sandbox）
- inspect 按 artifact_id 读回局部内容
- 按 start_line/end_line 行范围
- 按 keyword 关键词过滤
- max_lines 截断
- 不存在的 artifact_id → ToolResult.failure(INVALID_ARGUMENT)
- reconcile_hint verifiable=True
- 注册到 ToolRegistry 可被查找
"""

from __future__ import annotations

import pytest

from agent_harness.storage.artifact import FakeArtifactStore
from agent_harness.tooling.contract import ToolPermission, ToolSideEffect
from agent_harness.tooling.registry import ToolRegistry
from agent_harness.tools.inspect_artifact import InspectArtifactTool


@pytest.fixture
def setup() -> tuple[InspectArtifactTool, str]:
    """同步构造（FakeArtifactStore 内容预填不需 async）。"""
    store = FakeArtifactStore()
    lines = [f"line {i}" for i in range(1, 51)]  # 50 行
    content = "\n".join(lines)
    # 直接写入内部 dict（跳过 async save）
    from agent_harness.storage.artifact import Artifact, compute_artifact_id
    from agent_harness.storage.sqlite import _utc_now_iso
    artifact_id = compute_artifact_id(content)
    store._artifacts[artifact_id] = (
        Artifact(
            artifact_id=artifact_id,
            session_id="s1",
            size=len(content.encode("utf-8")),
            mime_type="text/plain",
            source_tool="bash",
            tool_call_id="c1",
            created_at=_utc_now_iso(),
        ),
        content,
    )
    tool = InspectArtifactTool(store)
    return tool, artifact_id


class TestInspectArtifactToolContract:
    def test_name(self, setup: tuple[InspectArtifactTool, str]) -> None:
        tool, _ = setup
        assert tool.name == "inspect_artifact"

    def test_side_effect_read_only(self, setup: tuple[InspectArtifactTool, str]) -> None:
        tool, _ = setup
        assert tool.side_effect == ToolSideEffect.READ_ONLY

    def test_permission_read_only(self, setup: tuple[InspectArtifactTool, str]) -> None:
        tool, _ = setup
        assert tool.permission == ToolPermission.READ_ONLY

    def test_reconcile_hint_verifiable(self, setup: tuple[InspectArtifactTool, str]) -> None:
        tool, _ = setup
        hint = tool.reconcile_hint
        assert hint.verifiable is True
        assert hint.suggested_action is not None


class TestInspectArtifactToolExecute:
    @pytest.mark.asyncio
    async def test_execute_default_reads_all(self, setup: tuple[InspectArtifactTool, str]) -> None:
        tool, artifact_id = setup
        args = tool.args_schema(artifact_id=artifact_id)
        result = await tool.execute(args)
        assert result.ok is True
        assert result.data["total_lines"] == 50
        assert result.data["returned_lines"] == 50
        assert result.data["truncated"] is False

    @pytest.mark.asyncio
    async def test_execute_line_range(self, setup: tuple[InspectArtifactTool, str]) -> None:
        tool, artifact_id = setup
        args = tool.args_schema(artifact_id=artifact_id, start_line=5, end_line=10)
        result = await tool.execute(args)
        assert result.ok is True
        assert result.data["returned_lines"] == 6
        assert result.data["lines"][0]["line_number"] == 5
        assert result.data["lines"][-1]["line_number"] == 10

    @pytest.mark.asyncio
    async def test_execute_keyword(self, setup: tuple[InspectArtifactTool, str]) -> None:
        tool, artifact_id = setup
        args = tool.args_schema(artifact_id=artifact_id, keyword="line 2")
        result = await tool.execute(args)
        assert result.ok is True
        # line 2, 20-29, etc → 所有含 "line 2" 的行
        assert all("line 2" in entry["text"] for entry in result.data["lines"])

    @pytest.mark.asyncio
    async def test_execute_max_lines_truncation(self, setup: tuple[InspectArtifactTool, str]) -> None:
        tool, artifact_id = setup
        args = tool.args_schema(artifact_id=artifact_id, max_lines=5)
        result = await tool.execute(args)
        assert result.ok is True
        assert result.data["returned_lines"] == 5
        assert result.data["truncated"] is True

    @pytest.mark.asyncio
    async def test_execute_nonexistent_returns_failure(self, setup: tuple[InspectArtifactTool, str]) -> None:
        tool, _ = setup
        args = tool.args_schema(artifact_id="nonexistent")
        result = await tool.execute(args)
        assert result.ok is False
        assert result.error_code is not None


class TestInspectArtifactToolRegistration:
    def test_register_in_registry(self) -> None:
        store = FakeArtifactStore()
        tool = InspectArtifactTool(store)
        registry = ToolRegistry()
        registry.register(tool)
        assert registry.get("inspect_artifact") is tool
