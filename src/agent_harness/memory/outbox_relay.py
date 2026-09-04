"""进程内 outbox relay；失败保留任务，下一轮重新 upsert。"""

import asyncio
import logging
from contextlib import suppress

from agent_harness.memory.record_store import MemoryOutbox
from agent_harness.memory.types import memory_session_var
from agent_harness.memory.vector_store import VectorIndexStore

logger = logging.getLogger(__name__)


class OutboxRelay:
    """SQLite 权威记录 → 向量索引的进程内同步。

    瞬态失败（网络/限流）按 ADR-0008 保留 outbox、下轮重试；持续失败的
    毒丸条目在连续 MAX_CONSECUTIVE_FAILURES 次后进入本进程死信：不再重试
    （避免每轮空转与告警噪音），outbox 行保留、indexed=FALSE 可观察。
    计数器在进程内存中——重启自愈（如 schema 修复后重新获得重试机会）。
    """

    MAX_CONSECUTIVE_FAILURES = 5

    def __init__(self, record_store: MemoryOutbox, vector_store: VectorIndexStore,
                 poll_interval_seconds: float = 5.0) -> None:
        if poll_interval_seconds <= 0:
            raise ValueError("poll_interval_seconds must be positive")
        self._records = record_store
        self._vectors = vector_store
        self._interval = poll_interval_seconds
        self._task: asyncio.Task | None = None
        self._lock = asyncio.Lock()
        self._failure_counts: dict[str, int] = {}

    async def flush(self) -> int:
        async with self._lock:
            count = 0
            after_id = ""
            while page := await self._records.pending(after_id=after_id):
                for change in page:
                    if self._failure_counts.get(change.entry.id, 0) >= self.MAX_CONSECUTIVE_FAILURES:
                        continue
                    token = memory_session_var.set(change.session_id)
                    try:
                        await self._vectors.upsert(change.entry.id, change.entry.content,
                                                   {**change.entry.metadata, "scope": change.entry.scope.value},
                                                   change.identity)
                        self._failure_counts.pop(change.entry.id, None)
                        count += await self._records.acknowledge(change)
                    except Exception:  # noqa: BLE001 — 保留 durable outbox，下轮重试，不记录 SDK 原始异常。
                        failures = self._failure_counts.get(change.entry.id, 0) + 1
                        self._failure_counts[change.entry.id] = failures
                        if failures >= self.MAX_CONSECUTIVE_FAILURES:
                            logger.error("Memory index sync abandoned after %d consecutive failures; "
                                         "record %s stays in outbox (indexed=false)", failures, change.entry.id)
                        else:
                            logger.warning("Memory index sync deferred; outbox retained")
                    finally:
                        memory_session_var.reset(token)
                after_id = page[-1].entry.id
            return count

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run())

    async def _run(self) -> None:
        while True:
            try:
                await self.flush()
            except Exception:  # noqa: BLE001 — SQLite 暂时不可用，下轮重试。
                logger.warning("Memory outbox unavailable; retrying later")
            await asyncio.sleep(self._interval)

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
            self._task = None
