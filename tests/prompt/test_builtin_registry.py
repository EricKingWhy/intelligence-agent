"""T3 默认注册表的完整性与启动自检。

区分 `build_registry()` 与 `DEFAULT_REGISTRY`：前者无参、确定性；后者是"按当前
环境构建的结果"（T5 起含 persona）。断言组成必须用前者。
"""

from __future__ import annotations

from agent_harness.prompt import DEFAULT_REGISTRY, run_self_check
from agent_harness.prompt.builtin import _declared_scopes, build_registry

_PROFILE_SCOPES = ["profile:coding", "profile:main", "profile:research_review"]


def test_builtin_registry_has_three_profile_sections() -> None:
    assert [s.name for s in build_registry().available()] == [
        "profile:coding:identity",
        "profile:main:identity",
        "profile:research_review:identity",
    ]


def test_builtin_registry_self_check_passed() -> None:
    """import 成功本身已证明自检通过；这里再做一次显式断言防回归。"""
    run_self_check(build_registry(), _PROFILE_SCOPES)


def test_builtin_registry_sections_sorted() -> None:
    sections = build_registry().available()
    keys = [(s.order, s.name) for s in sections]
    assert keys == sorted(keys)


def test_build_registry_is_deterministic() -> None:
    """两次构建结果逐字段相等——证明它不依赖 env / 全局可变状态。"""
    first = [(s.name, s.order, s.text) for s in build_registry().available()]
    second = [(s.name, s.order, s.text) for s in build_registry().available()]
    assert first == second


def test_default_registry_equals_build_registry_in_p0() -> None:
    """`DEFAULT_REGISTRY` 必须等于一次**全新确定性构建**——即它不读环境。

    交接文档 §4.4：`DEFAULT_REGISTRY = build_registry()`，**永不读环境变量**；
    persona 由装配点 `build_registry(persona=…)` 注入，不在默认注册表上变形。
    这条断言就是那条不变量的机器化表达：谁哪天把 env 派生的 section 塞进
    `DEFAULT_REGISTRY`，这里立刻红。
    """
    assert [(s.name, s.order, s.text) for s in DEFAULT_REGISTRY.available()] == [
        (s.name, s.order, s.text) for s in build_registry().available()
    ]


def test_declared_variables_is_empty_in_p0() -> None:
    assert build_registry().declared_variables() == frozenset()


def test_declared_scopes_excludes_wildcard_and_covers_all_profiles() -> None:
    """自检 scope 集 = 注册表里所有非 `*` 的 scope（T4 加 aux:* 时自动覆盖）。"""
    assert _declared_scopes(build_registry()) == _PROFILE_SCOPES
