"""子进程入口：Provider 已进入后保持阻塞，由父测试执行真实硬杀。"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

from agent_harness.agent.runtime import AgentRuntime
from agent_harness.model.fallback import TwoLevelFallbackPolicy
from agent_harness.session import JsonlSessionStore, Session
from agent_harness.tooling import ToolExecutor, ToolRegistry


class _BlockedModel:
    def __init__(self, marker: Path, label: str = "called") -> None:
        self._marker = marker
        self._label = label

    def bind_tools(self, tools, **kwargs):
        return self

    async def ainvoke(self, messages, **kwargs):
        with self._marker.open("a", encoding="utf-8") as marker:
            marker.write(f"{self._label}\n")
            marker.flush()
            os.fsync(marker.fileno())
        await asyncio.Future()

    async def astream(self, messages, **kwargs):
        raise AssertionError("本测试使用 AgentRuntime.run 的 invoke 路径")
        yield


class _FailingModel:
    def __init__(self, marker: Path) -> None:
        self._marker = marker

    def bind_tools(self, tools, **kwargs):
        return self

    async def ainvoke(self, messages, **kwargs):
        with self._marker.open("a", encoding="utf-8") as marker:
            marker.write("primary-called\n")
            marker.flush()
            os.fsync(marker.fileno())
        raise TimeoutError("primary unavailable")

    async def astream(self, messages, **kwargs):
        raise AssertionError("本测试使用 AgentRuntime.run 的 invoke 路径")
        yield


async def main() -> None:
    config = json.loads(sys.argv[1])
    root = Path(config["root"])
    session = Session.start(
        JsonlSessionStore(root / "sessions"),
        session_id=config["session_id"],
    )
    registry = ToolRegistry()
    marker = Path(config["provider_marker"])
    with_fallback = config.get("with_fallback", False)
    runtime = AgentRuntime(
        model=_FailingModel(marker) if with_fallback else _BlockedModel(marker),
        registry=registry,
        executor=ToolExecutor(registry),
        max_agent_turns=1,
        fallback_model=(
            _BlockedModel(marker, "fallback-called") if with_fallback else None
        ),
        fallback_policy=TwoLevelFallbackPolicy() if with_fallback else None,
        primary_model_name="primary-model",
        fallback_model_name="fallback-model",
    )
    await runtime.run(session, "在途请求硬杀测试")


if __name__ == "__main__":
    asyncio.run(main())
