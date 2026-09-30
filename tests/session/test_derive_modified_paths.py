"""W-31.5（#417）：derive_modified_file_paths 派生面用例。

PRD §4.6 第 4 项「最近修改文件路径清单（只回路径）」：`tool/call` 命中
`WRITE_TOOL_NAMES` 时取 `args["path"]`——去重保序（首现顺序）、坏形状只
损失该条不抛错（`collect_result_fields` 同口径）、纯函数（重放/换实例/
换列表副本同结果）。

与摘要第 8 节「文件清单」（`compactor._programmatic_summary_sections`，
读+改都进、跨压缩累积）是两个口径两个函数——口径互斥用例（T7）与跨压缩
用例（T5，需要 builder 触发真压缩）落在
tests/context/test_modified_paths_injection.py，本文件只钉派生面。
"""

import json

from agent_harness.session import TOOL_CALL, TOOL_RESULT
from agent_harness.session.derive import WRITE_TOOL_NAMES, derive_modified_file_paths
from tests.conftest import make_session


def _call(session, call_id: str, tool_name: str, args: object) -> None:
    session.append(TOOL_CALL, {
        "tool_call_id": call_id, "tool_name": tool_name, "args": args,
    })


def _result(session, call_id: str, output: str) -> None:
    session.append(TOOL_RESULT, {
        "tool_call_id": call_id,
        "content": json.dumps(
            {"ok": True, "message": "ok", "output": output}, ensure_ascii=False,
        ),
    })


def test_write_tool_names_is_the_shared_constant():
    """共享常量语义钉死：provider.py 的 changed_files 与本清单同一口径来源。"""
    assert WRITE_TOOL_NAMES == frozenset({"write", "edit", "apply_patch"})


def test_dedup_keeps_first_occurrence_order(tmp_path):
    """T1：同一路径多次 write/edit/apply_patch 只出现一次，位置 = 首次出现处。"""
    session = make_session(tmp_path)
    _call(session, "c1", "write", {"path": "a.txt"})
    _call(session, "c2", "write", {"path": "b.txt"})
    _call(session, "c3", "edit", {"path": "a.txt"})
    _call(session, "c4", "apply_patch", {"path": "c.txt"})
    _call(session, "c5", "write", {"path": "a.txt"})
    assert derive_modified_file_paths(session.events) == ["a.txt", "b.txt", "c.txt"]


def test_paths_only_no_file_bodies_no_read_tools(tmp_path):
    """T2：只回路径——文件正文（tool/result 里的 output）与读工具一律不进清单。"""
    session = make_session(tmp_path)
    _call(session, "w1", "write", {"path": "a.txt"})
    _result(session, "w1", output="正文" * 300)
    _call(session, "r1", "read_file", {"path": "b.txt"})
    _call(session, "r2", "glob", {"pattern": "*.txt"})
    _call(session, "r3", "grep", {"path": "."})
    _call(session, "r4", "web_search", {"query": "q"})
    paths = derive_modified_file_paths(session.events)
    assert paths == ["a.txt"]
    joined = json.dumps(paths, ensure_ascii=False)
    assert "正文" not in joined


def test_malformed_shapes_lose_only_that_entry(tmp_path):
    """T3：args 缺 path / path 非串 / args 非 dict / 未知工具 → 只损失该条不抛错。"""
    session = make_session(tmp_path)
    _call(session, "c1", "write", {"path": "ok.txt"})
    _call(session, "c2", "write", {})
    _call(session, "c3", "write", {"path": 7})
    _call(session, "c4", "write", {"path": ""})
    _call(session, "c5", "unknown_tool", {"path": "x.txt"})
    _call(session, "c6", "write", None)
    _call(session, "c7", "write", ["not", "a", "dict"])
    assert derive_modified_file_paths(session.events) == ["ok.txt"]


def test_pure_function_replay_and_copies(tmp_path):
    """T4：纯函数——同一事件流派生两次相等、换列表副本相等、空流得空表。"""
    session = make_session(tmp_path)
    _call(session, "c1", "write", {"path": "a.txt"})
    _call(session, "c2", "edit", {"path": "b.txt"})
    events = list(session.events)
    first = derive_modified_file_paths(events)
    assert first == ["a.txt", "b.txt"]
    assert derive_modified_file_paths(events) == first
    assert derive_modified_file_paths(list(events)) == first
    assert derive_modified_file_paths([]) == []
