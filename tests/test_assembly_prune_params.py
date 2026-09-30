"""#414（W-31.2）裁剪参数化的装配面：旋钮不是死键。

与 `tests/test_assembly_bash_budget.py` 同一条缝——走真 `build_runtime`，只把
模型工厂换成替身。判据是 `runtime._context_builder._pruner` 上的**有效值**
（`keep_recent_tool_results` / `clear_at_least_tokens`，读私有字段有
test_assembly_persona.py / test_builder_prune.py 的先例）：Settings 有键、
assembly 透传、builder 构造三段里任何一段断线，在这里红。

外加 T7（Settings 边界）：`ge=0` 负值在 pydantic 构造期 ValidationError；
缺省 = 3/5000（票面定死，Anthropic 官方示例出处）。
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from langchain_core.messages import AIMessageChunk
from pydantic import ValidationError

from agent_harness.assembly import (
    assemble_wiring,
    build_runtime,
    initialize_stores,
    recovery_stores,
)
from agent_harness.config import Settings
from agent_harness.sandbox import WorkspaceRegistry
from agent_harness.tooling.contract import PermissionPolicy


class ScriptedModelFactory:
    """astream 可用的替身模型（装配可构造即可，不真调用）。"""

    def bind_tools(self, tools, **kwargs):
        return self

    async def astream(self, messages, **kwargs):
        yield AIMessageChunk(content="ok")


async def _pruner_values_after_assembly(tmp_path: Path, **overrides) -> tuple[int, int]:
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
            session_id="sess-prune-params",
            workspace=tmp_path / "workspaces" / "sess-prune-params",
            max_agent_turns=10,
            permission_mode=PermissionPolicy.WORKSPACE_WRITE,
        )
    pruner = runtime._context_builder._pruner
    assert pruner is not None, "生产装配必须启用 artifact store（裁剪面在位）"
    return pruner._keep_recent_tool_results, pruner._clear_at_least_tokens


def test_settings_defaults_are_frozen_official_examples():
    """T7a：缺省 3/5000（票面定死：Anthropic context editing 官方示例）。"""
    settings = Settings(_env_file=None, model_api_key="sk-test")
    assert settings.keep_recent_tool_results == 3
    assert settings.clear_at_least_tokens == 5000


def test_settings_negative_values_fail_loudly():
    """T7b：负值在 pydantic 构造期 ValidationError（ge=0；0 = 关闭护栏合法）。"""
    with pytest.raises(ValidationError):
        Settings(_env_file=None, model_api_key="sk-test", keep_recent_tool_results=-1)
    with pytest.raises(ValidationError):
        Settings(_env_file=None, model_api_key="sk-test", clear_at_least_tokens=-1)


@pytest.mark.asyncio
async def test_assembled_pruner_uses_the_default_guard_values(tmp_path):
    """T8a：缺省装配后旋钮 = 3/5000——接不上成死键在这里红。"""
    assert await _pruner_values_after_assembly(tmp_path) == (3, 5000)


@pytest.mark.asyncio
async def test_assembled_pruner_follows_settings_overrides(tmp_path):
    """T8b：改 Settings 后装配跟随（含 0=关闭的合法显式配置）。"""
    values = await _pruner_values_after_assembly(
        tmp_path, keep_recent_tool_results=1, clear_at_least_tokens=0,
    )
    assert values == (1, 0)
