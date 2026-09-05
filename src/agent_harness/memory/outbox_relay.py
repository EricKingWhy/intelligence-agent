"""进程内 outbox relay；失败保留任务，下一轮重新 upsert。"""

import asyncio
import logging
from contextlib import suppress

from agent_harness.memory.record_store import MemoryOutbox, PendingMemory
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
        # 计数对应的 outbox revision：内容被重新 store（revision 更新）时重置
        # 重试预算——新版本不是旧毒丸的证据，死信不能跨版本生效（否则故障窗口
        # 期间写入的记忆 + 其后所有更新都静默永不索引，直到进程重启）。
        self._failure_revisions: dict[str, str] = {}

    async def flush(self) -> int:
        async with self._lock:
            count = 0
            after_id = ""
            while page := await self._records.pending(after_id=after_id):
                for change in page:
                    if self._failure_revisions.get(change.entry.id) != change.revision:
                        # revision 变化（含首次出现）：重置该条目的连续失败计数。
                        self._failure_counts.pop(change.entry.id, None)
                    self._failure_revisions[change.entry.id] = change.revision
                    if self._failure_counts.get(change.entry.id, 0) >= self.MAX_CONSECUTIVE_FAILURES:
                        continue
                    token = memory_session_var.set(change.session_id)
                    try:
                        await self._vectors.upsert(change.entry.id, change.entry.content,
                                                   {**change.entry.metadata, "scope": change.entry.scope.value},
                                                   change.identity)
                    except Exception as error:  # noqa: BLE001 — 保留 durable outbox，下轮重试。
                        self._count_failure(change, "vector", error)
                    else:
                        # ack（SQLite 写）失败与 vector 失败分开归因，但仍计入连续失败：
                        # ack 持续失败的条目必须死信，不能靠每轮重复 upsert 空转（毒丸）。
                        try:
                            count += await self._records.acknowledge(change)
                        except Exception as error:  # noqa: BLE001 — 保留 durable outbox，下轮重试。
                            self._count_failure(change, "ack", error)
                        else:
                            self._failure_counts.pop(change.entry.id, None)
                            self._failure_revisions.pop(change.entry.id, None)
                    finally:
                        memory_session_var.reset(token)
                after_id = page[-1].entry.id
            return count

    def _count_failure(self, change: PendingMemory, stage: str, error: Exception) -> None:
        """按失败阶段（vector/ack）归因记录；连续 MAX 次后死信跳过，outbox 行保留。"""
        failures = self._failure_counts.get(change.entry.id, 0) + 1
        self._failure_counts[change.entry.id] = failures
        detail = f"{stage}: {type(error).__name__}: {error}"
        if failures >= self.MAX_CONSECUTIVE_FAILURES:
            logger.error("Memory index sync abandoned after %d consecutive failures (%s); "
                         "record %s stays in outbox (indexed=false)", failures, detail, change.entry.id)
        else:
            logger.warning("Memory index sync deferred (%s); outbox retained", detail)

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
