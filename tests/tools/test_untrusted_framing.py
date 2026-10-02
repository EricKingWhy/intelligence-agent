"""#519 BUG-12 验收：exec/file 类工具结果的 untrusted framing。

票面验收锚点：bash / read / edit 的 `ToolResult.message` 均带
`frame:untrusted_tool_output`（文件内容与命令输出是最高频的 prompt 注入载体，
此前全仓只有 knowledge / websearch 两域有 framing）。既有 knowledge / websearch
行为不变——各自的 frame 挂点与文案不动，由 tests/prompt/test_frames_migration.py
与 tests/websearch/test_tools.py 继续覆盖。

边界声明：framing 是**纵深防御**，不替代 Sandbox / Permission（`AGENTS.md`
§7 不变量 11：边界是 Runtime 事实，不是提示词）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_harness.prompt import DEFAULT_REGISTRY
from agent_harness.sandbox import LocalSubprocessSandbox
from agent_harness.tooling import PermissionPolicy, ToolExecutor, ToolRegistry
from agent_harness.tools import BashTool, EditTool, ReadTool, WriteTool

_FRAME = DEFAULT_REGISTRY.assemble("frame:untrusted_tool_output").fragment_text


@pytest.fixture
def sandbox(tmp_path: Path) -> LocalSubprocessSandbox:
    return LocalSubprocessSandbox(workspace_root=tmp_path)


@pytest.fixture
def executor(sandbox: LocalSubprocessSandbox) -> ToolExecutor:
    reg = ToolRegistry()
    reg.register(ReadTool(sandbox))
    reg.register(WriteTool(sandbox))
    reg.register(BashTool(sandbox))
    reg.register(EditTool(sandbox))
    return ToolExecutor(reg, policy=PermissionPolicy.DANGER_FULL_ACCESS)


def _call(name: str, args: dict, call_id: str) -> dict:
    return {"id": call_id, "name": name, "args": args}


@pytest.mark.asyncio
async def test_bash_message_carries_untrusted_frame(executor: ToolExecutor):
    execution = await executor.execute(_call("bash", {"command": "echo hi"}, "c1"))

    assert execution.result.ok is True
    assert execution.result.message.startswith(_FRAME)


@pytest.mark.asyncio
async def test_read_message_carries_untrusted_frame(
    executor: ToolExecutor, tmp_path: Path
):
    await executor.execute(_call("write", {"path": "n.txt", "content": "hello"}, "w1"))

    execution = await executor.execute(_call("read", {"path": "n.txt"}, "r1"))

    assert execution.result.ok is True
    assert execution.result.message.startswith(_FRAME)


@pytest.mark.asyncio
async def test_edit_message_carries_untrusted_frame(executor: ToolExecutor):
    await executor.execute(
        _call("write", {"path": "e.txt", "content": "alpha beta"}, "w2")
    )

    execution = await executor.execute(
        _call(
            "edit",
            {"path": "e.txt", "old_string": "alpha", "new_string": "ALPHA"},
            "e1",
        )
    )

    assert execution.result.ok is True
    assert execution.result.message.startswith(_FRAME)


@pytest.mark.asyncio
async def test_write_confirmation_has_no_frame(executor: ToolExecutor):
    """写入确认的 message 不携带外部内容，不在挂点清单（bash/read/edit/artifact 读取）。"""
    execution = await executor.execute(
        _call("write", {"path": "w.txt", "content": "x"}, "w3")
    )

    assert execution.result.ok is True
    assert not execution.result.message.startswith(_FRAME)


def test_frame_registered_on_shared_untrusted_slot():
    """新 frame 与 knowledge / websearch 共用 `frame:untrusted_data`（9000）槽位键。"""
    sections = {s.name: s for s in DEFAULT_REGISTRY.available()}
    section = sections["frame:untrusted_tool_output"]
    assert section.order == sections["frame:untrusted_knowledge"].order == 9000
    assert section.target.value == "fragment"
    assert _FRAME  # 文案非空、可重复 assemble
