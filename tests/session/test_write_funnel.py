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


def test_session_has_one_durable_store_writer() -> None:
    """只有私有漏斗可以做 Session 自己发起的物理写盘（#252 的行为边界）。

    判据刻意**只**守行为边界：类级物理写点全部住在 `_persist_event` 里、且漏斗内
    至多一个写点。重投靠循环回边，不另抄一次写（#1024 起漏斗里是「首次 + 重投」
    共用同一个写点）。

    #1057：早先这里一路扩到「必须有 while+break、except 里必须 read_events+replace、
    墙钟常量名必须在」——那是在镜像实现形状，任何等价重构都会误红（且已造成两笔
    提交信息互相矛盾却都落库）。循环/换号/墙钟的**行为**由
    `test_seq_resync.py` 的行为用例 + 变异矩阵承重，不在本守卫的判据里。
    """
    session = _session_class()
    methods = _session_methods(session)

    funnel_calls = _self_store_append_calls(methods["_persist_event"])
    assert funnel_calls, "漏斗里必须有物理写点"

    in_funnel = {id(call) for call in funnel_calls}
    class_level = _self_store_append_calls(session)
    assert all(id(call) in in_funnel for call in class_level), "物理写点只能住在漏斗里"
    assert _self_store_append_calls(methods["append"]) == []
    assert _self_store_append_calls(methods["adopt_history"]) == []

    assert len(funnel_calls) == 1, "漏斗内至多一个物理写点（重投靠循环回边，不另抄一次写）"


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
