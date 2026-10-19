"""#1353 AC2 子进程 worker（不被 pytest 收集）。

用真实子进程验证 fcntl 锁 / 原子替换 / SIGKILL 崩溃后的文件完整性。
"""

from __future__ import annotations

import argparse
import asyncio
import inspect
import os
import pathlib
import signal
from pathlib import Path

# 子进程的 import 解析由 `_worker_env()` 设置的 PYTHONPATH 提供（见
# test_1353_ac2_concurrent_crash.py），不在此处改 sys.path——那会污染同进程其它测试。
from tests.memory.v2._markdown_memory_1353 import (
    build_store,
    call,
    load,
)


async def _build(root: Path):
    store = build_store(root)
    pending = load(store)
    if inspect.isawaitable(pending):
        await pending
    return store


async def _add_many(root: Path, prefix: str, count: int) -> None:
    store = await _build(root)
    for index in range(count):
        result = await call(store.add, "memory", f"{prefix}-{index:03d}")
        if not result.get("success"):
            raise SystemExit(f"add failed: {result}")


async def _loop_add(root: Path, prefix: str) -> None:
    store = await _build(root)
    index = 0
    while True:
        await call(store.add, "memory", f"{prefix}-{index:04d}")
        index += 1


async def _crash_before_replace(root: Path) -> None:
    store = await _build(root)

    def _die(*_args, **_kwargs):
        # Windows 没有 SIGKILL，但 crash-before-replace 用例在那边本就跳过（#1384）；
        # 回退分支不可达且会把「响亮失败」变成静默自杀，直接让这里 AttributeError。
        os.kill(os.getpid(), signal.SIGKILL)

    os.replace = _die
    pathlib.Path.replace = _die
    await call(store.add, "memory", "must-not-land")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True)
    parser.add_argument("--mode", required=True)
    parser.add_argument("--prefix", default="entry")
    parser.add_argument("--count", type=int, default=1)
    args = parser.parse_args()
    root = Path(args.root)

    if args.mode == "add":
        asyncio.run(_add_many(root, args.prefix, args.count))
    elif args.mode == "loop":
        asyncio.run(_loop_add(root, args.prefix))
    elif args.mode == "crash-before-replace":
        asyncio.run(_crash_before_replace(root))
    else:
        raise SystemExit(f"unknown mode: {args.mode}")


if __name__ == "__main__":
    main()
