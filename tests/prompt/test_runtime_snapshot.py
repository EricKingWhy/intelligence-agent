"""T7 运行时上下文快照的注册表侧（ADR-0023 D8）。

覆盖：section 的 target/order/scope、五个变量的自动推导、渲染落 meta_user、
缺变量响亮失败、以及**scope 隔离**（快照不进 profile、不进 aux、也不被 `*` 命中）。
"""

from __future__ import annotations

import pytest

from agent_harness.prompt import (
    SECTION_ORDERS,
    PromptError,
    Target,
    build_registry,
    run_self_check,
)
from agent_harness.prompt.builtin import _declared_scopes

SCOPE = "runtime:context_snapshot"
VARIABLES = {"cwd", "os", "date", "model", "tools"}
VALUES = {
    "cwd": "/tmp/proj",
    "os": "Windows 11",
    "date": "2026-09-12",
    "model": "deepseek-chat",
    "tools": "read, write",
}


def _section():
    sections = [s for s in build_registry().available() if s.name == SCOPE]
    assert len(sections) == 1, f"应有且仅有一条 {SCOPE} section，实际 {len(sections)}"
    return sections[0]


def test_snapshot_section_is_meta_user() -> None:
    section = _section()
    assert section.target is Target.META_USER
    assert section.order == SECTION_ORDERS["runtime:context_snapshot"] == 9500
    assert section.scopes == frozenset({SCOPE})  # 非 `*`
    assert "*" not in section.scopes


def test_snapshot_requires_five_variables() -> None:
    """`requires` 从正文自动推导，五个变量一个不漏。"""
    assert _section().requires == frozenset(VARIABLES)


def test_snapshot_renders_into_meta_user_text() -> None:
    assembled = build_registry().assemble(SCOPE, dict(VALUES))
    for value in VALUES.values():
        assert value in assembled.meta_user_text
    assert assembled.system_text == ""  # 快照不落 system-role


def test_snapshot_missing_variable_raises() -> None:
    """少传一个变量 → 响亮失败，不静默渲染成空串。"""
    incomplete = {k: v for k, v in VALUES.items() if k != "tools"}
    with pytest.raises(PromptError) as excinfo:
        build_registry().assemble(SCOPE, incomplete)
    assert excinfo.value.code == "missing_variable"


def test_snapshot_not_in_profile_prompt() -> None:
    assembled = build_registry().assemble("profile:main")
    assert "运行时事实" not in assembled.system_text
    assert assembled.meta_user_text == ""


def test_snapshot_not_in_aux_prompt() -> None:
    for scope in ("aux:compaction", "aux:memory_extraction", "aux:fork_tail"):
        assembled = build_registry().assemble(
            scope, {"tail_text": "T"} if scope == "aux:fork_tail" else None,
        )
        assert "运行时事实" not in assembled.system_text
        assert "运行时事实" not in assembled.meta_user_text


def test_snapshot_does_not_receive_wildcard_sections() -> None:
    """`*` 只匹配 `profile:<name>` → persona 前后缀不得进入快照 scope。"""
    from agent_harness.prompt import PersonaConfig

    with_persona = build_registry(PersonaConfig(prefix="P", suffix="S"))
    assert (with_persona.assemble(SCOPE, dict(VALUES)).meta_user_text
            == build_registry().assemble(SCOPE, dict(VALUES)).meta_user_text)


def test_snapshot_self_checked() -> None:
    """自检覆盖所有非 `*` scope（含变量 scope 自动填空串占位）。"""
    registry = build_registry()
    run_self_check(registry, _declared_scopes(registry))
    assert SCOPE in _declared_scopes(registry)


def test_snapshot_has_no_trailing_junk() -> None:
    text = build_registry().assemble(SCOPE, dict(VALUES)).meta_user_text
    assert not text.endswith("\n")
    assert "\n\n" not in text  # 单行正文，无多余空段
    assert text.startswith("以下是本次运行的运行时事实")
