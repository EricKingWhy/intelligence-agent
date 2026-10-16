"""#1197 项 1：上下文窗口五挡位的**构造期**白名单校验。

票面要求（实现要点）：「`src/agent_harness/config.py`：新增 `CONTEXT_WINDOW_TIERS`
常量 + `@field_validator` 白名单校验（照抄 `:246` 构造期校验先例，挡位外启动期
响亮失败）；`max_context_tokens`（`:110`）本身零改动」。

与 `tests/attachments/test_allowed_media_types_config.py` 同一条纪律：配置错误在
服务起来时即响亮失败，而不是拖到每次请求才在请求路径抛（那会变成 500，运维只能
从请求日志发现配置错误）。`_env_file=None` 的理由同
`tests/agent/test_local_budget_resolution.py`：断言的是**契约默认值**，不是本机
部署的 .env。
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from agent_harness.config import CONTEXT_WINDOW_TIERS, Settings
from agent_harness.context.builder import ContextBuilder


def _settings(**overrides: Any) -> Settings:
    return Settings(_env_file=None, model_api_key="sk-test", **overrides)


def test_tiers_are_the_five_decimal_values_the_ticket_names() -> None:
    """挡位一律十进制整数（票面验收：100000 / 200000 / 256000 / 300000 / 500000）。"""
    assert CONTEXT_WINDOW_TIERS == (100_000, 200_000, 256_000, 300_000, 500_000)


def test_default_context_window_matches_the_contract() -> None:
    """默认 200K：既有行为不变（票面「默认 200K」）。"""
    assert Settings(_env_file=None).max_context_tokens == 200_000


@pytest.mark.parametrize("tier", CONTEXT_WINDOW_TIERS)
def test_every_tier_passes_startup_validation(tier: int) -> None:
    assert _settings(max_context_tokens=tier).max_context_tokens == tier


@pytest.mark.parametrize("bad", [100, 150_000, 256_001, 400_000, 1_000_000, 0, -1])
def test_non_tier_value_fails_at_settings_construction(bad: int) -> None:
    """挡位外的值在构造期（= 启动期）响亮失败，不是拖到请求路径。"""
    with pytest.raises(ValidationError):
        _settings(max_context_tokens=bad)


def test_derived_thresholds_are_exact_decimal_integers_for_every_tier() -> None:
    """70%/85% 线在每个挡位上都是十进制整数（票面验收第一条）。

    机械判据：`w * 0.70` / `w * 0.85` 无小数余量，且与整数运算 `w*70//100` /
    `w*85//100` 一致——两条算法不得给出不同答案（否则「一律十进制整数」这条
    验收会随实现口径漂移）。
    """
    expected = {
        100_000: (70_000, 85_000),
        200_000: (140_000, 170_000),
        256_000: (179_200, 217_600),
        300_000: (210_000, 255_000),
        500_000: (350_000, 425_000),
    }
    assert set(expected) == set(CONTEXT_WINDOW_TIERS)
    for tier, (auto, hard) in expected.items():
        assert tier * 0.70 == auto and tier * 0.85 == hard
        assert int(tier * 0.70) == tier * 70 // 100 == auto
        assert int(tier * 0.85) == tier * 85 // 100 == hard


def test_builder_constructor_default_is_pinned_to_the_settings_default() -> None:
    """`builder.py:515` 的 ctor 默认 `200_000` 与 config 默认**机械钉住**。

    票面：「`:515` ctor 默认 `200_000` 与 config 默认构造期机械钉住；其余零改动」。
    两处字面量分列（config 是依赖图底层，builder 不该反向依赖它），相等靠本用例
    钉住——同 `memory_primary_provider` 与 `roles.MEMORY_PRIMARY_PROVIDER` 的先例。
    """
    import inspect

    default = (
        inspect.signature(ContextBuilder.__init__)
        .parameters["max_context_tokens"]
        .default
    )
    assert default == Settings(_env_file=None).max_context_tokens == 200_000
