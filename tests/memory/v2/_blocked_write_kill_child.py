"""Child process for the #1461 hard-kill test of the blocked-write path (AC7①).

The kill window is *after* `BlockedWriteStore.insert_in` returned but *before*
`commit_with_outcome` commits: the row exists only inside an open transaction, so a
real SIGKILL loses it together with the job's terminal state. Recovery must then
produce exactly one blocked row — never zero (silent drop) and never two (duplicate).

Same shape as `_protected_fact_extraction_kill_child.py`: the parent prints-barrier
handshake is the only thing this process does that the parent can observe.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from agent_harness.memory.v2.capability import MemoryV2Service
from agent_harness.memory.v2.executor import MemoryJobExecutor, MemoryModelStage
from agent_harness.memory.v2.index import InMemoryMemoryV2Index
from agent_harness.memory.v2.jobs import SqliteMemoryV2JobStore
from agent_harness.memory.v2.roles import MemoryModelRoles
from agent_harness.memory.v2.store import SqliteMemoryV2Store
from agent_harness.memory.v2.write_approval import BlockedWriteStore
from agent_harness.model.config import ModelConfig
from agent_harness.session.store import JsonlSessionStore


class _Invoker:
    """One candidate citing `e1` (the projection alias for the run's user message)."""

    async def __call__(self, call) -> str:
        if call.stage is MemoryModelStage.FORMATION:
            return json.dumps({
                "decision": "CANDIDATES",
                "candidates": [{
                    "kind": "semantic", "tier": "collection", "scope": "user_global",
                    "content": "用户偏好用 pnpm 安装依赖",
                    "payload": {"kind": "semantic", "subject": "包管理器",
                                "fact": "偏好 pnpm", "category": "preference"},
                    "importance": 0.8, "strength": 0.9,
                    "evidence": [{"event_id": "e1", "role": "user", "excerpt": "请用 pnpm"}],
                    "sensitivity": "ordinary",
                }],
                "skip_reason": None,
            }, ensure_ascii=False)
        if call.stage is MemoryModelStage.ADJUDICATION:
            return json.dumps({"results": [{
                "action": "ADD", "target_memory_id": None, "reason_code": "durable_new",
                "result": {
                    "kind": "semantic", "tier": "collection", "scope": "user_global",
                    "content": "用户偏好用 pnpm 安装依赖",
                    "payload": {"kind": "semantic", "subject": "包管理器",
                                "fact": "偏好 pnpm", "category": "preference"},
                    "importance": 0.8, "strength": 0.9,
                    "evidence": [{"event_id": "e1", "role": "user", "excerpt": "请用 pnpm"}],
                },
            }]}, ensure_ascii=False)
        raise AssertionError(f"unexpected model stage: {call.stage.value}")


async def main() -> None:
    root = Path(sys.argv[1])
    session_id = sys.argv[2]
    database = root / "memory-v2.db"
    jobs = SqliteMemoryV2JobStore(database)
    await jobs.initialize()
    # Default lease: every mid-run ownership check (`transition`, `commit_with_outcome`)
    # requires an unexpired lease held by this worker. The parent steals it by claiming
    # with `now=+1h` (same trick as the protected-fact kill child).
    job = await jobs.claim(worker_id="dead-worker")
    if job is None:
        raise AssertionError("the prepared durable job must be claimable")

    service = MemoryV2Service(SqliteMemoryV2Store(database), InMemoryMemoryV2Index())
    blocked = BlockedWriteStore(database, service)
    await blocked.initialize()
    service.attach_blocked(blocked)

    original = blocked.insert_in

    async def stop_after_insert(*args, **kwargs):
        result = await original(*args, **kwargs)
        print("BLOCKED_ROW_WRITTEN", flush=True)
        await asyncio.Future()
        return result

    # Instance attribute: this process's executor must see the barrier, and the
    # parent's recovery run (a different process) must not.
    blocked.insert_in = stop_after_insert  # type: ignore[method-assign]

    model = ModelConfig(
        provider="senseaudio",
        model_name="unit-model",
        api_key="unit-test-key",
        base_url="http://localhost:1",
        temperature=0.0,
    )
    sessions = JsonlSessionStore(root / "sessions")
    executor = MemoryJobExecutor(
        jobs=jobs, writer=service, searcher=service, invoker=_Invoker(),
        session_store=sessions, blocked=blocked,
    )
    await executor.run(
        job,
        worker_id="dead-worker",
        run_events=sessions.read_events(session_id),
        roles=MemoryModelRoles(primary=model, fallback=None),
        write_approval=True,
    )


if __name__ == "__main__":
    asyncio.run(main())
