"""场景注册表（`#307` Must Do「为后续 T3/T4/T7/T9/T10 预留场景注册入口」）。

注册表的全部价值是"后续票能按 id 接进来且**不会静默覆盖**"：重复 id 抛错、未知名抛错、
内置注册是显式且幂等的（不在 import 时发生，`--list` 的输出不随 import 图漂移）。
"""

from __future__ import annotations

import pytest

from evaluation.live_gate.registry import (
    LiveScenario,
    get_scenario,
    is_builtin,
    list_scenarios,
    register_scenario,
    unregister_scenario,
)
from evaluation.live_gate.scenarios import BUILTIN_SCENARIOS, register_builtin_scenarios

SMOKE_ID = "smoke-production-tools"


class _StubScenario:
    """最小合法场景（协议只有 id / version / description + 两个方法）。"""

    id = "stub-scenario"
    version = 1
    description = "机制测试用替身"

    async def prepare(self, ctx):
        return []

    async def run(self, ctx):
        raise AssertionError("替身不该被调用")


@pytest.fixture
def clean_registry():
    """测完把替身摘掉：注册表是进程级单例，别污染同一个 pytest 会话里的其他用例。"""
    yield
    unregister_scenario(_StubScenario.id)


def test_register_get_list_round_trip(clean_registry) -> None:
    scenario = register_scenario(_StubScenario())
    assert get_scenario(_StubScenario.id) is scenario
    assert isinstance(scenario, LiveScenario)
    assert _StubScenario.id in [item.id for item in list_scenarios()]


def test_duplicate_id_is_rejected(clean_registry) -> None:
    register_scenario(_StubScenario())
    with pytest.raises(ValueError, match="已注册"):
        register_scenario(_StubScenario())


def test_empty_id_is_rejected() -> None:
    class _NoId:
        id = ""
        version = 1
        description = ""

    with pytest.raises(ValueError, match="非空 id"):
        register_scenario(_NoId())


def test_unknown_scenario_raises_keyerror() -> None:
    with pytest.raises(KeyError, match="未知场景"):
        get_scenario("no-such-scenario")


def test_unregister_is_test_only_removal(clean_registry) -> None:
    register_scenario(_StubScenario())
    unregister_scenario(_StubScenario.id)
    with pytest.raises(KeyError):
        get_scenario(_StubScenario.id)


def test_builtin_registration_is_explicit_and_idempotent() -> None:
    """两次调用只注册一次（幂等）；列表里不会出现两份内置场景。

    内置清单会随票增长（T2 smoke → T3 长任务 → …），所以断言钉的是**幂等**与**可解析**，
    不是"只有一个"：把条数写死会让每张加场景的票都得改这里，而那一天真正的回归
    （同 id 注册两次）反而没人测。
    """
    register_builtin_scenarios()
    register_builtin_scenarios()
    smoke = get_scenario(SMOKE_ID)
    assert isinstance(smoke, LiveScenario)
    # 场景版本随票增长（T2 v1 → T5 v2 …），钉的是"版本是个正整数"而不是某个具体值：
    # 写死具体值会让每张改场景的票都得改这里，而真正的回归（版本没写 / 写成 0 或字符串）
    # 照样能被抓住。
    assert isinstance(smoke.version, int) and smoke.version >= 1
    declared = [item.id for item in BUILTIN_SCENARIOS]
    assert SMOKE_ID in declared
    assert len(declared) == len(set(declared)), f"内置清单里有重复 id：{declared}"
    for scenario_id in declared:
        assert is_builtin(scenario_id) is True, f"{scenario_id} 随 harness 发布，要能被 runner 认出"
        assert get_scenario(scenario_id) is not None
        assert [item.id for item in list_scenarios()].count(scenario_id) == 1


def test_stub_cannot_declare_itself_builtin(clean_registry) -> None:
    """P1：`builtin=True` 是**申请核实**，不是声明 —— 定义文件不在 `scenarios/` 下就注册不上。

    否则任何能 `import` 到注册表的代码都能把一个替身场景标成内置，写出一份
    `PASS / seams={}` 的证据（注册表是进程级可写单例，这正是攻击面）。
    """
    with pytest.raises(ValueError, match="不得声明为内置"):
        register_scenario(_StubScenario(), builtin=True)
    with pytest.raises(KeyError):
        get_scenario(_StubScenario.id)


def test_registered_stub_is_not_builtin(clean_registry) -> None:
    register_builtin_scenarios()
    register_scenario(_StubScenario())
    assert is_builtin(_StubScenario.id) is False
    unregister_scenario(_StubScenario.id)
    assert is_builtin(SMOKE_ID) is True, "摘掉替身不该动到内置集合"
