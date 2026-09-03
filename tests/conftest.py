"""测试共用夹具：SessionEvent 模块的测试便利函数。"""

from __future__ import annotations

from pathlib import Path

from agent_harness.session import JsonlSessionStore, Session


def make_session(tmp_path: str | Path) -> Session:
    """构造 ephemeral Session（用 tmp_path 做 SessionStore 根目录）。

    测试用：把"构造 Session + tmp_path JsonlStore"压成一行，让现有测试
    能批量替换为 event-sourced 版本。
    """
    store = JsonlSessionStore(root=tmp_path)
    return Session.start(store)
