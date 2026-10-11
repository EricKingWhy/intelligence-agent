"""导出面卫生（#939 M-30 包根 + #988 F-1 子模块）。

**包根那一条（M-30）**：包根 `__all__` 里不许有**零消费者**的名字。审计（MM A-F23 复核）
把票面的"4 组"纠正为"真正零消费者的只有 `KIND_IMAGE` / `AttachmentErrorCode`"——两个都只被
`__init__.py` 自己再导出一次，真身在 `projection.py` / `errors.py`，任何调用方都能直接
import 到。本用例把这条规则机械化：**对包根每一种导出名做全仓消费者计数**，零消费者即红。

**子模块那一条（#988 F-1）**：包根有专用守卫，子模块 `__all__` 此前**零覆盖**——变异实测
（只往 `attachments/types.py::__all__` 加一个死名）全门禁照绿。规则②（声明的名字必须
**定义在本模块**，或有**经本模块路径**的外部消费者）就是为"再导出面只增不减"这条病写的
机械判据。

为什么它值得机械守卫而不是"删完就完了"：这类残留的复发成本极低（加新符号时顺手再导出
一次），代价是"读代码的人以为它是公开 API，于是照着用"——导出面因此只增不减。
"""

from __future__ import annotations

import ast
import importlib
from pathlib import Path

import agent_harness.attachments as attachments_pkg

_REPO = Path(__file__).resolve().parents[2]
_SCAN_DIRS = ("src", "tests", "scripts")
_PKG_DIR = _REPO / "src" / "agent_harness" / "attachments"
_PKG = "agent_harness.attachments"
_INIT = _PKG_DIR / "__init__.py"
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


# --------------------------------------------------------------------------
# #988 F-1：上一条只扫**包根**，子模块 `__all__` 是守卫盲区。
#
# 第一性原理：`__all__` 的**唯一**作用是声明"本模块 `import *` 时导出哪些名字"，因此它
# 声明的名字必须满足"要么本来就在本模块里、要么真的有人从本模块取用"——否则它是一条
# **没有出口也没有入口的声明**。这类声明只增不减：删掉一个再导出对调用方零可见（因为没人
# 用），于是没人会去删；而每留一条，读代码的人就多一次"这看起来像公开 API"的误判。
# 守卫的价值不在"发现今天的死名"，而在**让明天加死名的动作当场变红**。
#
# 机械规则（模块 S、名字 N ∈ S.__all__）：
#   ① `hasattr(S, N)` —— `import *` 一致性；防"加了个根本不存在的名字"型变异；
#   ② N **定义在 S**（模块级 def/class/赋值；`from … import` 进来的不算定义），
#      **或** N 有**经 S 路径**的消费者（`from agent_harness.attachments.S import N`
#      / 绑定别名 `<alias>.N`，alias ∈ {S 短名}；排除 S 自身文件）。
#
# 规则②的边界（本守卫**不**管什么）：它钉的是**声明面**——`__all__` 里出现、却既非本
# 模块定义、又无人经本模块取用的名字。**模块自有定义是否为死代码是另一回事**，属
# ruff / 代码审查；本守卫不对自有定义做消费者计数（否则会把"定义在本模块但暂时无人用"
# 的合法符号判红，与包根那条的语义混成一团）。
# --------------------------------------------------------------------------

_SUBMODULE_ALIAS_EXTRA = {
    # `tests/attachments/test_import_direction.py` 用 `X_module` 形式绑定子模块做同一性断言。
    "types": ("types_module", "types_mod"),
    "validation": ("validation_module", "validation_mod"),
}


def _exporting_submodules() -> list[tuple[str, Path]]:
    """`attachments/` 下**每一个**定义了模块级 `__all__` 的子模块（`__init__` 除外）。

    覆盖面按目录枚举而不是硬编码文件名——新增一个带 `__all__` 的子模块会自动进入守卫
    范围，不需要有人记得来加一行。
    """
    found: list[tuple[str, Path]] = []
    for path in sorted(_PKG_DIR.glob("*.py")):
        if path.name == "__init__.py":
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:  # pragma: no cover - 语法坏掉由 ruff/import 先报
            continue
        assigns_all = any(
            isinstance(node, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == "__all__" for t in node.targets)
            for node in tree.body
        )
        if assigns_all:
            found.append((path.stem, path))
    return found


def _bound_names(target: ast.expr) -> set[str]:
    """赋值 / 解包目标的绑定名（`a, b = …` / `[a, *b] = …`）。"""
    if isinstance(target, ast.Name):
        return {target.id}
    if isinstance(target, (ast.Tuple, ast.List)):
        names: set[str] = set()
        for element in target.elts:
            names.update(_bound_names(element))
        return names
    if isinstance(target, ast.Starred):
        return _bound_names(target.value)
    return set()


def _module_level_defined_names(path: Path) -> set[str]:
    """模块级**定义**的名字：def / class / 赋值 / 注解赋值 / `for` 与 `with` 的绑定。

    刻意**不含** `import` / `from … import` 绑定的名字——那正是本规则要区分开的
    "借来的名字"（`types.py` 从 `validation` import 进来的四个就属这类）。
    """
    defined: set[str] = set()
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            defined.add(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                defined.update(_bound_names(target))
        elif isinstance(node, (ast.AnnAssign, ast.For, ast.AsyncFor)):
            defined.update(_bound_names(node.target))
        elif isinstance(node, (ast.With, ast.AsyncWith)):
            for item in node.items:
                if item.optional_vars is not None:
                    defined.update(_bound_names(item.optional_vars))
    return defined


def _submodule_consumers(submodule: str) -> dict[str, list[str]]:
    """谁在消费子模块 `S` 的导出：`from agent_harness.attachments.S import X` / `<alias>.X`。

    与包根那条同口径：**必须是经 S 路径取的**。定义模块自身对符号的使用**不算**消费者
    ——否则每个再导出名都会因为它在自己模块里被用而"合格"，守卫立刻退化成恒真。
    """
    module_path = _PKG_DIR / f"{submodule}.py"
    full_module = f"{_PKG}.{submodule}"
    aliases = {submodule, *_SUBMODULE_ALIAS_EXTRA.get(submodule, ())}
    consumers: dict[str, list[str]] = {}
    for base in _SCAN_DIRS:
        for path in (_REPO / base).rglob("*.py"):
            if path == module_path:
                continue
            source = path.read_text(encoding="utf-8", errors="replace")
            # 预筛：只有提到本子模块的模块才值得 AST 解析（`attachments.types` 是
            # `agent_harness.attachments.types` / `agent_harness.agent.types` 的子串，
            # 放宽不产生假绿，只多解析几个文件）。
            if full_module not in source:
                continue
            try:
                tree = ast.parse(source)
            except SyntaxError:
                continue
            rel = path.relative_to(_REPO)
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.ImportFrom)
                    and node.module == full_module
                    and node.level == 0
                ):
                    for alias in node.names:
                        consumers.setdefault(alias.name, []).append(f"{rel}:{node.lineno}")
                elif (
                    isinstance(node, ast.Attribute)
                    and isinstance(node.value, ast.Name)
                    and node.value.id in aliases
                ):
                    consumers.setdefault(node.attr, []).append(f"{rel}:{node.lineno}")
    return consumers


def _covered_submodules() -> list[tuple[str, Path]]:
    """覆盖面自证后的子模块枚举（规则①/②共用；两条各自断言，故失败面不混）。"""
    submodules = _exporting_submodules()
    names = {name for name, _ in submodules}
    assert {"types", "validation"} <= names, (
        f"子模块枚举漏掉了票面点名的模块：得到 {sorted(names)}；"
        "覆盖面无声明地缩小 = 守卫静默失效"
    )
    return submodules


def test_every_submodule_export_name_exists_on_the_module() -> None:
    """规则①：`__all__` 声明的名字必须真的在模块上（`hasattr`）——`import *` 不炸。

    单独一条（而不是并进规则②）是为了让两类失败的**失败面不同**：`__all__` 里加一个
    根本不存在的名字死在**本条**，加一个"借来的名字"死在规则②那条（协议 §8.3 第 4 条
    要求变异覆盖不同检查点且失败集合不同；合成一条会让两种鉴别力互相冒充）。
    """
    orphans: list[str] = []
    for submodule, _ in _covered_submodules():
        module = importlib.import_module(f"{_PKG}.{submodule}")
        orphans.extend(
            f"{submodule}.__all__ 列了模块上不存在的名字：{name!r}"
            for name in module.__all__
            if not hasattr(module, name)
        )
    assert orphans == [], (
        "子模块 `__all__` 列了模块上不存在的名字（`import *` 会当场 AttributeError）：\n  - "
        + "\n  - ".join(orphans)
        + "\n要导出就得先在本模块定义它（或真的 import 进来）——不要写一条没有指涉的声明。"
    )


def test_every_submodule_export_is_defined_here_or_consumed_via_here() -> None:
    """规则②：每名要么**定义在本模块**、要么有**经本模块**的消费者（#988 F-1 的核心判据）。"""
    violations: list[str] = []
    for submodule, path in _covered_submodules():
        module = importlib.import_module(f"{_PKG}.{submodule}")
        defined = _module_level_defined_names(path)
        consumed = _submodule_consumers(submodule)
        for name in module.__all__:
            if name in defined or name in consumed:
                continue
            violations.append(
                f"{submodule}.__all__ 里的 {name!r} 既非本模块定义、也无人经 "
                f"`{_PKG}.{submodule}` 取用（经本模块的消费者：{sorted(consumed) or '无'}）"
            )
    assert violations == [], (
        "子模块导出面有「借来的名字」（#988 F-1）：\n  - "
        + "\n  - ".join(violations)
        + "\n真身若在别的模块，就不要在本模块 `__all__` 里声明它——调用方直接 import 真身；"
        "确实要给外部用的再导出，请让调用方**经本模块**取用（`from …S import N`）。"
    )


_TYPES_FORBIDDEN_REEXPORTS = (
    "EXTENSION_MEDIA_TYPES",
    "SUPPORTED_IMAGE_MEDIA_TYPES",
    "ImageMediaType",
    "parse_allowed_media_types",
)


def test_types_no_longer_reexports_validation_names() -> None:
    """#988 F-2 收窄守卫：`attachments.types` 不得再持有/声明 `validation` 的真身名。

    判据钉的是**病**（宽再导出面），不是"自有定义集合不许变"——所以它只针对这四个名字，
    将来给 `types` 加一个真正自有定义的新契约不受影响。

    三个方向都要成立，缺一个都不算收窄：
    ① `__all__` 里没有它们（声明面）；
    ② **属性面上也没有**（`EXTENSION_MEDIA_TYPES` / `SUPPORTED_IMAGE_MEDIA_TYPES` /
       `ImageMediaType` 连 import 都删了 ⇒ `hasattr` 为假）。只钉 `__all__` 会漏掉
       "import 回来了但不进 `__all__`"的中间态；
    ③ `parse_allowed_media_types` **例外**：它是 `resolve_image_limits` 的内部调用，
       属性必须仍在（删了就把功能删了）——收窄的是导出面，不是能力。
    另钉真身仍在 `validation`，让"收窄"有下界（不是把名字删没了）。
    """
    import agent_harness.attachments.types as types_module
    import agent_harness.attachments.validation as validation_module

    for name in _TYPES_FORBIDDEN_REEXPORTS:
        assert name not in types_module.__all__, (
            f"`attachments.types.__all__` 又声明了 {name!r}——真身在中性模块 "
            "`attachments.validation`（#988 F-2 收窄了宽再导出面）；"
            "需要它的调用方直接 import 真身。"
        )
        assert hasattr(validation_module, name), (
            f"{name!r} 在 `attachments.validation` 上不存在了——收窄导出面不等于删真身"
        )
    for name in ("EXTENSION_MEDIA_TYPES", "SUPPORTED_IMAGE_MEDIA_TYPES", "ImageMediaType"):
        assert not hasattr(types_module, name), (
            f"`attachments.types` 上仍能取到 {name!r}——它的 import 没删干净；"
            "这三个名字在 `types` 里连属性都不该存在（#988 F-2）"
        )
    assert hasattr(types_module, "parse_allowed_media_types"), (
        "`attachments.types.parse_allowed_media_types` 不见了——它是 `resolve_image_limits` "
        "的内部调用，必须保留（收窄的是再导出面，不是能力）"
    )
