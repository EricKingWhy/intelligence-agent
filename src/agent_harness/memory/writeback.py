"""Owned background tasks for run-end extraction and capability writes."""

import asyncio
import logging
from typing import Any

from agent_harness.memory.capability import MemoryCapability
from agent_harness.memory.types import memory_session_var
from agent_harness.session import Session, SessionEvent
from agent_harness.session.event import MEMORY_DEGRADED

logger = logging.getLogger("agent_harness.memory")


class MemoryWriteback:
    def __init__(self, capability: MemoryCapability, extractor: Any, timeout_seconds: float = 30.0) -> None:
        self._capability = capability
        self._extractor = extractor
        self._timeout = timeout_seconds
        self._tasks: set[asyncio.Task] = set()

    def submit(self, session: Session, events: list[SessionEvent]) -> None:
        # create_task snapshots the request IdentityContext before middleware resets it.
        task = asyncio.create_task(self._write(session, list(events)))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _write(self, session: Session, events: list[SessionEvent]) -> None:
        token = memory_session_var.set(session.session_id)
        try:
            async with asyncio.timeout(self._timeout):
                for scope, content, metadata in await self._extractor.extract(events):
                    await self._capability.store(scope, content, metadata)
        except Exception as error:
            # 根因（类型 + 消息 + 堆栈）只进日志；事件 reason 仅带异常类型名——
            # 原始异常消息可能含密钥，不得泄入事件流（见 redaction 测试）。
            logger.exception("Memory writeback failed")
            try:
                session.append(MEMORY_DEGRADED,
                               {"operation": "writeback", "reason": f"unavailable: {type(error).__name__}"},
                               run_id=next((e.run_id for e in events if e.run_id), None))
            except Exception:  # noqa: BLE001 — persistence can also be unavailable.
                logger.warning("Memory degradation event persistence unavailable")
        finally:
            memory_session_var.reset(token)

    async def drain(self) -> None:
        if self._tasks:
            await asyncio.gather(*list(self._tasks))

    async def close(self) -> None:
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
