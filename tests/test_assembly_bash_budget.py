"""#244 AC5 的接线面：装配出的 `bash` 工具真的消费配置里的预算（旋钮不是死的）。

与 `tests/test_assembly.py` 同一条缝——走真 `build_runtime`，只把模型工厂换成替身。
判据是**有效预算**：`Tool.timeout_seconds`，也就是 `ToolExecutor` 建 attempt 时
用的那个数（ADR-0039 D1）。旋钮读得到、但没接到工具上，在这里会红。
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from langchain_core.messages import AIMessageChunk

from agent_harness.assembly import (
    assemble_wiring,
    build_runtime,
    initialize_stores,
    recovery_stores,
)
from agent_harness.config import Settings
from agent_harness.sandbox import WorkspaceRegistry
from agent_harness.tooling.contract import PermissionPolicy

FROZEN_BASH_TIMEOUT_SECONDS = 60.0


class ScriptedModelFactory:
    """astream 可用的替身模型（装配可构造即可，不真调用）。"""

    def bind_tools(self, tools, **kwargs):
        return self

    async def astream(self, messages, **kwargs):
        yield AIMessageChunk(content="ok")


async def _bash_budget_after_assembly(tmp_path: Path, **overrides) -> float:
    settings = Settings(
        _env_file=None,
        workspace_dir=str(tmp_path),
        model_api_key="sk-test",
        **overrides,
    )
    _, wiring = await assemble_wiring(settings)
    stores = recovery_stores(tmp_path / "harness.db")
    await initialize_stores(stores)
    with patch("agent_harness.assembly.create_chat_model",
               return_value=ScriptedModelFactory()):
        runtime = await build_runtime(
            settings=settings,
            wiring=wiring,
            stores=stores,
            workspace_registry=WorkspaceRegistry(root=tmp_path, backend="local"),
            session_id="sess-bash-budget",
            workspace=tmp_path / "workspaces" / "sess-bash-budget",
            max_agent_turns=10,
            permission_mode=PermissionPolicy.WORKSPACE_WRITE,
        )
    tools = {tool.name: tool for tool in runtime.registry.list()}
    return tools["bash"].timeout_seconds


@pytest.mark.asyncio
async def test_assembled_bash_tool_uses_the_default_frozen_budget(tmp_path):
    assert await _bash_budget_after_assembly(tmp_path) == FROZEN_BASH_TIMEOUT_SECONDS


@pytest.mark.asyncio
async def test_assembled_bash_tool_follows_the_configured_budget(tmp_path):
    """改配置即改有效预算——证明配置入口接到了工具上，不是死键。"""
    budget = await _bash_budget_after_assembly(tmp_path, bash_timeout_seconds=25.0)
    assert budget == 25.0
