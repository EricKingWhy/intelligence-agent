"""共用来源/快照原语的中性归属（issue #1083）。

两件事要用机械判据钉死：

1. MCP 包生命周期**不再**从 `agent_harness.skills.package_manager` 导入共用的私有
   来源/安全函数——它们搬到了中性顶层模块 `agent_harness.package_source`。
2. Skill 安装器与 MCP 包生命周期绑定的是**同一个**实现对象（不是各抄一份）：共用的
   安全校验只在 `package_source` 里存在一份，边界只是错误类型适配。
3. 中性模块 `agent_harness.package_source` 与 MCP 车道**都不再**触碰 Skill 内部模块
   （`skills.package_manager` / `skills.inspection`）的私有符号，也不以命名空间
   import 绑定它们——中性化彻底，不留 `skills.inspection` 这类残留依赖（issue #1083 P3）。

**已知局限**（P4，Correctness）：守卫是纯 AST 语法层判定，覆盖 `from ... import _x`
与 `import ... [as ns]` 两种形状；对 `ns._x` 这类**属性访问**不做符号/类型解析，故
不判定其是否真取到私有符号（见 `_namespace_imports` docstring），留待代码评审判定。
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
_PACKAGE_SOURCE = _ROOT / "src" / "agent_harness" / "package_source.py"

#: 中性层与 MCP 车道**都不得**触碰的 Skill 内部模块（issue #1083）：既不许取它们的
#: 下划线私有符号，也不许以命名空间 import（`import ... [as ns]`）绑定它们——绑定后
#: `ns._foo` 就能绕过所有权边界。`skills.package_manager` 的**公开**符号
#: （`SkillPackageError` / `SkillPackageManager`）MCP 仍可正当地 `from ... import`。
_FORBIDDEN_SKILL_MODULES = frozenset(
    {
        "agent_harness.skills.package_manager",
        "agent_harness.skills.inspection",
    }
)

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


def _namespace_imports(text: str) -> set[str]:
    """`import a.b [as c]` 形式的模块点分名（P4，Correctness）。

    旧守卫只收集 `ast.ImportFrom`，`import agent_harness.skills.inspection as insp`
    绑定后 `insp._foo` 即可触达私有符号而不被判红。这里把 `ast.Import` 也纳入，
    凡命名空间 import 命中禁列模块即算违规。

    **已知局限**：本判定是纯语法层，不解析属性访问——`ns._foo` 具体取哪个符号、
    是否真为私有，需类型/符号解析才能定论，本守卫不做（留待代码评审兜底）。
    """
    modules: set[str] = set()
    for node in ast.walk(ast.parse(text)):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
    return modules


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


def test_neutral_layer_and_mcp_never_reach_into_skill_internal_modules() -> None:
    """中性模块与 MCP 车道都不触碰 `skills.package_manager` / `skills.inspection` 私有面。

    覆盖两种 import 形状（issue #1083 P3 守卫扩展 + P4 Correctness）：
    ① `from <skill-module> import _x`——任何下划线私有符号零命中；
    ② `import <skill-module> [as ns]`——命名空间 import 一律判红（可 `ns._x` 绕过）。
    """
    for path in (_PACKAGE_SOURCE, _MCP_SOURCE):
        text = path.read_text(encoding="utf-8")
        relative = path.relative_to(_ROOT)
        for module in _FORBIDDEN_SKILL_MODULES:
            private = sorted(
                name for name in _imported_from(text, module) if name.startswith("_")
            )
            assert not private, (
                f"{relative} 从 {module} 导入私有符号，中性化不彻底（issue #1083）：{private}"
            )
            assert module not in _namespace_imports(text), (
                f"{relative} 以命名空间 import 绑定 {module}，可经属性访问触达其私有符号"
                f"（issue #1083）：应改为 from ... import 具体公开符号"
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
