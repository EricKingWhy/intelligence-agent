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
                candidates = await self._extractor.extract(events)
                stored, failed = 0, 0
                for scope, content, metadata in candidates:
                    try:
                        await self._capability.store(scope, content, metadata)
                        stored += 1
                    except Exception as item_error:  # noqa: BLE001 — 单候选失败隔离
                        # R7-4：逐候选隔离——第 N 个失败不再吞掉其余候选
                        #（此前顺序写、一个失败全部丢失，且 LLM 抽取不重跑）。
                        failed += 1
                        logger.warning(
                            "Memory candidate store failed (%s/%d): %s",
                            failed, len(candidates), type(item_error).__name__,
                        )
                if failed:
                    # 部分失败诚实记录：数量 + 阶段可观察（类型名脱敏，与
                    # writeback 脱敏不变量一致）。已成功的候选不受影响。
                    session.append(
                        MEMORY_DEGRADED,
                        {"operation": "writeback",
                         "reason": f"partial: {failed}/{len(candidates)} candidates failed"},
                        run_id=next((e.run_id for e in events if e.run_id), None),
                    )
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

    async def close(self, *, drain_timeout_seconds: float = 10.0) -> None:
        """收尾：先 drain 在途 writeback（等它们写完），超时才取消（R3-2）。

        此前直接 cancel——已抽取未存储的候选静默丢失。drain 有界：超时后
        仍然取消（进程退出不能被无限挂住），被取消的丢失是显式权衡。
        """
        tasks = list(self._tasks)
        if not tasks:
            return
        try:
            async with asyncio.timeout(drain_timeout_seconds):
                await asyncio.gather(*tasks, return_exceptions=True)
            return
        except TimeoutError:
            logger.warning("Memory writeback drain timed out; cancelling pending tasks")
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
