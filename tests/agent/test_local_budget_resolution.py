"""`#308` T3：local turn fuse 的解析规则（默认 500 / alias / 只能收窄）。

判据来源：ADR-0044 D1/D8 与 `02 §5.1`——权威表在规格，本文件只钉实现是否照抄：

- 缺省 → 500（Deployment 默认）；
- `max_steps` 单独出现 ⇒ 解释为 local fuse（deprecated alias，来源可见）；
- 新字段单独出现 ⇒ 生效；
- 相等双字段 ⇒ 接受（且 alias 的存在仍然可见）；
- 不等双字段 ⇒ 422 类的领域拒绝；
- 越过生效上层 ceiling（Deployment / AgentProfile）⇒ 拒绝，不静默截断。

解析是**纯函数**：只回答"生效几轮、谁定的"，不持有计数器（计数点在 Agent Loop）。
"""

from __future__ import annotations

import logging

import pytest

from agent_harness.agent.budget import (
    DEFAULT_MAX_AGENT_TURNS,
    SOURCE_ALIAS,
    SOURCE_DEPLOYMENT,
    SOURCE_PROFILE,
    SOURCE_REQUEST,
    BudgetAliasConflict,
    BudgetCeilingExceeded,
    BudgetRejection,
    LocalFuse,
    resolve_local_fuse,
)
from agent_harness.session.errors import SessionServiceError


def test_default_is_the_contractual_500() -> None:
    """AC：所有缺省入口解析为同一个 effective local fuse 500。"""
    fuse = resolve_local_fuse()
    assert fuse.max_agent_turns == DEFAULT_MAX_AGENT_TURNS == 500
    assert fuse.source == SOURCE_DEPLOYMENT
    assert fuse.used_legacy_alias is False


def test_deployment_can_lower_the_default() -> None:
    """Deployment 是政策旋钮：可以下调，缺省请求随之收窄。"""
    fuse = resolve_local_fuse(deployment=120)
    assert fuse.max_agent_turns == 120
    assert fuse.source == SOURCE_DEPLOYMENT


def test_legacy_only_is_the_root_local_fuse() -> None:
    """只发 `max_steps` 的旧客户端 ⇒ 解释为根 AgentRuntime 的 local fuse。"""
    fuse = resolve_local_fuse(alias=7)
    assert fuse.max_agent_turns == 7
    assert fuse.source == SOURCE_ALIAS
    assert fuse.used_legacy_alias is True
    assert fuse.as_projection()["deprecation"] == {
        "field": "max_steps", "replacement": "budget.local.max_agent_turns",
    }


def test_new_field_only_wins_over_deployment_default() -> None:
    fuse = resolve_local_fuse(request=250)
    assert fuse.max_agent_turns == 250
    assert fuse.source == SOURCE_REQUEST
    assert fuse.used_legacy_alias is False
    assert "deprecation" not in fuse.as_projection()


def test_equal_double_fields_are_accepted_and_alias_stays_visible() -> None:
    """相等双字段接受；但"客户端还在发 deprecated 字段"这件事不许消失。"""
    fuse = resolve_local_fuse(request=30, alias=30)
    assert fuse.max_agent_turns == 30
    assert fuse.source == SOURCE_ALIAS
    assert fuse.used_legacy_alias is True


def test_conflicting_double_fields_are_rejected() -> None:
    with pytest.raises(BudgetAliasConflict) as excinfo:
        resolve_local_fuse(request=30, alias=20)
    message = str(excinfo.value)
    assert "max_steps=20" in message and "max_agent_turns=30" in message


def test_conflict_is_caught_before_the_ceiling_rule() -> None:
    """两个 422 判据同时成立时先报冲突（请求形状自身矛盾，与策略无关）。"""
    with pytest.raises(BudgetAliasConflict):
        resolve_local_fuse(deployment=10, request=30, alias=20)


@pytest.mark.parametrize("field", ["request", "alias"])
def test_request_above_deployment_ceiling_is_rejected(field: str) -> None:
    """R4：越过生效上层 ceiling ⇒ 拒绝（不是静默截断到 100）。"""
    with pytest.raises(BudgetCeilingExceeded) as excinfo:
        resolve_local_fuse(deployment=100, **{field: 200})
    assert "ceiling=100" in str(excinfo.value)


def test_request_equal_to_ceiling_is_accepted() -> None:
    """边界：等于 ceiling 是"收窄到上限"，不是越权。"""
    assert resolve_local_fuse(deployment=100, request=100).max_agent_turns == 100


def test_profile_narrows_below_deployment() -> None:
    fuse = resolve_local_fuse(deployment=500, profile=120)
    assert fuse.max_agent_turns == 120
    assert fuse.source == SOURCE_PROFILE


def test_profile_above_deployment_is_a_configuration_error() -> None:
    """档位声明大于 Deployment hard ceiling ⇒ 拒绝（不静默取小值）。"""
    with pytest.raises(BudgetCeilingExceeded):
        resolve_local_fuse(deployment=100, profile=500)


def test_request_above_profile_ceiling_is_rejected_even_below_deployment() -> None:
    """PRD 决策 3：请求不得越过 **AgentProfile 政策**（生效上层 = min(各层)）。"""
    with pytest.raises(BudgetCeilingExceeded):
        resolve_local_fuse(deployment=500, profile=120, request=200)
    assert resolve_local_fuse(
        deployment=500, profile=120, request=60,
    ).max_agent_turns == 60


def test_profile_none_means_inherit() -> None:
    """内置档位不声明数字（None = 继承）——出厂设定不该写死一个低位数字。"""
    assert resolve_local_fuse(profile=None, request=None).max_agent_turns == 500


@pytest.mark.parametrize(
    ("kwargs", "layer"),
    [
        ({"deployment": 0}, "deployment"),
        ({"request": -1}, "budget.local.max_agent_turns"),
        ({"alias": 0}, "max_steps"),
        ({"profile": -3}, "agent_profile"),
    ],
)
def test_non_positive_values_are_rejected(kwargs: dict, layer: str) -> None:
    """非正数不是"很小的预算"，是无意义配置——形状闸门（领域侧第二道）。"""
    with pytest.raises(BudgetRejection) as excinfo:
        resolve_local_fuse(**kwargs)
    assert layer in str(excinfo.value)


def test_bool_is_not_an_int_ceiling() -> None:
    """`True` 在 Python 里是 int——但它不是回合数（避免 `max_agent_turns=True`）。"""
    with pytest.raises(BudgetRejection):
        resolve_local_fuse(request=True)  # type: ignore[arg-type]


def test_alias_use_is_logged_as_a_deprecation_signal(caplog) -> None:
    """R5：旧客户端仅发 `max_steps` 时行为可预测**且**有 deprecation signal。"""
    with caplog.at_level(logging.WARNING, logger="agent_harness.agent.budget"):
        resolve_local_fuse(alias=5)
    assert any(
        "budget_local_fuse_alias_deprecated" in record.message
        for record in caplog.records
    )


def test_rejections_are_domain_errors_for_the_http_map() -> None:
    """两种拒绝都必须是 `SessionServiceError` 子类：422 映射走单一映射源。"""
    assert issubclass(BudgetAliasConflict, SessionServiceError)
    assert issubclass(BudgetCeilingExceeded, SessionServiceError)
    assert issubclass(BudgetRejection, SessionServiceError)


def test_projection_carries_only_effective_value_and_source() -> None:
    """投影刻意不含已消耗 counter——那是 RunBudget（#312）的数据面。"""
    assert LocalFuse(max_agent_turns=42, source=SOURCE_REQUEST).as_projection() == {
        "max_agent_turns": 42,
        "source": SOURCE_REQUEST,
    }


def test_settings_default_matches_contract() -> None:
    """Deployment 默认值必须与契约默认相等（`config.py` 的字面量有言在先：不 import）。

    这是那处分列的**唯一**防漂移闸门：若规格的 500 变了而 `Settings` 没跟上，
    所有缺省入口会解析出一个与契约不同的 local fuse，而且没有任何东西会报错。

    `_env_file=None`：断言的对象是**契约默认值**，不是这台机器的部署策略——读仓库 `.env`
    会让一个合法的 `LOCAL_MAX_AGENT_TURNS=200` 把这条闸门打红（那时红的是部署，不是契约），
    也会让单测去吃部署机的其它配置。
    """
    from agent_harness.config import Settings

    assert Settings(_env_file=None).local_max_agent_turns == DEFAULT_MAX_AGENT_TURNS


def test_settings_rejects_non_positive_ceiling() -> None:
    """非法 Deployment ceiling 在**构造期**失败（构造期即启动期，同 bash 预算的规矩）。"""
    from pydantic import ValidationError

    from agent_harness.config import Settings

    with pytest.raises(ValidationError):
        Settings(_env_file=None, local_max_agent_turns=0)


def test_declared_turn_ceiling_follows_the_profile_choice(monkeypatch) -> None:
    """根路径与 child 路径取同一个档位声明（`None` ⇒ 回落到 `main`）。"""
    import dataclasses

    from agent_harness.agent.profiles import BUILTIN_PROFILES, declared_turn_ceiling

    # 内置三档位都不写死数字（出厂设定不制造"档位 500 撞 deployment 100"的必然失败组合）。
    assert {name: declared_turn_ceiling(name) for name in BUILTIN_PROFILES} == {
        name: None for name in BUILTIN_PROFILES
    }
    # `None`（未指定档位）与显式 `main` 必须同值：`build_runtime` 就是这么落档的。
    assert declared_turn_ceiling(None) == declared_turn_ceiling("main")
    with pytest.raises(KeyError):
        declared_turn_ceiling("no-such-profile")
    # 声明数字时**值要回传**（上面三条只证明"内置档位都是 None"，对"真的读到了声明值"
    # 没有鉴别力——web 用例是靠 monkeypatch 档位才间接覆盖到这一点的）。
    monkeypatch.setitem(
        BUILTIN_PROFILES, "coding",
        dataclasses.replace(BUILTIN_PROFILES["coding"], max_agent_turns=40),
    )
    assert declared_turn_ceiling("coding") == 40
    assert resolve_local_fuse(
        deployment=DEFAULT_MAX_AGENT_TURNS, profile=declared_turn_ceiling("coding"),
    ).as_projection() == {"max_agent_turns": 40, "source": SOURCE_PROFILE}
