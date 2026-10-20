"""#149 ARCH-6 / ADR-0024 / #1353 §5：memory provider seam —— 白名单单一事实源。

#1353 §5 之后记忆域只剩**一个**实现（markdown 权威 + SQLite FTS5），
`build_memory_components` 及其分派表随向量腿一起删除；`langmem` 退化为历史别名。
本文件钉住仍成立的两条契约：

1. **白名单同源**：`factories.memory_provider_names()` 与装配期 `_known_providers("memory")`
   是同一个集合，且恰好是 `{builtin, langmem}`（既有 `.env` 不能因改名启动失败）。
2. **未知 provider 响亮失败**：装配期是 `CapabilityError(init_failed)`，不静默落到默认实现。
3. **真 V2 装配**：`builtin` 走到 `provider_name == "builtin-v2"` 的注册表条目。
"""

from __future__ import annotations

import pytest

from agent_harness.capability.base import (
    CapabilityError,
    CapabilityRegistry,
)
from agent_harness.capability.config import parse_capabilities_config
from agent_harness.capability.factories import memory_provider_names
from agent_harness.capability.wiring import _known_providers, wire_capabilities
from agent_harness.config import Settings


def _settings(tmp_path) -> Settings:
    return Settings(
        _env_file=None,
        workspace_dir=str(tmp_path),
        model_api_key="sk-test",
    )


def test_whitelist_is_the_single_source_of_truth() -> None:
    """AC1/AC3：白名单不是第二份要手工同步的清单。"""
    names = memory_provider_names()
    # 冻结的期望（非重言）：memory 只剩一个实现 + 一个历史别名。
    assert names == {"builtin", "langmem"}
    assert _known_providers("memory") == names


def test_deprecated_alias_is_still_accepted() -> None:
    """AC3 明确"不硬失败"：既有 `.env` 写着 langmem 的部署不能因改名而启动失败。"""
    assert "langmem" in _known_providers("memory")


@pytest.mark.asyncio
async def test_unknown_provider_fails_loudly_at_assembly(tmp_path) -> None:
    """AC1/AC5：未知 provider 在装配期是硬失败（不走 OPTIONAL_RUNTIME 降级）。"""
    with pytest.raises(CapabilityError) as err:
        await wire_capabilities(
            CapabilityRegistry(),
            parse_capabilities_config('{"memory": {"provider": "mem0"}}'),
            settings=_settings(tmp_path),
        )
    assert err.value.code == "init_failed"
    assert "mem0" in str(err.value)


@pytest.mark.asyncio
async def test_builtin_config_wires_the_v2_service(tmp_path) -> None:
    """生产装配总是 V2：注册表描述符与 `wiring.memory_v2` 指向同一个实现。"""
    registry = CapabilityRegistry()
    wiring = await wire_capabilities(
        registry,
        parse_capabilities_config('{"memory": {"provider": "builtin"}}'),
        settings=_settings(tmp_path),
    )

    assert registry.descriptor("memory").provider_name == "builtin-v2"
    assert registry.get("memory") is wiring.memory_v2
    assert wiring.memory_v2 is not None
    assert wiring.memory_formation is None
    await wiring.aclose()
