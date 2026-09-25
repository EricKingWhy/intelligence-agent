"""能力面判定（`#307` AC「无 Provider 凭证时判定为 BLOCKED/SKIPPED，**不发模型请求**、不写 PASS」）。

全线不碰网络：`chain_from_settings` / `probe_endpoint` 都被替换成替身。**替身在这里是合适的**
——被测的是"什么时候**不**该发请求、以及发不出去时怎么归类"，真实端点行为由入库证据
（`docs/live_gate/**`）覆盖，不由本文件覆盖。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from pydantic import BaseModel, SecretStr

from agent_harness.model.config import ConfigError
from evaluation.live_gate import capability as capability_module
from evaluation.live_gate.capability import BLOCKED, READY, _host, check_capability
from tests import live_model_guard
from tests.live_model_guard import REASON_RATE_LIMITED, EndpointProbe

CONFIGURED_KEY = "configured-provider-key-0123456789"


class _StubSettings(BaseModel):
    """`credential_names` / `credential_values` 只认 `model_fields` + `SecretStr`。"""

    model_api_key: SecretStr = SecretStr("")
    fallback_model_api_key: SecretStr = SecretStr("")


def _chain(base_url: str = "https://api.example.com/v1"):
    primary = SimpleNamespace(
        provider="stub-provider", model_name="stub-model", base_url=base_url, fallback=None,
    )
    return [("primary", primary)]


def _probe(ok: bool, *, reason: str | None = None) -> EndpointProbe:
    return EndpointProbe(
        label="primary", provider="stub-provider", model_name="stub-model",
        base_url="https://api.example.com/v1", ok=ok, reason=reason,
        error_type="StubError",
    )


def _forbid_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """任何真实调用都必须炸掉 —— 本文件的判据之一是"根本没走到网络"。"""

    def _explode(*args, **kwargs):
        raise AssertionError("不应发起任何真实模型请求")

    monkeypatch.setattr(live_model_guard, "probe_endpoint", _explode)


async def _probe_ok(config, *, label, timeout):
    return _probe(True)


async def _probe_unclassified(config, *, label, timeout):
    return _probe(False, reason=None)


async def _probe_all_down(config, *, label, timeout):
    return _probe(False, reason=REASON_RATE_LIMITED)


@pytest.mark.asyncio
async def test_missing_credentials_blocks_before_any_request(monkeypatch) -> None:
    _forbid_network(monkeypatch)

    def _explode_chain(settings):
        raise AssertionError("缺凭证时不该去构造配置链")

    monkeypatch.setattr(live_model_guard, "chain_from_settings", _explode_chain)
    result = await check_capability(_StubSettings())
    assert result.verdict == BLOCKED
    assert result.ready is False
    assert result.probes == []
    assert result.credential_names == []
    assert "未配置任何模型凭证" in result.reason


@pytest.mark.asyncio
async def test_unbuildable_chain_blocks_and_masks_the_error(monkeypatch) -> None:
    """"声明了却建不起来"是配置缺陷 ⇒ BLOCKED，且报错原文里的 key 形状必须先脱敏。"""
    _forbid_network(monkeypatch)
    leaked = "sk-" + "a" * 24
    monkeypatch.setattr(
        live_model_guard, "chain_from_settings",
        lambda settings: (_ for _ in ()).throw(ConfigError(f"api_key={leaked} 无效")),
    )
    result = await check_capability(_StubSettings(model_api_key=SecretStr(CONFIGURED_KEY)))
    assert result.verdict == BLOCKED
    assert result.probes == []
    assert "配置链建不起来" in result.reason
    assert leaked not in result.reason
    assert CONFIGURED_KEY not in result.reason


@pytest.mark.asyncio
async def test_all_endpoints_classified_unavailable_blocks(monkeypatch) -> None:
    monkeypatch.setattr(live_model_guard, "chain_from_settings", lambda settings: _chain())
    monkeypatch.setattr(live_model_guard, "probe_endpoint", _probe_all_down)
    result = await check_capability(_StubSettings(model_api_key=SecretStr(CONFIGURED_KEY)))
    assert result.verdict == BLOCKED
    assert result.ready is False
    assert [probe.ok for probe in result.probes] == [False]
    assert result.provider is not None
    assert result.provider.provider_id == "stub-provider"
    assert result.provider.base_url_host == "api.example.com"
    assert result.credential_names == ["MODEL_API_KEY"]


@pytest.mark.asyncio
async def test_unclassified_failure_stays_ready(monkeypatch) -> None:
    """fail-closed：判不出环境的失败**不**能换来一个 BLOCKED（那等于用"没判出来"免跑）。"""
    monkeypatch.setattr(live_model_guard, "chain_from_settings", lambda settings: _chain())
    monkeypatch.setattr(live_model_guard, "probe_endpoint", _probe_unclassified)
    result = await check_capability(_StubSettings(model_api_key=SecretStr(CONFIGURED_KEY)))
    assert result.verdict == READY


@pytest.mark.asyncio
async def test_any_working_endpoint_is_ready(monkeypatch) -> None:
    monkeypatch.setattr(live_model_guard, "chain_from_settings", lambda settings: _chain())
    monkeypatch.setattr(live_model_guard, "probe_endpoint", _probe_ok)
    result = await check_capability(_StubSettings(model_api_key=SecretStr(CONFIGURED_KEY)))
    assert result.verdict == READY
    assert result.probes[0].message == "可用"


def test_host_strips_path_query_and_userinfo() -> None:
    assert _host("https://api.example.com/v1") == "api.example.com"
    assert _host("https://api.example.com/v1?k=secret#frag") == "api.example.com"
    assert _host("https://user:hunter2@api.example.com/v1") == "api.example.com"
    assert _host("api.example.com:8080") == "api.example.com:8080"
    assert _host("") == "(empty)"


def test_probe_records_never_carry_the_raw_detail() -> None:
    """`EndpointProbe.message()` 是固定文案：上游回显原文（可能在 `detail` 里）不进证据。"""
    probe = EndpointProbe(
        label="primary", provider="p", model_name="m", base_url="https://h/v1",
        ok=False, reason=REASON_RATE_LIMITED, error_type="RateLimitError",
        detail="request failed with api_key=should-not-appear",
    )
    record = capability_module._probe_records([probe])[0]
    assert "should-not-appear" not in record.message
    assert "should-not-appear" not in record.model_dump_json()
