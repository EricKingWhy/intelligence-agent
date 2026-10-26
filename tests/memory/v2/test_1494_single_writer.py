"""#1494 AC5：当前内容的写者只有一处——静态图 + 真实入口的运行时配对。

两条独立证据：

1. **静态**：`src/agent_harness` 的 AST 里，`mutate_now` 的调用点只出现在
   `commit.py`；没有任何模块直接调用 `_write_file` 或直接给 `memory_entries` /
   `user_entries` 赋值；`MarkdownMemoryStore` 不再暴露 add/replace/remove 旧写面。
2. **运行时**：把引擎的落盘函数包一层，跑完 service / 工具 / 审批 / 被拦放行 /
   formation 全部入口后，**每一次落盘的 (target, digest) 都能在某条 COMMITTED 回执的
   `after_digest` 里找到**——即没有任何一次内容变更绕过回执（因此也绕过 commit）。

静态只能证明"当前代码没有第二条路"，运行时只能证明"跑过的路径都配对了回执"；
两者一起才排除了"静态漏看某个动态分发"和"图上干净但实际旁路"两种失败。
"""

from __future__ import annotations

import ast
import json
from collections import Counter
from pathlib import Path

import pytest

from agent_harness.config import Settings
from agent_harness.memory.v2 import assembly
from agent_harness.memory.v2.markdown_store import (
    MarkdownMemoryStore,
    entries_digest,
    entry_digest,
)
from agent_harness.memory.v2.memory_tool import MemoryTool, _MemoryArgs
from agent_harness.memory.v2.types import TrustedMemoryIdentity
from agent_harness.memory.v2.write_approval import _identity
from tests.memory.v2._records import make_draft, make_record

REPO_ROOT = Path(__file__).resolve().parents[3]
SOURCE_ROOT = REPO_ROOT / "src" / "agent_harness"
LOCAL = TrustedMemoryIdentity(tenant_id="local", user_id="local")


# --------------------------------------------------------------------------------------
# 静态图
# --------------------------------------------------------------------------------------


def _call_sites(method: str) -> dict[str, list[int]]:
    found: dict[str, list[int]] = {}
    for path in sorted(SOURCE_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == method
            ):
                found.setdefault(str(path.relative_to(REPO_ROOT)).replace("\\", "/"), []).append(
                    node.lineno)
    return found


def _entry_assignments() -> set[str]:
    """给活条目赋值的模块：直接赋值、增量赋值、注解赋值、以及 ``setattr`` 动态写。"""
    assigned: set[str] = set()
    for path in sorted(SOURCE_ROOT.rglob("*.py")):
        relative = str(path.relative_to(REPO_ROOT)).replace("\\", "/")
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            targets: list[ast.expr] = []
            if isinstance(node, ast.Assign):
                targets = list(node.targets)
            elif isinstance(node, (ast.AugAssign, ast.AnnAssign)):
                targets = [node.target]
            for target in targets:
                if (
                    isinstance(target, ast.Attribute)
                    and target.attr in {"memory_entries", "user_entries"}
                ):
                    assigned.add(relative)
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "setattr"
                and len(node.args) >= 2
                and isinstance(node.args[1], ast.Constant)
                and str(node.args[1].value) in {"memory_entries", "user_entries"}
            ):
                assigned.add(relative)
    return assigned


def test_only_commit_calls_the_single_mutation_entry() -> None:
    sites = _call_sites("mutate_now")
    assert set(sites) == {"src/agent_harness/memory/v2/commit.py"}, sites
    assert len(sites["src/agent_harness/memory/v2/commit.py"]) >= 3
    # 落盘函数只在引擎自己的 mutate_now 临界区里被调用
    assert list(_call_sites("_write_file")) == [
        "src/agent_harness/memory/v2/markdown_store.py"]
    assert list(_call_sites("_set_entries")) == [
        "src/agent_harness/memory/v2/markdown_store.py"]


#: 能打开/替换内容文件的原语。静态图不追踪 `_write_file` 的名字，而是按「写原语 +
#: 内容路径」判定：改个函数名或直接 `engine.path_for(...).write_text(...)` 都会被抓住。
_CONTENT_PATH_HINTS = frozenset({
    "memory_file", "user_file", "memories", "MEMORY.md", "USER.md",
    "MEMORIES_DIRECTORY_NAME",
})
#: `path_for` 在各状态 store 上也有同名方法（receipt/staged/job/tombstone）；
#: 只有经过 markdown 引擎的 `path_for` 才指向 memories/ 内容文件。
_ENGINE_HINTS = frozenset({"engine", "_engine", "markdown", "markdown_store"})
_WRITE_ATTRS = frozenset({
    "write_text", "write_bytes", "replace", "rename",
    "copy", "copyfile", "copy2", "move", "atomic_write_text",
    "unlink", "remove", "touch", "truncate", "rmtree", "rmdir",
})
_WRITE_OPEN_FLAGS = frozenset({
    "O_WRONLY", "O_RDWR", "O_CREAT", "O_TRUNC", "O_APPEND",
})
_MODULE_NAMES = frozenset({
    "os", "shutil", "pathlib", "io", "codecs", "gzip", "bz2", "lzma", "builtins",
})
_ALLOWED_WRITE_MODULES = frozenset({
    # 引擎自身与底层原子写原语；其余模块只能经 mutate_now / MemoryCommit。
    "src/agent_harness/memory/v2/markdown_store.py",
    "src/agent_harness/memory/v2/file_state.py",
    # migration 清单是操作状态，不是 memories/ 内容文件。
    "src/agent_harness/memory/v2/identity_mapping.py",
})


def _engine_aliases(tree: ast.AST) -> set[str]:
    """`md = engine` / `store = markdown` 这类别名，静态图里也必须跟着走。"""
    aliases: set[str] = set()
    changed = True
    while changed:
        changed = False
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Assign, ast.AnnAssign)) or node.value is None:
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            value = node.value
            is_engine = (
                isinstance(value, ast.Name)
                and (value.id in _ENGINE_HINTS or value.id in aliases)
            ) or (
                isinstance(value, ast.Attribute) and value.attr in _ENGINE_HINTS
            )
            if not is_engine:
                continue
            for target in targets:
                if isinstance(target, ast.Name) and target.id not in aliases:
                    aliases.add(target.id)
                    changed = True
    return aliases


def _module_aliases(tree: ast.AST) -> set[str]:
    """`import os as o` / `from pathlib import Path` 后的名字也要按模块识别。"""
    names: set[str] = set(_MODULE_NAMES)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in _MODULE_NAMES:
                    names.add(alias.asname or alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module in _MODULE_NAMES:
            for alias in node.names:
                names.add(alias.asname or alias.name)
    return names


def _is_engine_ref(node: ast.AST, aliases: set[str]) -> bool:
    return any(
        (isinstance(part, ast.Name)
         and (part.id in _ENGINE_HINTS or part.id in aliases))
        or (isinstance(part, ast.Attribute) and part.attr in _ENGINE_HINTS)
        for part in ast.walk(node)
    )


def _references_content(
    node: ast.AST, tainted: set[str], aliases: set[str] | None = None,
    path_functions: set[str] | None = None,
    path_methods: set[str] | None = None,
) -> bool:
    engine_aliases = aliases or set()
    functions = path_functions or set()
    methods = path_methods or set()
    for child in ast.walk(node):
        if isinstance(child, ast.Attribute):
            if child.attr in _CONTENT_PATH_HINTS:
                return True
            if child.attr in methods:
                # 类体里的 `PATH = engine.path_for(...)`：`store.PATH` 就是内容路径。
                # 方法名也走这一支（保守：同名属性访问一律当路径）。
                return True
            if child.attr == "path_for" and _is_engine_ref(child.value, engine_aliases):
                return True
        if isinstance(child, ast.Call):
            func = child.func
            if isinstance(func, ast.Name) and func.id in functions:
                return True
            if isinstance(func, ast.Attribute) and func.attr in methods:
                return True
            if (
                isinstance(func, ast.Name)
                and func.id == "getattr"
                and len(child.args) >= 2
                and isinstance(child.args[1], ast.Constant)
                and child.args[1].value == "path_for"
                and _is_engine_ref(child.args[0], engine_aliases)
            ):
                return True
        if (
            isinstance(child, ast.Constant)
            and isinstance(child.value, str)
            and child.value in _CONTENT_PATH_HINTS
        ):
            return True
        if isinstance(child, ast.Name) and child.id in tainted:
            return True
    return False


def _is_write_mode(value: str) -> bool:
    """合法文件模式里含写/追加/独占位；``"data"`` / ``"raw"`` 这类普通字符串不算。"""
    if not value or any(char not in "rwaxbt+" for char in value):
        return False
    if sum(char in "rwax" for char in value) != 1 or value.count("+") > 1:
        return False
    if "b" in value and "t" in value:
        return False
    return any(char in value for char in "wax+")


def _references_method_content(
    node: ast.AST, local: set[str], attr_names: set[str], aliases: set[str],
    path_functions: set[str], path_methods: set[str],
) -> bool:
    """方法体里的内容路径引用：额外认 `self.<attr>`（类级属性缓存路径）。"""
    if _references_content(node, local, aliases, path_functions, path_methods):
        return True
    return any(
        isinstance(child, ast.Attribute)
        and isinstance(child.value, ast.Name)
        and child.value.id == "self"
        and child.attr in attr_names
        for child in ast.walk(node)
    )


def _content_path_functions(
    tree: ast.AST, aliases: set[str], path_methods: set[str] | None = None,
    path_vars: set[str] | None = None,
) -> set[str]:
    """返回 `def p(): return engine.path_for(...)` / 返回内容路径别名的函数名。

    名字集合本身也参与判定并迭代到不动点：`outer()` 只返回 `inner()` 时，必须等
    `inner` 先被认定为内容路径函数，`outer` 才能被认定——否则 `outer().write_text()`
    这种多一层包装的旁路会漏扫。已识别的方法名同样参与，`def f(): return
    store.content_path()` 才会被追上；模块级别名（`PATH = ...`）经函数包装返回时，
    由 `path_vars` 作为初始 taint 传入（review R11-P4-3）。
    """
    methods = path_methods or set()
    known_paths = path_vars or set()
    names: set[str] = set()
    changed = True
    while changed:
        changed = False
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if node.name in names:
                continue
            local: set[str] = set(known_paths)
            local_changed = True
            while local_changed:
                local_changed = False
                for child in ast.walk(node):
                    if not isinstance(child, (ast.Assign, ast.AnnAssign)) or child.value is None:
                        continue
                    if not _references_content(
                        child.value, local, aliases, names, methods,
                    ):
                        continue
                    for target in _assign_targets(child):
                        if isinstance(target, ast.Name) and target.id not in local:
                            local.add(target.id)
                            local_changed = True
            if any(
                isinstance(child, ast.Return) and child.value is not None
                and _references_content(child.value, local, aliases, names, methods)
                for child in ast.walk(node)
            ):
                names.add(node.name)
                changed = True
    return names


def _content_path_methods(
    tree: ast.AST, aliases: set[str], path_functions: set[str],
    path_vars: set[str] | None = None,
) -> set[str]:
    """返回类方法名与类级路径属性名（`store.content_path()` / `store.PATH`）。

    排除 `path_for`：它在 receipt/staged/job 等状态 store 上同名但指向操作状态
    文件，由 `_is_engine_ref` 单独判定，不能按方法名一刀切。方法名匹配对普通仓库
    可能偏保守（同名方法会被当成路径），但这个静态闸门宁可多报也不漏掉一条动态
    包装的写旁路。

    类级不动点覆盖四种包装：方法内局部别名、`self.<attr> = <内容路径>` 后再返回
    属性、`return self.other_method()` 的方法链、以及类体 `PATH = engine.path_for(...)`
    后经实例属性访问（`store.PATH.write_text`，review R11-P4-3）。
    """
    known_paths = path_vars or set()
    names: set[str] = set()
    attr_names: set[str] = set()
    changed = True
    while changed:
        changed = False
        for class_node in ast.walk(tree):
            if not isinstance(class_node, ast.ClassDef):
                continue
            for statement in class_node.body:
                if (
                    isinstance(statement, (ast.Assign, ast.AnnAssign))
                    and statement.value is not None
                    and _references_content(
                        statement.value, set(known_paths), aliases,
                        path_functions, names | attr_names,
                    )
                ):
                    for target in _assign_targets(statement):
                        if (
                            isinstance(target, ast.Name)
                            and target.id not in attr_names
                        ):
                            attr_names.add(target.id)
                            changed = True
            for node in class_node.body:
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                if node.name == "path_for":
                    continue
                local: set[str] = set(known_paths)
                local_changed = True
                while local_changed:
                    local_changed = False
                    for child in ast.walk(node):
                        if (
                            not isinstance(child, (ast.Assign, ast.AnnAssign))
                            or child.value is None
                        ):
                            continue
                        if not _references_method_content(
                            child.value, local, attr_names, aliases,
                            path_functions, names,
                        ):
                            continue
                        for target in _assign_targets(child):
                            if isinstance(target, ast.Name) and target.id not in local:
                                local.add(target.id)
                                local_changed = True
                            elif (
                                isinstance(target, ast.Attribute)
                                and isinstance(target.value, ast.Name)
                                and target.value.id == "self"
                                and target.attr not in attr_names
                            ):
                                attr_names.add(target.attr)
                                changed = local_changed = True
                if node.name in names:
                    continue
                if any(
                    isinstance(child, ast.Return) and child.value is not None
                    and _references_method_content(
                        child.value, local, attr_names, aliases,
                        path_functions, names,
                    )
                    for child in ast.walk(node)
                ):
                    names.add(node.name)
                    changed = True
    return names | attr_names


def _write_mode(call: ast.Call, module_names: set[str]) -> ast.expr | None:
    if (
        isinstance(call.func, ast.Attribute)
        and call.func.attr == "open"
        and not _is_module_receiver(call.func.value, module_names)
        and call.args
    ):
        # path.open("w")：第一个位置参数就是 mode，不是路径。
        return call.args[0]
    if len(call.args) > 1:
        return call.args[1]
    return next(
        (kw.value for kw in call.keywords if kw.arg in {"mode", "flags"}), None)


def _has_open_write_flag(node: ast.AST, known: set[str]) -> bool:
    return any(
        (isinstance(child, ast.Attribute) and child.attr in _WRITE_OPEN_FLAGS)
        or (
            isinstance(child, ast.Name)
            and (child.id in _WRITE_OPEN_FLAGS or child.id in known)
        )
        for child in ast.walk(node)
    )


def _assign_targets(node: ast.AST) -> list[ast.expr]:
    if isinstance(node, ast.Assign):
        return list(node.targets)
    if isinstance(node, (ast.AnnAssign, ast.AugAssign)):
        return [node.target]
    return []


def _write_mode_vars(tree: ast.AST) -> tuple[set[str], set[str]]:
    """``flags = os.O_WRONLY | os.O_CREAT`` / ``flags |= O_TRUNC`` / ``mode = "w"``。

    先存变量、再用变量 open 的写法同样算写；增量赋值（``|=`` / ``+=``）也要跟到。
    """
    flag_vars: set[str] = set()
    mode_vars: set[str] = set()
    changed = True
    while changed:
        changed = False
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                continue
            if node.value is None:
                continue
            names = {
                target.id for target in _assign_targets(node)
                if isinstance(target, ast.Name)
            }
            value = node.value
            if isinstance(value, ast.Name):
                is_flag = value.id in flag_vars
                is_mode = value.id in mode_vars
            else:
                is_flag = _has_open_write_flag(value, flag_vars)
                is_mode = any(
                    isinstance(child, ast.Constant)
                    and isinstance(child.value, str)
                    and _is_write_mode(child.value)
                    for child in ast.walk(value)
                )
            if isinstance(node, ast.AugAssign):
                is_flag = is_flag or any(
                    isinstance(target, ast.Name) and target.id in flag_vars
                    for target in _assign_targets(node))
                is_mode = is_mode or any(
                    isinstance(target, ast.Name) and target.id in mode_vars
                    for target in _assign_targets(node))
            fresh_flags = names - flag_vars if is_flag else set()
            fresh_modes = names - mode_vars if is_mode else set()
            if fresh_flags:
                flag_vars |= fresh_flags
                changed = True
            if fresh_modes:
                mode_vars |= fresh_modes
                changed = True
    return flag_vars, mode_vars


def _is_path_expr(
    node: ast.AST, aliases: set[str], path_vars: set[str],
    path_functions: set[str] | None = None,
    path_methods: set[str] | None = None,
) -> bool:
    """判断表达式是不是**单个内容路径**（而不是装着路径的 list/dict）。"""
    functions = path_functions or set()
    methods = path_methods or set()
    if isinstance(node, ast.Name):
        return node.id in path_vars or node.id in _CONTENT_PATH_HINTS
    if isinstance(node, ast.Attribute):
        if node.attr in _CONTENT_PATH_HINTS:
            return True
        if node.attr in methods:
            # 类级缓存属性（`store.PATH`）与方法名同一集合：属性访问也按路径算。
            return True
        if node.attr == "path_for" and _is_engine_ref(node.value, aliases):
            return True
        if node.attr in {"parent", "resolve", "absolute", "with_name", "with_suffix"}:
            return _is_path_expr(node.value, aliases, path_vars, functions, methods)
        return False
    if isinstance(node, ast.Call):
        if isinstance(node.func, ast.Name) and node.func.id in functions:
            return True
        if isinstance(node.func, ast.Attribute) and node.func.attr in methods:
            return True
        if (
            isinstance(node.func, ast.Attribute)
            and node.func.attr == "path_for"
            and _is_engine_ref(node.func.value, aliases)
        ):
            return True
        if (
            isinstance(node.func, ast.Attribute)
            and node.func.attr in {"joinpath", "resolve", "absolute", "with_name", "with_suffix"}
        ):
            return _is_path_expr(node.func.value, aliases, path_vars, functions, methods)
        return False
    if isinstance(node, ast.Constant):
        return isinstance(node.value, str) and node.value in _CONTENT_PATH_HINTS
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        return _is_path_expr(node.left, aliases, path_vars, functions, methods) or _is_path_expr(
            node.right, aliases, path_vars, functions, methods)
    return False


def _path_aliases(
    tree: ast.AST, aliases: set[str], path_functions: set[str] | None = None,
    path_methods: set[str] | None = None,
) -> set[str]:
    """只收「直接就是内容路径」的别名；装着路径的 list 不在此列。"""
    path_vars: set[str] = set()
    changed = True
    while changed:
        changed = False
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Assign, ast.AnnAssign)) or node.value is None:
                continue
            if not _is_path_expr(
                node.value, aliases, path_vars, path_functions, path_methods,
            ):
                continue
            for target in _assign_targets(node):
                if isinstance(target, ast.Name) and target.id not in path_vars:
                    path_vars.add(target.id)
                    changed = True
    return path_vars


def _bound_write_methods(
    tree: ast.AST, tainted: set[str], aliases: set[str],
    path_functions: set[str] | None = None,
    path_methods: set[str] | None = None,
) -> set[str]:
    """``writer = engine.path_for("memory").write_text; writer("x")`` 也要抓住。"""
    names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Assign, ast.AnnAssign)) or node.value is None:
            continue
        value = node.value
        if (
            isinstance(value, ast.Attribute)
            and value.attr in (_WRITE_ATTRS | {"write_json"})
            and _references_content(
                value.value, tainted, aliases, path_functions, path_methods)
        ):
            names |= {
                target.id for target in _assign_targets(node)
                if isinstance(target, ast.Name)
            }
    return names


def _open_for_write(
    call: ast.Call, flag_vars: set[str], mode_vars: set[str],
    module_names: set[str],
) -> bool:
    """``open(..., "w")`` 与 ``os.open(..., flags=os.O_WRONLY)`` 都算写。"""
    mode = _write_mode(call, module_names)
    if mode is None:
        return False
    if isinstance(mode, ast.Constant):
        return isinstance(mode.value, str) and _is_write_mode(mode.value)
    if isinstance(mode, ast.Name):
        return mode.id in mode_vars or mode.id in flag_vars
    return _has_open_write_flag(mode, flag_vars)


def _is_module_receiver(
    node: ast.AST | None, module_names: set[str] | None = None,
) -> bool:
    root: ast.AST | None = node
    while isinstance(root, ast.Attribute):
        root = root.value
    names = module_names or set(_MODULE_NAMES)
    return isinstance(root, ast.Name) and root.id in names


def _path_argument(call: ast.Call) -> ast.expr | None:
    if call.args:
        return call.args[0]
    return next(
        (kw.value for kw in call.keywords if kw.arg in {"file", "path", "filename"}),
        None,
    )


def _scan_tree(relative: str, tree: ast.AST) -> list[int]:
    """扫一棵 AST：直接对 memories/ 内容文件落笔的调用点行号。"""
    aliases = _engine_aliases(tree)
    module_names = _module_aliases(tree)
    path_functions: set[str] = set()
    path_methods: set[str] = set()
    path_vars: set[str] = set()
    while True:
        # 函数 / 方法 / 模块级别名互相包装：`def f(): return store.content_path()`、
        # `PATH = engine.path_for(...)` + `def f(): return PATH` 都要迭代到同一个
        # 不动点；否则多一层包装的写旁路会漏扫（review R11-P4-3）。
        next_functions = _content_path_functions(
            tree, aliases, path_methods, path_vars)
        next_methods = _content_path_methods(
            tree, aliases, next_functions, path_vars)
        next_vars = _path_aliases(tree, aliases, next_functions, next_methods)
        if (
            next_functions == path_functions and next_methods == path_methods
            and next_vars == path_vars
        ):
            break
        path_functions, path_methods = next_functions, next_methods
        path_vars = next_vars
    flag_vars, mode_vars = _write_mode_vars(tree)
    tainted: set[str] = set()
    changed = True
    while changed:
        changed = False
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Assign, ast.AnnAssign)) or node.value is None:
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if _references_content(
                node.value, tainted, aliases, path_functions, path_methods,
            ):
                for target in targets:
                    if isinstance(target, ast.Name) and target.id not in tainted:
                        tainted.add(target.id)
                        changed = True
    bound_writes = _bound_write_methods(
        tree, tainted, aliases, path_functions, path_methods)

    found: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        attr = func.attr if isinstance(func, ast.Attribute) else None
        name = func.id if isinstance(func, ast.Name) else None
        suspicious = False
        if name in bound_writes:
            suspicious = True
        elif (
            attr in _WRITE_ATTRS or name == "atomic_write_text"
            or attr == "write_json" or name == "write_json"
        ):
            receiver = func.value if isinstance(func, ast.Attribute) else None
            receiver_hit = (
                receiver is not None
                and _references_content(
                    receiver, tainted, aliases, path_functions, path_methods)
            )
            path_receiver = (
                receiver is not None
                and _is_path_expr(
                    receiver, aliases, path_vars, path_functions, path_methods)
            )
            if attr == "replace" and len(node.args) >= 2 and receiver_hit:
                # Path.replace 只有一个参数；两参形式是 str.replace（文本变换，
                # 不是写盘）或 os.replace（目标在参数里，由下面的参数扫描兜住）。
                receiver_hit = False
            argument_hit = any(
                # 目标也可能在参数或关键字里（module-qualified atomic_write_text、
                # shutil.copy(src, engine.path_for(...))、write_json(path=...)）。
                _references_content(
                    argument, tainted, aliases, path_functions, path_methods)
                for argument in (
                    *node.args, *(keyword.value for keyword in node.keywords))
            )
            if attr == "remove" and not path_receiver:
                # list/dict/set.remove 是在内存集合里摘元素，不是删文件；只有
                # os.remove 这类模块函数才继续扫描参数里的内容路径。
                suspicious = argument_hit and _is_module_receiver(
                    receiver, module_names)
            else:
                suspicious = receiver_hit or argument_hit
        elif attr == "open" or name == "open":
            path_arg = _path_argument(node)
            suspicious = bool(
                _open_for_write(node, flag_vars, mode_vars, module_names) and (
                    (path_arg is not None
                     and _references_content(
                         path_arg, tainted, aliases, path_functions, path_methods))
                    or (isinstance(func, ast.Attribute)
                        and _references_content(
                            func.value, tainted, aliases, path_functions, path_methods))
                )
            )
        elif attr == "fdopen" or name == "fdopen":
            mode = _write_mode(node, module_names)
            suspicious = bool(
                isinstance(mode, ast.Constant)
                and isinstance(mode.value, str)
                and _is_write_mode(mode.value)
                and _references_content(
                    func, tainted, aliases, path_functions, path_methods)
            )
        if suspicious and relative not in _ALLOWED_WRITE_MODULES:
            found.append(node.lineno)
    return found


def _content_write_sites() -> dict[str, list[int]]:
    """直接对 memories/ 内容文件落笔的调用点（不经过 `_write_file` / `mutate_now`）。

    先做模块内简单污点传播：`path = engine.path_for(target)` 之后 `path.write_text(...)`
    同样算旁路。读原语（`read_text` / `open(..., "r")`）不在扫描面内。
    """
    found: dict[str, list[int]] = {}
    for path in sorted(SOURCE_ROOT.rglob("*.py")):
        relative = str(path.relative_to(REPO_ROOT)).replace("\\", "/")
        tree = ast.parse(path.read_text(encoding="utf-8"))
        hits = _scan_tree(relative, tree)
        if hits:
            found[relative] = hits
    return found


def test_no_module_opens_a_second_write_handle_on_content_files() -> None:
    offenders = _content_write_sites()
    assert offenders == {}, (
        "这些模块直接对 memories/ 内容文件落笔，绕过 mutate_now/MemoryCommit："
        f"{offenders}"
    )


def test_static_scan_catches_alias_keyword_and_atomic_write() -> None:
    """静态图必须覆盖别名/关键字 mode/atomic_write_text/getattr 这些旁路写法。"""
    source = (
        "engine = object()\n"
        "md = engine\n"
        "path = md.path_for('memory')\n"
        "path.open(mode='w')\n"
        "atomic_write_text(engine.path_for('user'), 'x')\n"
        "getattr(engine, 'path_for')('memory').write_text('x')\n"
        "file_state.atomic_write_text(engine.path_for('memory'), 'x')\n"
        "os.open(engine.path_for('memory'), os.O_WRONLY | os.O_CREAT)\n"
        "shutil.copy('x', engine.path_for('user'))\n"
        "engine.path_for('memory').read_text()\n"
    )

    assert _scan_tree("src/fake.py", ast.parse(source)) == [4, 5, 6, 7, 8, 9]


def test_static_scan_catches_delete_and_saved_open_modes() -> None:
    """P4 S3：删除类原语与「先存 flags/mode 变量再 open」也不能漏。"""
    source = (
        "engine = object()\n"
        "flags = os.O_WRONLY | os.O_CREAT\n"
        "os.open(engine.path_for('memory'), flags)\n"
        "mode = 'w'\n"
        "open(engine.path_for('user'), mode)\n"
        "engine.path_for('memory').unlink()\n"
        "engine.path_for('user').touch()\n"
        "shutil.rmtree(engine.path_for('memory').parent)\n"
        "os.replace(engine.path_for('memory'), 'backup')\n"
        "os.remove(engine.path_for('user'))\n"
    )

    assert _scan_tree("src/fake.py", ast.parse(source)) == [3, 5, 6, 7, 8, 9, 10]


def test_static_scan_catches_flags_keyword_augassign_write_json_and_bound_methods() -> None:
    """P4 S3：关键字 flags、增量标志位、write_json、绑定后的写方法也不能漏。"""
    source = (
        "engine = object()\n"                                  # 1
        "flags = os.O_WRONLY\n"                                # 2
        "flags |= os.O_CREAT\n"                                # 3
        "os.open(engine.path_for('memory'), flags=flags)\n"    # 4
        "write_json(engine.path_for('user'), {})\n"            # 5
        "engine.path_for('memory').open('w')\n"                # 6
        "writer = engine.path_for('user').write_bytes\n"       # 7
        "writer(b'x')\n"                                       # 8
        "mode = 'data'\n"                                      # 9
        "open(engine.path_for('memory'), mode)\n"              # 10
        "paths = [engine.path_for('user')]\n"                  # 11
        "paths.remove(paths[0])\n"                             # 12
        "import os as o\n"                                     # 13
        "o.open(engine.path_for('user'), 'w')\n"               # 14
        "write_json(path=engine.path_for('memory'), payload={})\n"  # 15
        "def content_path():\n"                                # 16
        "    return engine.path_for('user')\n"                 # 17
        "content_path().write_text('x')\n"                     # 18
        "open(file=str(engine.path_for('memory')), mode='w')\n"  # 19
    )

    assert _scan_tree("src/fake.py", ast.parse(source)) == [4, 5, 6, 8, 14, 15, 18, 19]


def test_static_scan_catches_method_and_nested_helpers() -> None:
    """review P4-4：方法返回路径、以及多层包装的函数返回，也必须能追到落笔点。"""
    source = (
        "engine = object()\n"                                  # 1
        "class Store:\n"                                       # 2
        "    def content_path(self):\n"                        # 3
        "        return self.engine.path_for('memory')\n"      # 4
        "store = Store()\n"                                    # 5
        "store.content_path().write_text('x')\n"               # 6
        "def inner():\n"                                       # 7
        "    return engine.path_for('user')\n"                 # 8
        "def outer():\n"                                       # 9
        "    return inner()\n"                                 # 10
        "outer().write_text('x')\n"                            # 11
        "p = store.content_path()\n"                           # 12
        "p.unlink()\n"                                         # 13
    )

    assert _scan_tree("src/fake.py", ast.parse(source)) == [6, 11, 13]


def test_static_scan_catches_method_chains_and_cached_path_attributes() -> None:
    """review P4-2：方法→方法、self.<attr> 缓存、函数包装方法都不能漏。"""
    source = (
        "engine = object()\n"                                  # 1
        "class Store:\n"                                       # 2
        "    def __init__(self):\n"                            # 3
        "        self.path = self.engine.path_for('user')\n"   # 4
        "    def raw(self):\n"                                 # 5
        "        return self.engine.path_for('memory')\n"      # 6
        "    def wrapped(self):\n"                             # 7
        "        return self.raw()\n"                          # 8
        "    def cached(self):\n"                              # 9
        "        return self.path\n"                           # 10
        "    def local(self):\n"                               # 11
        "        p = self.engine.path_for('memory')\n"         # 12
        "        return p\n"                                   # 13
        "store = Store()\n"                                    # 14
        "store.wrapped().write_text('x')\n"                    # 15
        "store.cached().unlink()\n"                            # 16
        "store.local().touch()\n"                              # 17
        "def via_method():\n"                                  # 18
        "    return store.raw()\n"                             # 19
        "via_method().write_text('x')\n"                       # 20
    )

    assert _scan_tree("src/fake.py", ast.parse(source)) == [15, 16, 17, 20]


def test_static_scan_catches_class_body_path_attributes() -> None:
    """review R11-P4-3：类体 `PATH = engine.path_for(...)` 经实例属性访问也不能漏。"""
    source = (
        "engine = object()\n"                                  # 1
        "class Store:\n"                                       # 2
        "    PATH = engine.path_for('memory')\n"               # 3
        "store = Store()\n"                                    # 4
        "store.PATH.write_text('x')\n"                         # 5
    )

    assert _scan_tree("src/fake.py", ast.parse(source)) == [5]


def test_static_scan_catches_direct_self_attribute_writes() -> None:
    """review R11-P4-2：直接对 `self.<attr>` 落笔、类级属性与属性局部别名都要抓住。"""
    source = (
        "engine = object()\n"                                    # 1
        "class Store:\n"                                         # 2
        "    PATH = engine.path_for('user')\n"                   # 3
        "    def __init__(self):\n"                              # 4
        "        self.path = engine.path_for('memory')\n"        # 5
        "    def write(self):\n"                                 # 6
        "        self.path.write_text('x')\n"                    # 7
        "        self.PATH.unlink()\n"                           # 8
        "        p = self.path\n"                                # 9
        "        p.touch()\n"                                    # 10
    )

    assert _scan_tree("src/fake.py", ast.parse(source)) == [7, 8, 10]


def test_static_scan_catches_module_alias_returned_by_a_wrapper() -> None:
    """review R11-P4-3：模块级别名经函数包装返回，也要追到落笔点。"""
    source = (
        "engine = object()\n"                                  # 1
        "PATH = engine.path_for('user')\n"                     # 2
        "def get_path():\n"                                    # 3
        "    return PATH\n"                                    # 4
        "get_path().unlink()\n"                                # 5
    )

    assert _scan_tree("src/fake.py", ast.parse(source)) == [5]


def test_collection_remove_and_plain_string_mode_are_not_content_writes() -> None:
    """P4-2：list.remove 不是删文件；普通字符串变量也不是写模式。"""
    source = (
        "engine = object()\n"
        "paths = [engine.path_for('memory')]\n"
        "paths.remove(paths[0])\n"
        "mode = 'data'\n"
        "open(engine.path_for('user'), mode)\n"
    )

    assert _scan_tree("src/fake.py", ast.parse(source)) == []


def test_string_replace_is_not_a_content_write() -> None:
    """P4 S3：对已读出的文本调用 str.replace 不是对内容文件落笔。"""
    source = (
        "engine = object()\n"
        "text = engine.path_for('memory').read_text()\n"
        "text.replace('a', 'b')\n"
    )

    assert _scan_tree("src/fake.py", ast.parse(source)) == []


def test_live_entries_are_only_assigned_inside_the_engine() -> None:
    assert _entry_assignments() == {"src/agent_harness/memory/v2/markdown_store.py"}


def test_store_exposes_no_second_write_surface() -> None:
    public = {
        name for name in vars(MarkdownMemoryStore)
        if not name.startswith("_")
    }
    assert "mutate_now" in public
    assert not public & {
        "add", "replace", "remove", "apply_batch", "batch", "save", "save_to_disk",
        "write", "write_entries",
    }, "旧写面不得残留：所有变更必须经 mutate_now"


# --------------------------------------------------------------------------------------
# 运行时配对：每一次落盘都能在 COMMITTED 回执的 after_digest 里找到
# --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_every_runtime_write_is_paired_with_a_committed_receipt(tmp_path: Path) -> None:
    settings = Settings(
        _env_file=None, workspace_dir=str(tmp_path / "workspace"), model_api_key="sk-test")
    service = await assembly.build_memory_v2_service(settings)
    engine = service.markdown
    assert engine is not None

    writes: list[tuple[str, str]] = []
    real_write = engine._write_file

    def _record(path: Path, entries: list[str]) -> None:
        target = "user" if path.name == "USER.md" else "memory"
        writes.append((target, entries_digest(entries)))
        real_write(path, entries)

    engine._write_file = _record  # type: ignore[method-assign]

    created = await service.create(make_draft(content="服务创建"), LOCAL)
    updated = await service.update(
        created.id, make_draft(content="服务更新", source_event_ids=["event-2"]), LOCAL)
    await service.invalidate(updated.id, LOCAL)
    editable = await service.create(
        make_draft(content="服务编辑", source_event_ids=["event-edit"]), LOCAL)
    await service.edit(
        editable.id, LOCAL, expected_version=1, content="服务编辑修订",
        payload=make_draft(content="服务编辑修订").payload)
    doomed = await service.create(
        make_draft(content="服务删除", source_event_ids=["event-3"]), LOCAL)
    await service.delete(doomed.id, LOCAL)

    tool = MemoryTool(service.commit, service=service)
    assert (await tool.execute(
        _MemoryArgs(action="add", target="memory", content="工具写入"))).ok is True

    # formation 的提交前发布（executor 走的就是这个入口，确定性 jobp_ 前缀）
    await service.mirror_committed(
        [make_record(memory_id="mem-formation-writer", content="formation 写入")],
        operation_prefix="jobp_single_writer",
    )
    # 双目标按 digest 清理（治理删除的墓碑-only 分支）
    await service.commit.remove_by_digest(
        [entry_digest("工具写入")], operation_id="del_single_writer_digest")

    staged = service.staged
    record = await staged.stage(
        {"target": "memory", "action": "add", "content": "审批写入"}, None, LOCAL)
    decision, _response = await staged.approve(record.id, LOCAL)
    assert decision.allow is True

    blocked_id = await service.blocked.insert(
        job_id="job-single-writer", identity=_identity(LOCAL), project_id=None,
        payload={"actions": [{
            "action": "add", "scope": "user_global",
            "draft": make_draft(
                content="放行写入", source_event_ids=["event-blocked"]).model_dump(mode="json"),
            "pin": None,
        }]},
    )
    assert blocked_id is not None
    assert (await service.blocked.promote(blocked_id, LOCAL)).applied is True
    # bulk_delete 也必须是同一条 seam（治理批量删除）
    assert await service.bulk_delete(LOCAL, kind=None)

    assert writes, "本用例必须真的发生内容落盘"

    planned: list[tuple[str, str, str]] = []
    for path in service.state_root.receipts_dir.glob("*.json"):
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert payload["state"] == "COMMITTED", path.name
        for target, plan in payload["targets"].items():
            if (
                plan.get("planned")
                and plan.get("after_digest")
                and plan["after_digest"] != plan.get("before_digest")
            ):
                planned.append(
                    (payload["operation_id"], target, plan["after_digest"]))

    prefixes = {operation_id.split("_", 1)[0] + "_" for operation_id, _, _ in planned}
    assert {"mcf_", "del_", "stgp_", "blkp_", "jobp_"} <= prefixes

    # 每一次落盘恰好对应一条计划（数量也相等，集合相等只能证明包含关系）。
    assert Counter(writes) == Counter(
        (target, digest) for _, target, digest in planned
    ), "落盘与回执计划必须一一配对（多写或无回执写都会在这里暴露）"

    # 每个入口自己的 operation id 必须真的对上一次落盘，而不是只有前缀出现过。
    by_operation = {operation_id: (target, digest)
                    for operation_id, target, digest in planned}
    assert by_operation["jobp_single_writer_apply"][0] == "memory"
    assert by_operation[f"stgp_{record.id}"][0] == "memory"
    assert by_operation[f"blkp_{blocked_id}_apply"][0] == "memory"
    assert by_operation["del_single_writer_digest"][0] == "memory"
    assert any(operation_id.startswith("mcf_") for operation_id in by_operation)
    assert any(operation_id.startswith("del_") and operation_id != "del_single_writer_digest"
               for operation_id in by_operation)
