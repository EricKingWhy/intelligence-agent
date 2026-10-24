"""#1539 跨包私有 import 守卫：生产代码不得 import 其他包的 `_` 开头符号。

## 它守什么

F2-1（#1539）清点并收敛了 12 个跨包私有符号消费（Runtime/Session/Context/
Storage/Web/Skills/Model 互知对方私有常量与函数）。本闸门把「零跨包私有
import」钉成机械判据：新增穿透在 CI 直接红，而不是等下一轮深挖报告。

## 判定口径

- 只扫 `src/agent_harness` 生产代码（tests 自己的 helper 不算）。
- 「包」= `agent_harness` 下的**一级子包**（agent/context/session/storage/
  web/model/skills/memory/...）；根级模块（`agent_harness.<file>.py`）算
  `(root)`，它 import 子包私有同样算穿透（mcp_precheck / plugin_inspection
  即此形状）。
- 触发 = `from agent_harness.<pkg>... import _name`（`__dunder__` 除外），且
  importer 所在包 ≠ 被导入包。相对 import 先解析成绝对再判。
- 别名不豁免：`from ... import _x as y` / `as _y` 同样穿透。
- 包**内部**跨模块的私有共享（如 `memory.v2` → `memory.extractor`）不在此列。
- 公开名局部别名（`from ... import lock as _lock`）不算：穿透判据看
  **被导入符号本体**，不是本地绑定的名字。

## 豁免

`_ALLOWED` 是**带证据的**白名单：每项必须注明票号与理由（票面 AC：
「未解决点如实留具体证据」）。当前为空——12 处全部收敛。
"""

from __future__ import annotations

import ast
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_SRC_ROOT = _ROOT / "src" / "agent_harness"

#: (importer 相对路径, 被导入目标模块的点分路径, 被导入符号) -> 证据说明。
#: 新增项必须附票号与理由；键绑定**目标模块**（如 `context` / `context.compactor`
#: / `(root)`），一条豁免只覆盖「从该模块导入该符号」，不外溢到其他模块的同名符号。
_ALLOWED: dict[tuple[str, str, str], str] = {}


def _package_of(rel_parts: tuple[str, ...]) -> str:
    """`('agent', 'runtime.py')` ⇒ `agent`；`('mcp_precheck.py',)` ⇒ `(root)`。"""
    if len(rel_parts) >= 2:
        return rel_parts[0]
    return "(root)"


def _cross_package_private_imports() -> list[str]:
    offenders: list[str] = []
    for path in sorted(_SRC_ROOT.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        rel = path.relative_to(_SRC_ROOT)
        rel_posix = "/".join(rel.parts)
        importer_pkg = _package_of(rel.parts)
        module_pkg_parts = ("agent_harness",) + rel.parts[:-1]
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom):
                continue
            base_parts: tuple[str, ...] = tuple(node.module.split(".")) if node.module else ()
            if node.level:  # 相对 import：按 importer 所在包上推成绝对
                base_parts = (
                    module_pkg_parts[: len(module_pkg_parts) - (node.level - 1)] + base_parts
                )
            if base_parts[:1] != ("agent_harness",):
                continue
            target_pkg = base_parts[1] if len(base_parts) >= 2 else "(root)"
            if target_pkg == importer_pkg:
                continue  # 包内跨模块共享不在此列
            for alias in node.names:
                name = alias.name
                if name.startswith("_") and not name.startswith("__"):
                    target_prefix = ".".join(base_parts[1:]) or "(root)"
                    if (rel_posix, target_prefix, name) in _ALLOWED:
                        continue
                    offenders.append(
                        f"src/agent_harness/{rel_posix}:{node.lineno}: "
                        f"from {'.'.join(base_parts)} import {name}"
                    )
    return offenders


def test_no_cross_package_private_imports() -> None:
    offenders = _cross_package_private_imports()
    assert not offenders, (
        "#1539 跨包私有 import 穿透（新增消费先建公开合同，或在 _ALLOWED 带票号证据豁免）：\n"
        + "\n".join(offenders)
    )
