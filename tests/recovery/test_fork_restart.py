"""#555 kill/restart：fork 拷贝中途真崩溃 → 启动扫描回收残留、现场可读。

CHAOS-01 的完整链条（真崩溃，不是模拟异常）：

- 崩溃现场（durable）：child 文件 = `session/started` + `fork/in-progress`（无
  `session/forked`）；暂存目录里有半份父 workspace 拷贝；child 默认工作区是
  空壳；父文件与父资产完好。修复前的 main 上，半份拷贝直接落在 child 登记
  路径里、无任何意图标记，扫描与对账都无法判定"这是个没完成的 fork"。
- 恢复：`scan_unfinished_forks` 按标记回收暂存与子工作区（harness 自建工件
  白名单），child 的 JSONL 与映射保留——「fork 未完成」继续可读，续聊对账
  给 fork 专属指引；重扫幂等。

HTTP/CLI 面不在此处；本文件只钉 durable 判定与"重启之后现场是否可解释"。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from agent_harness.recovery.scan import scan_unfinished_forks
from agent_harness.sandbox.registry import WorkspaceRegistry
from agent_harness.session.store import JsonlSessionStore

pytestmark = pytest.mark.asyncio

_REPO_ROOT = Path(__file__).resolve().parents[2]
_CHILD = Path(__file__).with_name("_fork_kill_child.py")

#: 与子进程的 `CRASH_EXIT_CODE` 一致。子进程 import 即执行（模块末行触发），
#: 所以常量只能抄一份，不能 import 它。
_CRASH_EXIT_CODE = 9

PARENT_ID = "parent"
CHILD_ID = "child"


def _crash(tmp_path: Path) -> None:
    """跑一次"fork 拷贝中途真崩溃"的子进程，并校验退出码前提。"""
    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    completed = subprocess.run(
        [sys.executable, str(_CHILD), json.dumps({"root": str(tmp_path)})],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
        cwd=str(_REPO_ROOT),
        check=False,
    )
    assert completed.returncode == _CRASH_EXIT_CODE, (
        f"子进程应以 os._exit({_CRASH_EXIT_CODE}) 崩溃，"
        f"实际 {completed.returncode}\nstderr: {completed.stderr[-2000:]}"
    )


async def test_fork_crash_residue_reclaimed_on_startup_scan(tmp_path: Path) -> None:
    _crash(tmp_path)
    store = JsonlSessionStore(root=tmp_path / "sessions")
    registry = WorkspaceRegistry(root=tmp_path / "sandbox", backend="local")

    # ── 崩溃现场前提（durable）：标记在场、暂存半份拷贝在场、子工作区是空壳 ──
    child_types = [e.type for e in store.read_events(CHILD_ID)]
    assert child_types == ["session/started", "fork/in-progress"]
    intent = store.read_events(CHILD_ID)[1]
    assert intent.data["parent_session_id"] == PARENT_ID
    staging = registry.fork_staging_root()
    assert staging.is_dir() and any(staging.iterdir()), "暂存目录应留下半份拷贝"
    child_workspace = registry.default_workspace_root(CHILD_ID)
    assert child_workspace.is_dir(), "registry.create 的空壳工作区应在场"
    assert not any(child_workspace.iterdir()), "发布未发生：子工作区应保持空壳"
    parent_bytes_before = store._events_path(PARENT_ID).read_bytes()

    results = await scan_unfinished_forks(
        session_store=store, workspace_registry=registry
    )

    # ── 恢复：回收 harness 自建工件，现场事实保留 ──
    assert [r.session_id for r in results] == [CHILD_ID]
    assert results[0].reclaimed and results[0].detail is None
    assert not staging.exists(), "暂存半份拷贝应被整体回收"
    assert not child_workspace.exists(), "未完成 child 的默认工作区应被回收"
    assert [e.type for e in store.read_events(CHILD_ID)] == child_types, (
        "child JSONL 是「fork 未完成」的可读事实，不因回收被抹掉"
    )
    assert registry.exists(CHILD_ID), "映射文件保留（可见性事实）"
    # 父侧零改动（§7 父不可改）：文件逐字节不变、workspace 资产完好
    assert store._events_path(PARENT_ID).read_bytes() == parent_bytes_before
    assert (
        registry.default_workspace_root(PARENT_ID) / "keep.txt"
    ).read_text(encoding="utf-8") == "父资产"

    # ── 幂等：重扫如实再报（child 仍是未完成 fork），但无新残留可回收 ──
    again = await scan_unfinished_forks(
        session_store=store, workspace_registry=registry
    )
    assert [r.session_id for r in again] == [CHILD_ID]
    assert again[0].reclaimed
