"""#298 / MEM-V2-2 的记忆模型角色解析（R9 / PRD §5.3 第 3 条）。

Seam：`resolve_memory_roles` —— 纯函数，输入 `Settings`，输出两个 `ModelConfig`（或 `None`）。

本文件钉三件事：

1. **别名指向哪个 provider**。`memory.primary` → `senseaudio`、`memory.fallback` → `qwen`
   是 PRD 固定值；而且这两个 provider 名必须真的在 `PROVIDER_PRESETS` 里。拼错一个字母
   会让角色**永远**解析不出，而运行时这只表现为"记忆作业静默降级"——最难发现的那种坏。
2. **从哪读配置**：显式目录（`AGENT_MODELS`）优先，其次是"默认链恰好就是该 provider"。
   "未配置"（⇒ `None`）与"配错了"（⇒ 响亮抛 `ConfigError`）刻意分开。
3. **凭据不外泄**：解析结果拿在手里是 `SecretStr`，`repr` / `str` / `vars` 里不得出现明文
   （ticket Context 末句：不得暴露本地凭据与端点值）。
"""

from __future__ import annotations

import dataclasses
import json

import pytest

from agent_harness.config import Settings
from agent_harness.memory.v2.roles import (
    MEMORY_FALLBACK_ALIAS,
    MEMORY_FALLBACK_PROVIDER,
    MEMORY_PRIMARY_ALIAS,
    MEMORY_PRIMARY_PROVIDER,
    resolve_memory_roles,
)
from agent_harness.model.config import PROVIDER_PRESETS, ConfigError

# --------------------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------------------


def _entry(name: str, provider: str, model_name: str, **extra: object) -> dict:
    return {"name": name, "provider": provider, "model_name": model_name, **extra}


def _catalog(*entries: dict) -> str:
    return json.dumps(list(entries))


def _settings(**kwargs: object) -> Settings:
    """`_env_file=None`：本文件的解析只看显式传参（conftest 已密封环境变量）。

    默认给一把假 key：目录里没写 `api_key` 的条目会回落 `MODEL_API_KEY`，空 key 会
    触发 `ModelConfig` 的快速失败（那不是本文件多数用例要考察的东西）。
    """
    values: dict[str, object] = {"model_api_key": "test-key"}
    values.update(kwargs)
    return Settings(_env_file=None, **values)


# --------------------------------------------------------------------------------------
# 1 · 别名与 provider 名
# --------------------------------------------------------------------------------------


def test_the_aliases_are_the_ones_the_prd_fixes() -> None:
    assert MEMORY_PRIMARY_ALIAS == "memory.primary"
    assert MEMORY_FALLBACK_ALIAS == "memory.fallback"


@pytest.mark.parametrize(
    "provider", [MEMORY_PRIMARY_PROVIDER, MEMORY_FALLBACK_PROVIDER], ids=["primary", "fallback"]
)
def test_each_alias_names_a_provider_the_repository_knows(provider: str) -> None:
    """别名必须落在 `PROVIDER_PRESETS` 里——否则角色永远解析不出，且无人报错。"""
    assert provider in PROVIDER_PRESETS


def test_each_alias_points_at_the_provider_the_prd_names() -> None:
    assert MEMORY_PRIMARY_PROVIDER == "senseaudio"
    assert MEMORY_FALLBACK_PROVIDER == "qwen"


# --------------------------------------------------------------------------------------
# 2 · 从显式目录（AGENT_MODELS）解析
# --------------------------------------------------------------------------------------


def test_a_catalog_entry_for_senseaudio_resolves_the_primary_role() -> None:
    settings = _settings(agent_models=_catalog(
        _entry("mem-primary", MEMORY_PRIMARY_PROVIDER, "senseaudio-small")))
    roles = resolve_memory_roles(settings)

    assert roles.primary is not None
    assert roles.primary.provider == MEMORY_PRIMARY_PROVIDER
    assert roles.primary.model_name == "senseaudio-small"
    assert roles.fallback is None


def test_a_catalog_entry_for_qwen_resolves_the_fallback_role() -> None:
    settings = _settings(agent_models=_catalog(
        _entry("mem-fallback", MEMORY_FALLBACK_PROVIDER, "qwen-turbo")))
    roles = resolve_memory_roles(settings)

    assert roles.fallback is not None
    assert roles.fallback.provider == MEMORY_FALLBACK_PROVIDER
    assert roles.fallback.model_name == "qwen-turbo"
    assert roles.primary is None


def test_one_catalog_can_configure_both_roles_independently() -> None:
    settings = _settings(agent_models=_catalog(
        _entry("mem-primary", MEMORY_PRIMARY_PROVIDER, "sa-model"),
        _entry("mem-fallback", MEMORY_FALLBACK_PROVIDER, "qwen-model")))
    roles = resolve_memory_roles(settings)

    assert roles.primary is not None and roles.primary.model_name == "sa-model"
    assert roles.fallback is not None and roles.fallback.model_name == "qwen-model"
    assert roles.has_fallback is True


def test_an_entry_for_another_provider_does_not_satisfy_a_role() -> None:
    """目录里有别的 provider（真实形态：选择器的候选列表）不等于记忆角色已配置。"""
    settings = _settings(agent_models=_catalog(_entry("d", "deepseek", "deepseek-chat")))
    roles = resolve_memory_roles(settings)

    assert roles.primary is None
    assert roles.fallback is None


def test_the_first_declared_entry_of_the_provider_wins() -> None:
    """同 provider 多条 = 真实形态（选择器候选）；规则是确定的：取声明顺序第一条。"""
    settings = _settings(agent_models=_catalog(
        _entry("first", MEMORY_PRIMARY_PROVIDER, "first-model"),
        _entry("second", MEMORY_PRIMARY_PROVIDER, "second-model")))
    roles = resolve_memory_roles(settings)

    assert roles.primary is not None
    assert roles.primary.model_name == "first-model"


def test_the_catalog_entry_wins_over_the_main_chain() -> None:
    settings = _settings(
        model_provider=MEMORY_PRIMARY_PROVIDER, model_name="chain-model", model_api_key="chain",
        agent_models=_catalog(_entry("explicit", MEMORY_PRIMARY_PROVIDER, "catalog-model")))
    roles = resolve_memory_roles(settings)

    assert roles.primary is not None
    assert roles.primary.model_name == "catalog-model"


# --------------------------------------------------------------------------------------
# 3 · 从 `.env` 的默认链解析
# --------------------------------------------------------------------------------------


def test_the_main_chain_satisfies_the_primary_role_when_it_is_senseaudio() -> None:
    """主链本来就是 senseaudio 的部署 ⇒ 零新增配置即可启用记忆作业。"""
    settings = _settings(
        model_provider=MEMORY_PRIMARY_PROVIDER, model_name="chain-model",
        model_api_key="chain-secret")
    roles = resolve_memory_roles(settings)

    assert roles.primary is not None
    assert roles.primary.model_name == "chain-model"


def test_the_main_chain_does_not_satisfy_the_primary_role_for_another_provider() -> None:
    settings = _settings(
        model_provider="deepseek", model_name="deepseek-chat", model_api_key="chain-secret")
    roles = resolve_memory_roles(settings)

    assert roles.primary is None


def test_the_chain_fallback_satisfies_the_fallback_role_when_it_is_qwen() -> None:
    settings = _settings(
        model_provider="deepseek", model_name="deepseek-chat", model_api_key="chain",
        fallback_model_provider=MEMORY_FALLBACK_PROVIDER,
        fallback_model_name="qwen-max", fallback_model_api_key="fallback-secret")
    roles = resolve_memory_roles(settings)

    assert roles.fallback is not None
    assert roles.fallback.provider == MEMORY_FALLBACK_PROVIDER
    assert roles.fallback.model_name == "qwen-max"
    assert roles.has_fallback is True
    assert roles.primary is None


def test_the_main_chain_fallback_is_not_reused_as_the_memory_fallback() -> None:
    """主链的 `.fallback`（ADR-0014 的两级链）**不是** `memory.fallback`。

    主链 fallback 是 mimo ⇒ 记忆的 fallback 角色仍未配置（`None`）；而且主链 primary
    被当作记忆 primary 返回时，它自己身上的 `.fallback` 也必须已被摘掉——否则
    "memory.fallback 是 qwen"这句话在主链配了别的 fallback 时就不成立。
    """
    settings = _settings(
        model_provider=MEMORY_PRIMARY_PROVIDER, model_name="chain-model", model_api_key="chain",
        fallback_model_provider="mimo", fallback_model_name="mimo-v2.6-flash",
        fallback_model_api_key="mimo-secret")
    roles = resolve_memory_roles(settings)

    assert roles.primary is not None
    assert roles.fallback is None
    assert roles.has_fallback is False
    assert roles.primary.fallback is None


def test_an_unconfigured_role_resolves_to_none() -> None:
    roles = resolve_memory_roles(_settings())

    assert roles.primary is None
    assert roles.fallback is None
    assert roles.has_fallback is False


# --------------------------------------------------------------------------------------
# 4 · 未配置 ≠ 配错了
# --------------------------------------------------------------------------------------


def test_a_provider_without_a_credential_fails_loudly_instead_of_going_absent() -> None:
    """provider 是 senseaudio 但没有 key：这是配置错误，不是"没配这个角色"。

    静默成 `None` 会让一个错误配置表现为"记忆永远不形成"，而没有任何地方报错。
    与装配层既有姿势一致：`.env` 的模型链本来就要求 key（`create_chat_model`）。
    """
    settings = _settings(
        model_provider=MEMORY_PRIMARY_PROVIDER, model_name="chain-model", model_api_key="")

    with pytest.raises(ConfigError):
        resolve_memory_roles(settings)


def test_a_malformed_catalog_fails_loudly_instead_of_going_absent() -> None:
    settings = _settings(agent_models="{not json")

    with pytest.raises(ConfigError):
        resolve_memory_roles(settings)


# --------------------------------------------------------------------------------------
# 5 · 凭据不外泄
# --------------------------------------------------------------------------------------


def test_a_resolved_role_never_prints_its_credential() -> None:
    secret = "sk-" + "not-a-real-key" * 3
    settings = _settings(agent_models=_catalog(
        _entry("mem-primary", MEMORY_PRIMARY_PROVIDER, "sa-model", api_key=secret)))
    roles = resolve_memory_roles(settings)

    assert roles.primary is not None
    assert secret not in repr(roles)
    assert secret not in str(roles)
    assert secret not in repr(roles.primary)
    assert secret not in str(vars(roles.primary))
    # 明文只有一个出口（SDK 请求边界）。
    assert roles.primary.get_secret_value() == secret


def test_the_roles_view_does_not_print_the_local_endpoint() -> None:
    """ticket Context 末句的另一半：不得暴露**端点值**。

    解析结果里的 `ModelConfig` 是普通对象（默认 `repr` 只有类名与地址），容器本身是
    frozen dataclass —— 两层 repr 都不带字段值。这条把它钉住：将来谁给 `ModelConfig`
    加一个把字段拼进去的 `__repr__`，这里会红。
    """
    local_endpoint = "https://internal-proxy.invalid/v1"
    settings = _settings(agent_models=_catalog(
        _entry("mem-primary", MEMORY_PRIMARY_PROVIDER, "sa-model",
               base_url=local_endpoint, api_key="k")))
    roles = resolve_memory_roles(settings)

    assert roles.primary is not None
    assert roles.primary.base_url == local_endpoint  # 解析本身是对的
    assert local_endpoint not in repr(roles)
    assert local_endpoint not in str(roles)


def test_the_roles_view_is_frozen() -> None:
    """解析结果是一次快照：装配层拿到之后不能再被就地改写。"""
    roles = resolve_memory_roles(_settings())

    with pytest.raises(dataclasses.FrozenInstanceError):
        roles.primary = None  # type: ignore[misc]
