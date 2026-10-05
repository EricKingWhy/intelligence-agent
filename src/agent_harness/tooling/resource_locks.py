"""Resource Lock Registry：为执行域提供跨 executor 实例的共享互斥。

#525 一期（IMP-14）：
同一 Tool 实例（跨工具调用批次 / 跨 SubAgent executor 实例）对同一 resource key
的并发执行必须串行——这是 MUTATING 工具原子性的最简保障。

锁注册表由 assembly 构造时注入，所有 ToolExecutor（父 + SubAgent）共享同一实例，
这样父 Agent + 并发 SubAgent 的 executor 能对同一个 resource key 产生跨 runtime 互斥。

Key 格式：Tool 负责声明（如 "workspace-file:<normalized-path>"）；
锁消费者只管 acquire/release，不解释 key 语义。

死锁防护：acquire 按 key 升序排序后再逐个 await，同一时刻所有调用方按
相同顺序竞争相同 key，无环路（类比哲学家就餐的全局资源排序）。
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Sequence

# asyncio.Lock() 在 Python 3.10+ 不绑定事件循环，直接创建即可。


class ResourceLockRegistry:
    """进程内共享的 resource → asyncio.Lock 映射。

    所有 ToolExecutor（父 + SubAgent）共享同一实例，按字母序 acquire
    相同 key 的调用方会严格串行。
    """

    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = {}

    def _lock_for(self, key: str) -> asyncio.Lock:
        existing = self._locks.get(key)
        if existing is not None:
            return existing
        # setdefault 原子：GIL 保护 dict 的 check-then-insert；
        # 竞争者各得到同一把锁，互斥正确。
        return self._locks.setdefault(key, asyncio.Lock())

    @staticmethod
    def _sorted_unique(keys: list[str]) -> list[str]:
        return sorted(set(keys))

    async def acquire_all(self, keys: Sequence[str]) -> list[asyncio.Lock]:
        """按 key 升序 acquire 并返回锁列表（caller 在 finally 中释放）。"""
        ordered = self._sorted_unique(list(keys))
        acquired: list[asyncio.Lock] = []
        try:
            for key in ordered:
                lock = self._lock_for(key)
                await lock.acquire()
                acquired.append(lock)
            return acquired
        except BaseException:
            # 异常时逆序释放已获取的锁（避免死锁）
            for lock in reversed(acquired):
                lock.release()
            raise

    def release_all(self, locks: list[asyncio.Lock]) -> None:
        """逆序释放锁（配合 acquire_all 的有序获取）。"""
        for lock in reversed(locks):
            lock.release()

    @asynccontextmanager
    async def hold(self, keys: Sequence[str]) -> AsyncIterator[None]:
        """上下文管理器：acquire keys 并在 exit 时自动 release。

        用法：
            async with registry.hold(["workspace-file:foo.py"]):
                await tool.execute(...)
        """
        locks = await self.acquire_all(keys)
        try:
            yield
        finally:
            self.release_all(locks)
