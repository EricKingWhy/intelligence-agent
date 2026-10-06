"""Child process for hard-kill tests of protected-fact extraction recovery."""

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
from agent_harness.model.config import ModelConfig
from agent_harness.session.store import JsonlSessionStore


class _Invoker:
    def __init__(self) -> None:
        self.extraction_output = json.dumps({
            "candidates": [{
                "source": "u0",
                "value": "\u8bf7\u4ee5\u540e\u90fd\u7528 pnpm \u88c5\u4f9d\u8d56",
            }],
        })

    async def __call__(self, call) -> str:
        if call.stage is MemoryModelStage.PROTECTED_FACT_EXTRACTION:
            return self.extraction_output
        if call.stage is MemoryModelStage.FORMATION:
            return json.dumps({
                "decision": "NO_MEMORY",
                "candidates": [],
                "skip_reason": "no_durable_value",
            })
        raise AssertionError(f"unexpected model stage: {call.stage.value}")


async def main() -> None:
    root = Path(sys.argv[1])
    case = sys.argv[2]
    session_id = sys.argv[3]
    database = root / "memory-v2.db"
    jobs = SqliteMemoryV2JobStore(database)
    await jobs.initialize()
    job = await jobs.claim(worker_id="dead-worker")
    if job is None:
        raise AssertionError("the prepared durable job must be claimable")

    if case == "after-candidates-saved":
        original = jobs.save_protected_fact_candidates

        async def stop_after_save(**kwargs):
            result = await original(**kwargs)
            print("CANDIDATES_SAVED", flush=True)
            await asyncio.Future()
            return result

        jobs.save_protected_fact_candidates = stop_after_save
    elif case == "after-fact-appended":
        original = jobs.finish_protected_fact_extraction

        async def stop_before_finish(**kwargs):
            print("FACT_APPENDED", flush=True)
            await asyncio.Future()
            return await original(**kwargs)

        jobs.finish_protected_fact_extraction = stop_before_finish
    else:
        raise ValueError(f"unknown kill case: {case}")

    sessions = JsonlSessionStore(root / "sessions")
    invoker = _Invoker()
    model = ModelConfig(
        provider="senseaudio",
        model_name="unit-model",
        api_key="unit-test-key",
        base_url="http://localhost:1",
        temperature=0.0,
    )
    executor = MemoryJobExecutor(
        jobs=jobs,
        writer=MemoryV2Service(
            SqliteMemoryV2Store(database), InMemoryMemoryV2Index(),
        ),
        searcher=MemoryV2Service(
            SqliteMemoryV2Store(database), InMemoryMemoryV2Index(),
        ),
        invoker=invoker,
        session_store=sessions,
    )
    events = sessions.read_events(session_id)
    await executor.run(
        job,
        worker_id="dead-worker",
        run_events=events,
        roles=MemoryModelRoles(primary=model, fallback=None),
    )


if __name__ == "__main__":
    asyncio.run(main())
