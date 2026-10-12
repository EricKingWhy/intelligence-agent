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
    """`try: ... except SeqConflict:` 的处理器子句（#1024 的 resync 落点）。"""
    for candidate in ast.walk(node):
        if not isinstance(candidate, ast.Try):
            continue
        for handler in candidate.handlers:
            exception = handler.type
            if isinstance(exception, ast.Name) and exception.id == "SeqConflict":
                return handler
    return None


def _attr_calls_in(node: ast.AST, name: str) -> list[ast.Call]:
    """该子树里任何 `<...>.<name>(...)` 的调用（不看嵌套函数——漏斗里没有）。"""
    return [
        candidate
        for candidate in ast.walk(node)
        if isinstance(candidate, ast.Call)
        and isinstance(candidate.func, ast.Attribute)
        and candidate.func.attr == name
    ]


def _bare_calls_in(node: ast.AST, name: str) -> list[ast.Call]:
    """该子树里对裸名 `name(...)`（import 进来的模块级函数）的调用。"""
    return [
        candidate
        for candidate in ast.walk(node)
        if isinstance(candidate, ast.Call)
        and isinstance(candidate.func, ast.Name)
        and candidate.func.id == name
    ]


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

    # 漏斗内**一个**物理写点，包在「有界撞号重投」循环里（#1024）。形状锁死为：
    # 写点唯一；循环是 `while True`；成功分支 `break`；`except SeqConflict` 不另抄
    # 一次写、必须重读盘面、撞满墙钟预算必须 `raise`（有界 fail-loud，不许吞成写成功）；
    # 换号**必须先核对盘面**（`if event.seq <= last`）。
    funnel_appends = _self_store_append_calls(funnel)
    assert len(funnel_appends) == 1, "物理写点必须唯一（重投靠循环回边，不另抄一次写）"

    loop = next(
        (
            n
            for n in ast.walk(funnel)
            if isinstance(n, ast.While) and funnel_appends[0] in list(ast.walk(n))
        ),
        None,
    )
    assert loop is not None, "写点必须包在重投循环内（重投靠循环回边）"
    assert any(isinstance(n, ast.Break) for n in ast.walk(loop)), (
        "写成功后必须 break（漏掉会把一次正常 append 变成自我冲突）"
    )

    handler = _seq_conflict_handler(funnel)
    assert handler is not None, "resync 必须挂在 except SeqConflict 分支上"
    assert _self_store_append_calls(handler) == [], "except 里不得再抄一次物理写"
    assert _attr_calls_in(loop, "read_events"), "重投循环每轮必须先重读盘面核对 seq"
    deadline_if = next(
        (
            n
            for n in ast.walk(handler)
            if isinstance(n, ast.If)
            and any(
                isinstance(c, ast.Call) and getattr(c.func, "attr", "") == "monotonic"
                for c in ast.walk(n.test)
            )
        ),
        None,
    )
    assert deadline_if is not None, "撞满墙钟预算必须单独判定"
    assert any(isinstance(n, ast.Raise) for n in ast.walk(deadline_if)), (
        "撞满预算必须 raise（有界 fail-loud）——break/return 会把没落盘的事件当成写成"
    )

    # 有界性：预算必须是墙钟常量 + monotonic 时钟（不是无界 while True 死循环）。
    names = {n.id for n in ast.walk(funnel) if isinstance(n, ast.Name)}
    assert "_APPEND_CONFLICT_DEADLINE_S" in names, "重投必须有墙钟预算（有界）"
    assert _attr_calls_in(funnel, "monotonic"), "预算必须按 monotonic 时钟判定"

    # 换号必须**有条件**（盘面核对之下）。#1024 第三轮实测教训：无条件换号会让活聚合
    # 永远追不上持续抢号的旁路写者（零延迟竞争下 22% 的 append 直接抛错）。
    seq_check_ifs = [
        n
        for n in ast.walk(funnel)
        if isinstance(n, ast.If)
        and isinstance(n.test, ast.Compare)
        and any(isinstance(op, ast.LtE) for op in n.test.ops)
    ]
    assert seq_check_ifs, "换号必须挂在「seq 已被占用」的盘面核对之下"
    reassigns = _bare_calls_in(funnel, "replace")
    assert reassigns, "resync 必须换号重投"
    guarded = [
        call
        for call in reassigns
        if any(call in list(ast.walk(node)) for node in seq_check_ifs)
    ]
    assert len(guarded) == len(reassigns), (
        "不得有无条件的换号（#1024 第三轮：会永远追不上旁路写者）"
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
