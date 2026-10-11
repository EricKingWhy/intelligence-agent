"""导出面卫生：包根 `__all__` 里不许有**零消费者**的名字（#939 M-30）。

审计（MM A-F23 复核）把票面的"4 组"纠正为"真正零消费者的只有 `KIND_IMAGE` /
`AttachmentErrorCode`"——两个都只被 `__init__.py` 自己再导出一次，真身在
`projection.py` / `errors.py`，任何调用方都能直接 import 到。本用例把这条规则机械化：
**对包根每一种导出名做全仓消费者计数**，零消费者即红。

为什么它是规则的机械形态而不是"删完就完了"：这类残留的复发成本极低（加一个新符号时
顺手 `__init__` 再导出一次），而残留的代价是"读代码的人以为包根是公开 API，于是照着
用"——导出面因此只增不减。本用例让"加了一个没人用的再导出"当场变红。
"""

from __future__ import annotations

import ast
from pathlib import Path

import agent_harness.attachments as attachments_pkg

_REPO = Path(__file__).resolve().parents[2]
_SCAN_DIRS = ("src", "tests", "scripts")
_INIT = _REPO / "src" / "agent_harness" / "attachments" / "__init__.py"
#: `import agent_harness.attachments as <alias>` 的常见别名（属性访问式消费）。
_PKG_ALIASES = frozenset({"attachments", "attachments_pkg"})


def _root_export_consumers() -> dict[str, list[str]]:
    """谁在消费**包根**的导出：`from agent_harness.attachments import X` / `pkg.X`。

    口径必须是"从包根拿"，不是"文件里出现过这个名字"——后者会把定义模块里对
    自身符号的使用算成消费者（假绿）。这正是 `AttachmentErrorCode` 的形态：
    它被 `errors.py` 自己用作注解，但从没有谁从包根 import 过它。

    扫描前先用字符串预筛（只有提到本包的模块才值得 AST 解析）。
    """
    consumers: dict[str, list[str]] = {}
    for base in _SCAN_DIRS:
        for path in (_REPO / base).rglob("*.py"):
            if path == _INIT:
                continue
            source = path.read_text(encoding="utf-8", errors="replace")
            if (
                "agent_harness.attachments" not in source
                and "from agent_harness import attachments" not in source
            ):
                continue
            try:
                tree = ast.parse(source)
            except SyntaxError:
                continue
            rel = path.relative_to(_REPO)
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.ImportFrom)
                    and node.module == "agent_harness.attachments"
                    and node.level == 0
                ):
                    for alias in node.names:
                        consumers.setdefault(alias.name, []).append(f"{rel}:{node.lineno}")
                elif (
                    isinstance(node, ast.Attribute)
                    and isinstance(node.value, ast.Name)
                    and node.value.id in _PKG_ALIASES
                ):
                    consumers.setdefault(node.attr, []).append(f"{rel}:{node.lineno}")
    return consumers


def test_every_root_export_has_a_root_consumer() -> None:
    consumers = _root_export_consumers()
    dead = [name for name in attachments_pkg.__all__ if name not in consumers]
    assert dead == [], (
        f"包根 `__all__` 里有零消费者的导出：{sorted(dead)}\n"
        "它们的真身在子模块里（本用例不反对直接 import 子模块）；包根再导出只会让人"
        "以为它是公开 API。删掉这条再导出（#939 M-30），或让调用方从包根真的用上它"
    )


def test_root_all_star_matches_module_attributes() -> None:
    """`__all__` 与模块属性一致（`import *` 不炸、也不静默漏名）。"""
    missing = [name for name in attachments_pkg.__all__ if not hasattr(attachments_pkg, name)]
    assert missing == [], f"`__all__` 列了模块上不存在的名字：{missing}"
