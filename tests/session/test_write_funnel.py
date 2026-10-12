"""#252: keep Session's durable commit path single and explicit."""

from __future__ import annotations

import ast
from pathlib import Path

_SESSION_SOURCE = (
    Path(__file__).parents[2]
    / "src"
    / "agent_harness"
    / "session"
    / "session.py"
)


def _session_class() -> ast.ClassDef:
    tree = ast.parse(_SESSION_SOURCE.read_text(encoding="utf-8"))
    return next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "Session"
    )


def _session_methods(
    session: ast.ClassDef | None = None,
) -> dict[str, ast.FunctionDef | ast.AsyncFunctionDef]:
    """类内方法表。传入已解析的类可避免二次 parse —— `id()` 只在同一棵 AST 内
    有意义（#1024：两批互不相干的对象会让「同一写点」判据假红）。"""
    session = _session_class() if session is None else session
    return {
        node.name: node
        for node in session.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def _self_store_append_calls(node: ast.AST) -> list[ast.Call]:
    calls: list[ast.Call] = []
    for candidate in ast.walk(node):
        if not isinstance(candidate, ast.Call):
            continue
        function = candidate.func
        if not isinstance(function, ast.Attribute) or function.attr != "append_event":
            continue
        store = function.value
        if (
            isinstance(store, ast.Attribute)
            and store.attr == "_store"
            and isinstance(store.value, ast.Name)
            and store.value.id == "self"
        ):
            calls.append(candidate)
    return calls


def _self_funnel_calls(node: ast.AST) -> list[ast.Call]:
    calls: list[ast.Call] = []
    for candidate in ast.walk(node):
        if not isinstance(candidate, ast.Call):
            continue
        function = candidate.func
        if (
            isinstance(function, ast.Attribute)
            and function.attr == "_persist_event"
            and isinstance(function.value, ast.Name)
            and function.value.id == "self"
        ):
            calls.append(candidate)
    return calls


def _seq_conflict_handler(node: ast.AST) -> ast.ExceptHandler | None:
    """`try: ... except SeqConflict:` 的处理器子句（#1024 的 resync 重投落点）。"""
    for candidate in ast.walk(node):
        if not isinstance(candidate, ast.Try):
            continue
        for handler in candidate.handlers:
            exception = handler.type
            if isinstance(exception, ast.Name) and exception.id == "SeqConflict":
                return handler
    return None


def test_session_has_one_durable_store_writer() -> None:
    """Only the private funnel may perform Session-owned physical appends.

    #1024 起漏斗内的物理写在源码上是**两次**：首次 + 撞号 resync 的**重投一次**。
    这不是放宽判据，而是把形状钉得更准——真正的风险面（旁路写者绕过 `_persist_event`
    自己写盘）仍由 `append` / `adopt_history` 必须为 0、以及「重投只能挂在
    `except SeqConflict` 里、且恰好一次」两条锁死。
    """
    # 单次解析（`id()` 只在同一棵 AST 内有意义：`_session_class` /
    # `_session_methods` 各自 parse 一遍会得到两批互不相干的对象）。
    session = _session_class()
    methods = _session_methods(session)
    funnel = methods["_persist_event"]

    # 类级物理写点**全部**落在漏斗里（不再是「全类恰一处」：漏斗内现在是
    # 首次写 + 撞号重投两处，见下）。
    in_funnel = {id(call) for call in _self_store_append_calls(funnel)}
    class_level = _self_store_append_calls(session)
    assert class_level, "漏斗里必须有物理写点"
    assert all(id(call) in in_funnel for call in class_level), "物理写点只能住在漏斗里"
    assert _self_store_append_calls(methods["append"]) == []
    assert _self_store_append_calls(methods["adopt_history"]) == []

    funnel_appends = _self_store_append_calls(funnel)
    assert len(funnel_appends) == 2, "漏斗内 = 首次写 + resync 重投一次"
    handler = _seq_conflict_handler(funnel)
    assert handler is not None, "resync 重投必须挂在 except SeqConflict 分支上"
    assert len(_self_store_append_calls(handler)) == 1, "重投恰好一次（有界）"
    assert len(funnel_appends) - len(_self_store_append_calls(handler)) == 1, (
        "重投之外的物理写恰好一次"
    )


def test_normal_and_history_entries_share_the_durable_funnel() -> None:
    """Construction differs, but both entry points share commit/update semantics."""
    methods = _session_methods()

    assert len(_self_funnel_calls(methods["append"])) == 1
    assert len(_self_funnel_calls(methods["adopt_history"])) == 1

    append_call = _self_funnel_calls(methods["append"])[0]
    adopt_call = _self_funnel_calls(methods["adopt_history"])[0]
    assert any(keyword.arg == "notify" for keyword in append_call.keywords)
    assert any(keyword.arg == "notify" for keyword in adopt_call.keywords)
    assert next(k.value.value for k in append_call.keywords if k.arg == "notify") is True
    assert next(k.value.value for k in adopt_call.keywords if k.arg == "notify") is False
