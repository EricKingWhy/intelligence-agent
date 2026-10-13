"""共用来源/快照原语的中性归属（issue #1083）。

两件事要用机械判据钉死：

1. MCP 包生命周期**不再**从 `agent_harness.skills.package_manager` 导入共用的私有
   来源/安全函数——它们搬到了中性顶层模块 `agent_harness.package_source`。
2. Skill 安装器与 MCP 包生命周期绑定的是**同一个**实现对象（不是各抄一份）：共用的
   安全校验只在 `package_source` 里存在一份，边界只是错误类型适配。
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

from agent_harness import package_source
from agent_harness.plugins import mcp_package_manager
from agent_harness.skills import package_manager

_ROOT = Path(__file__).resolve().parents[2]
_MCP_SOURCE = _ROOT / "src" / "agent_harness" / "plugins" / "mcp_package_manager.py"

#: 迁移到 `agent_harness.package_source` 的共用私有来源/安全函数/常量（issue #1083）。
_SHARED_PRIVATE = frozenset(
    {
        "_COMMIT_PATTERN",
        "_DIGEST_PATTERN",
        "_fetch_git_skill",
        "_git_tree_sha256",
        "_is_reparse_point",
        "_reject_junctions",
        "_tree_sha256",
        "_validate_git_ref",
        "_validate_git_subdirectory",
        "_validate_git_url",
    }
)

#: (管理模块, 该模块暴露的私有名, 中性模块里的实现)。适配器用 `functools.wraps` 包裹，
#: 故 `__wrapped__` 指向中性实现；不抛错的直接绑定（`is_reparse_point`）则 `is` 成立。
_SHARED_BINDINGS = (
    ("_tree_sha256", package_source.tree_sha256),
    ("_git_tree_sha256", package_source.git_tree_sha256),
    ("_validate_git_url", package_source.validate_git_url),
    ("_validate_git_ref", package_source.validate_git_ref),
    ("_validate_git_subdirectory", package_source.validate_git_subdirectory),
    ("_fetch_git_skill", package_source.fetch_git_source),
    ("_reject_junctions", package_source.reject_junctions),
    ("_is_reparse_point", package_source.is_reparse_point),
)


def _imported_from(text: str, module: str) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(ast.parse(text)):
        if isinstance(node, ast.ImportFrom) and node.module == module:
            names.update(alias.name for alias in node.names)
    return names


def test_mcp_does_not_import_shared_source_primitives_from_skill_manager() -> None:
    """MCP 不得再从 `skills.package_manager` 导入共用的私有来源/安全符号。"""
    imported = _imported_from(
        _MCP_SOURCE.read_text(encoding="utf-8"), "agent_harness.skills.package_manager"
    )
    leaked = sorted(_SHARED_PRIVATE & imported)
    assert not leaked, (
        "MCP 仍从 skills.package_manager 导入共用私有来源/安全函数，所有权未中立化"
        f"（issue #1083）：{leaked}"
    )


def test_mcp_imports_the_neutral_source_module() -> None:
    text = _MCP_SOURCE.read_text(encoding="utf-8")
    assert _imported_from(text, "agent_harness") >= {"package_source"}, (
        "MCP 应改为从中性顶层模块 agent_harness.package_source 取共用来源原语"
    )


def test_both_managers_bind_the_same_source_primitive_implementation() -> None:
    """两车道绑定同一实现：共用校验只有一份，边界只做错误类型适配。"""
    for manager in (package_manager, mcp_package_manager):
        for name, implementation in _SHARED_BINDINGS:
            bound: Any = getattr(manager, name)
            underlying = getattr(bound, "__wrapped__", bound)
            assert underlying is implementation, (
                f"{manager.__name__}.{name} 不是 agent_harness.package_source."
                f"{implementation.__name__} 的同一实现（疑似各抄一份）"
            )
