"""T2：CAPABILITIES 配置解析 + wire_capabilities 装配（spec 08 §6/§9，ADR-0010 Q4-Q6）。"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from agent_harness.capability.base import (
    CapabilityDescriptor,
    CapabilityError,
    CapabilityRegistry,
    Degradation,
)
from agent_harness.capability.config import ProviderConfig, parse_capabilities_config
from agent_harness.capability.wiring import CapabilityWiring, wire_capabilities
from agent_harness.config import Settings
from agent_harness.tooling import Tool, ToolResult


class TestParseConfig:
    def test_empty_or_none_is_empty_map(self):
        assert parse_capabilities_config(None) == {}
        assert parse_capabilities_config("") == {}
        assert parse_capabilities_config("   ") == {}

    def test_valid_json_parses(self):
        config = parse_capabilities_config(
            '{"memory": {"provider": "langmem", "enabled": true, "options": {"k": 1}}}'
        )
        assert config["memory"].provider == "langmem"
        assert config["memory"].enabled is True
        assert config["memory"].options == {"k": 1}

    def test_defaults(self):
        config = parse_capabilities_config('{"memory": {}}')
        assert config["memory"].provider == "builtin"
        assert config["memory"].enabled is True
        assert config["memory"].options == {}

    def test_invalid_json_raises_init_failed(self):
        with pytest.raises(CapabilityError) as err:
            parse_capabilities_config("{not json")
        assert err.value.code == "init_failed"

    def test_non_dict_json_raises(self):
        with pytest.raises(CapabilityError) as err:
            parse_capabilities_config('["memory"]')
        assert err.value.code == "init_failed"

    def test_bad_field_raises(self):
        with pytest.raises(CapabilityError) as err:
            parse_capabilities_config('{"memory": {"enabled": "yes"}}')
        assert err.value.code == "init_failed"


def _memory_settings(tmp_path, *, ready: bool) -> Settings:
    if ready:
        return Settings(
            _env_file=None, workspace_dir=str(tmp_path),
            milvus_uri="https://example.test", milvus_token="test-only",
            milvus_collection="col", embedding_model="m",
            embedding_base_url="https://x", embedding_api_key="k",
        )
    return Settings(_env_file=None, workspace_dir=str(tmp_path))


class _FakeMemoryComponents:
    def __init__(self):
        self.initialized = False
        self.relay_started = False
        self.closed = False
        self.capability = object()
        self.writeback = object()

        class _Relay:
            def start(self): self.owner.relay_started = True  # type: ignore[attr-defined]
            async def stop(self): pass
        self.relay = _Relay()
        self.relay.owner = self

    async def initialize(self): self.initialized = True
    async def close(self): self.closed = True


class _TickerTool(Tool):
    @property
    def name(self) -> str:
        return "tick"

    @property
    def description(self) -> str:
        return "demo tick"

    from pydantic import BaseModel as _BM
    @property
    def args_schema(self) -> type:
        class _Args(self._BM):
            pass
        return _Args

    async def execute(self, args) -> ToolResult:
        return ToolResult.success(message="ticked", data={})


class _ContributesToolsProvider:
    def contributes_tools(self) -> list:
        return [_TickerTool()]


class TestWireCapabilities:
    @pytest.mark.asyncio
    async def test_memory_unready_is_skipped_not_registered(self, tmp_path):
        """配置不齐 → OPTIONAL_RUNTIME 降级：不注册、不注入、无 writer（Phase 6 行为）。"""
        registry = CapabilityRegistry()
        wiring = await wire_capabilities(
            registry, parse_capabilities_config('{"memory": {}}'),
            settings=_memory_settings(tmp_path, ready=False),
        )
        assert registry.available() == []
        assert wiring.context_providers == []
        assert wiring.memory_writer is None
        assert wiring.memory is None

    @pytest.mark.asyncio
    async def test_memory_ready_registers_and_wires(self, tmp_path, monkeypatch):
        fake = _FakeMemoryComponents()
        monkeypatch.setattr(
            "agent_harness.capability.factories.build_memory_components",
            lambda settings: fake,
        )
        registry = CapabilityRegistry()
        wiring = await wire_capabilities(
            registry, parse_capabilities_config('{"memory": {"provider": "langmem"}}'),
            settings=_memory_settings(tmp_path, ready=True),
        )
        assert registry.descriptor("memory").provider_name == "langmem"
        assert registry.descriptor("memory").degradation is Degradation.OPTIONAL_RUNTIME
        assert registry.get("memory") is fake.capability
        assert fake.initialized and fake.relay_started
        assert len(wiring.context_providers) == 1
        assert wiring.memory_writer is fake.writeback
        assert wiring.memory is fake

    @pytest.mark.asyncio
    async def test_disabled_entry_is_skipped(self, tmp_path, monkeypatch):
        called = []
        monkeypatch.setattr(
            "agent_harness.capability.factories.build_memory_components",
            lambda settings: called.append(1),
        )
        registry = CapabilityRegistry()
        await wire_capabilities(
            registry, parse_capabilities_config('{"memory": {"enabled": false}}'),
            settings=_memory_settings(tmp_path, ready=True),
        )
        assert called == [] and registry.available() == []

    @pytest.mark.asyncio
    async def test_unknown_capability_raises_init_failed(self, tmp_path):
        with pytest.raises(CapabilityError) as err:
            await wire_capabilities(
                CapabilityRegistry(), parse_capabilities_config('{"finance": {}}'),
                settings=_memory_settings(tmp_path, ready=False),
            )
        assert err.value.code == "init_failed"

    @pytest.mark.asyncio
    async def test_unknown_provider_raises_not_silently_ignored(self, tmp_path):
        """config 写了未知 provider：显式失败（08 §5），不走降级、不静默换默认实现。"""
        with pytest.raises(CapabilityError) as err:
            await wire_capabilities(
                CapabilityRegistry(),
                parse_capabilities_config('{"memory": {"provider": "milvus"}}'),
                settings=_memory_settings(tmp_path, ready=True),
            )
        assert err.value.code == "init_failed"
        assert "milvus" in str(err.value)

    @pytest.mark.asyncio
    async def test_contributes_tools_collected_into_wiring(self, tmp_path):
        """实现了 ContributesTools 的 provider：其工具被收集（T5 由装配侧注册进 ToolRegistry）。"""
        registry = CapabilityRegistry()
        registry.register(
            CapabilityDescriptor(name="demo", version="1.0.0", provider_name="builtin",
                                 degradation=Degradation.OPTIONAL_RUNTIME),
            _ContributesToolsProvider(),
        )
        wiring = await wire_capabilities(
            registry, parse_capabilities_config(None),
            settings=_memory_settings(tmp_path, ready=False),
        )
        assert [t.name for t in wiring.tools] == ["tick"]

    @pytest.mark.asyncio
    async def test_empty_config_is_noop(self, tmp_path):
        wiring = await wire_capabilities(
            CapabilityRegistry(), parse_capabilities_config(None),
            settings=_memory_settings(tmp_path, ready=False),
        )
        assert isinstance(wiring, CapabilityWiring)
        assert wiring.tools == [] and wiring.context_providers == []

    @pytest.mark.asyncio
    async def test_dict_options_rejected_not_key_iterated(self, tmp_path):
        """directories 是 dict 时按键迭代会静默产出 Path("a")——显式拒绝。"""
        with pytest.raises(CapabilityError) as err:
            await wire_capabilities(
                CapabilityRegistry(),
                parse_capabilities_config('{"skills": {"options": {"directories": {"a": 1}}}}'),
                settings=_memory_settings(tmp_path, ready=False),
            )
        assert err.value.code == "init_failed"


# ── Round 7：半初始化的 MemoryComponents 必须被关闭（资源泄漏防线）──


@pytest.mark.asyncio
async def test_memory_partial_init_failure_closes_components(caplog):
    """initialize() 半途失败（Milvus 连上后 schema/embedding 探测挂）时，
    已构造的 gRPC channel / httpx client 必须被 close，而不是丢弃等 GC——
    否则对故障 Milvus 的后台重连永不停止，wiring.memory 未设置导致
    AppState.shutdown 也无法收尾。降级语义不变（OPTIONAL_RUNTIME 跳过）。
    """
    from unittest.mock import patch

    class FakeComponents:
        def __init__(self):
            self.closed = False
            self.relay = SimpleNamespace(stop=AsyncMock(return_value=None))
            self.writeback = SimpleNamespace(close=AsyncMock(return_value=None))
            self.vectors = SimpleNamespace(close=AsyncMock(return_value=None))

        async def initialize(self):
            raise RuntimeError("milvus schema mismatch")

        async def close(self):
            self.closed = True

    fake = FakeComponents()
    settings = Settings(
        milvus_uri="http://localhost:19530", milvus_token="tk",
        milvus_collection="c", embedding_model="e", embedding_base_url="http://x",
        embedding_api_key="k", _env_file=None,
    )
    registry = CapabilityRegistry()
    with patch("agent_harness.capability.factories.build_memory_components", return_value=fake):
        wiring = await wire_capabilities(
            registry,
            {"memory": ProviderConfig(provider="builtin", enabled=True)},
            settings=settings,
        )
    assert fake.closed, "半初始化的 components 必须被显式 close"
    assert wiring.memory is None  # 未注册 → 调用方无从关闭 → 必须在接线内关闭
