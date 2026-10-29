"""Characterize both artifact tool contracts across interchangeable stores."""
import pytest

from agent_harness.config import Settings
from agent_harness.storage.artifact import FakeArtifactStore
from agent_harness.storage.local_artifact import LocalArtifactStore
from agent_harness.tooling import ErrorCode, ToolExecutor, ToolRegistry
from agent_harness.tooling.permission_rules import (
    PermissionRule,
    PermissionRuleSet,
    RuleVerdict,
)
from agent_harness.tools.inspect_artifact import InspectArtifactTool
from agent_harness.tools.read_artifact import ReadArtifactTool


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_type,key", [(ReadArtifactTool, "artifact_ref"), (InspectArtifactTool, "artifact_id")])
@pytest.mark.parametrize("provider", ["fake", "local"])
async def test_artifact_tools_preserve_bounded_reads_and_failure_contract(tmp_path, tool_type, key, provider):
    store = FakeArtifactStore() if provider == "fake" else LocalArtifactStore(Settings(artifact_dir=str(tmp_path)), session_id="session-1")
    artifact = await store.save("session-1", "first\nneedle " + "x" * 40 + "\nlast", mime_type="text/plain", source_tool="bash", tool_call_id="call")
    tool = tool_type(store)
    registry = ToolRegistry()
    registry.register(tool)
    executor = ToolExecutor(registry)
    result = await executor.execute({"id": "read-1", "name": tool.name, "args": {
        "artifact_ref": artifact.artifact_id, "start_line": 2, "end_line": 3,
        "keyword": "needle", "max_lines": 1, "max_chars_per_line": 8,
    }})
    assert result.tool_call_id == "read-1"
    assert result.result.ok
    assert result.result.data[key] == artifact.artifact_id
    assert result.result.data["lines"] == [{"line_number": 2, "text": "needle x", "truncated": True, "full_length": 47}]
    assert result.result.data["returned_lines"] == 1
    assert result.result.data["truncated"] is True
    invalid = await executor.execute({"id": "bad-1", "name": tool.name, "args": {"artifact_ref": artifact.artifact_id, "max_lines": 0}})
    assert invalid.tool_call_id == "bad-1"
    assert invalid.result.error_code is ErrorCode.INVALID_ARGUMENT
    missing = await executor.execute({"id": "missing-1", "name": tool.name, "args": {"artifact_ref": "0000000000000000"}})
    assert missing.tool_call_id == "missing-1"
    assert missing.result.error_code is ErrorCode.INVALID_ARGUMENT


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_type", [ReadArtifactTool, InspectArtifactTool])
async def test_artifact_tool_denial_precedes_read(tool_type):
    tool = tool_type(FakeArtifactStore())
    registry = ToolRegistry()
    registry.register(tool)
    deny = PermissionRule(
        name="deny-artifact", tools=frozenset({tool.name}), verdict=RuleVerdict.DENY,
        reason="denied", source="test",
    )
    executor = ToolExecutor(registry, permission_rules=PermissionRuleSet(deny=(deny,)))
    result = await executor.execute({"id": "denied-1", "name": tool.name, "args": {"artifact_ref": "missing"}})
    assert result.tool_call_id == "denied-1"
    assert result.result.error_code is ErrorCode.PERMISSION_DENIED
