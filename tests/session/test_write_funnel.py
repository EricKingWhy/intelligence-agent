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


def _session_methods() -> dict[str, ast.FunctionDef | ast.AsyncFunctionDef]:
    session = _session_class()
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
    """Only the private funnel may perform Session-owned physical appends."""
    methods = _session_methods()
    session = _session_class()

    assert len(_self_store_append_calls(session)) == 1
    assert len(_self_store_append_calls(methods["_persist_event"])) == 1
    assert _self_store_append_calls(methods["append"]) == []
    assert _self_store_append_calls(methods["adopt_history"]) == []


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
