"""#555 fork 崩溃注入子进程（house 模式：`os._exit(9)` 等价 SIGKILL，零清理）。

场景：fork 已过校验、child 文件已落 `fork/in-progress` 意图标记（fsync），
workspace 拷贝进行到一半时进程被杀——audit CHAOS-01 的 W2 窗口。子进程在
拷贝回调里直接 `os._exit`，跳过一切补偿/finally/flush 逻辑，只留 fsync 过的
磁盘现场。与 `_approval_kill_child.py` 同型：模块末行触发，常量不能被父进程
import（父进程抄一份 exit code）。
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
from pathlib import Path

CRASH_EXIT_CODE = 9


def main() -> None:
    payload = json.loads(sys.argv[1])
    root = Path(payload["root"])

    from agent_harness.sandbox.registry import WorkspaceRegistry
    from agent_harness.session import Session
    from agent_harness.session.event import (
        MODEL_COMPLETED,
        RUN_COMPLETED,
        RUN_STARTED,
        USER_MESSAGE,
    )
    from agent_harness.session.fork import fork_session
    from agent_harness.session.store import JsonlSessionStore
    from agent_harness.storage.sqlite import SqliteSessionMetaStore

    store = JsonlSessionStore(root=root / "sessions")
    registry = WorkspaceRegistry(root=root / "sandbox", backend="local")
    meta = SqliteSessionMetaStore(root / "harness.db")

    async def scenario() -> None:
        await meta.initialize()
        parent = Session.start(
            store, session_id="parent", workspace_registry=registry
        )
        parent.append(USER_MESSAGE, {"content": "第一条"})
        parent.append(RUN_STARTED, {})
        parent.append(MODEL_COMPLETED, {"content": "好的"})
        parent.append(RUN_COMPLETED, {})
        parent.append(USER_MESSAGE, {"content": "第二条"})
        (registry.default_workspace_root("parent") / "keep.txt").write_text(
            "父资产", encoding="utf-8"
        )

        def _crash_mid_copy(src, dst, **kwargs):
            # 拷出半份内容后硬杀：无补偿、无清理，磁盘只留 fsync 过的现场
            dst_path = Path(dst)
            dst_path.mkdir(parents=True, exist_ok=True)
            half = dst_path / "half.txt"
            half.write_text("半份", encoding="utf-8")
            with open(half, "ab") as handle:
                os.fsync(handle.fileno())
            os._exit(CRASH_EXIT_CODE)

        shutil.copytree = _crash_mid_copy
        await fork_session(
            store, meta, "parent",
            boundary_user_message_seq=parent.events[-1].seq,
            child_session_id="child", workspace_registry=registry,
        )

    asyncio.run(scenario())


if __name__ == "__main__":
    main()
