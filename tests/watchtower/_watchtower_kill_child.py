"""Hard-crash fixture for the production Watchtower → SessionService → Runtime path."""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from unittest.mock import patch

from langchain_core.messages import AIMessage

from agent_harness.agent.runtime import AgentRuntime
from agent_harness.config import Settings
from agent_harness.session.service import SessionService
from agent_harness.storage.checkpoint import CheckpointBoundary
from agent_harness.web.app import AppState, session_service
from tests.scripted_model import ScriptedModel

CRASH_EXIT_CODE = 91
PRE_SESSION_CRASH_EXIT_CODE = 92


class _DelayedScriptedModel(ScriptedModel):
    async def astream(self, *args, **kwargs):
        await asyncio.sleep(0.2)
        async for chunk in super().astream(*args, **kwargs):
            yield chunk


def _settings(root: Path) -> Settings:
    return Settings(
        _env_file=None,
        workspace_dir=str(root),
        model_api_key="sk-test",
        model_provider="deepseek",
        model_name="deepseek-chat",
        # Scale the production 300-second boundary down while keeping it enabled.
        run_disconnect_grace_seconds=0.05,
    )


def _model_factory(*_args, **_kwargs):
    return _DelayedScriptedModel([AIMessage(content="completed after recovery")])


async def _wait_for_terminal(state: AppState, session_id: str) -> list:
    from agent_harness.session.event import RUN_COMPLETED, RUN_FAILED, RUN_PAUSED

    for _ in range(3000):
        events = state.store.read_events(session_id)
        if any(event.type in {RUN_COMPLETED, RUN_FAILED, RUN_PAUSED} for event in events):
            return events
        await asyncio.sleep(0.01)
    raise TimeoutError("recovered Watchtower run did not reach a terminal or paused state")


async def _run(
    root: Path, *, crash_point: str | None, session_id: str,
) -> list | None:
    state = AppState(_settings(root))
    await state.ensure_stores()

    async def save_checkpoint(self, session, boundary_type):
        if crash_point == "after_message" and boundary_type is CheckpointBoundary.USER_ACCEPTED:
            # This is after the durable USER_MESSAGE append and before begin_run.
            os._exit(CRASH_EXIT_CODE)
        return await original_save_checkpoint(self, session, boundary_type)

    async def create_and_launch(self, *args, **kwargs):
        if crash_point == "before_session":
            # The durable dispatch intent already exists, but SessionService has
            # not created the planned session yet.
            os._exit(PRE_SESSION_CRASH_EXIT_CODE)
        return await original_create_and_launch(self, *args, **kwargs)

    original_save_checkpoint = AgentRuntime._save_checkpoint
    original_create_and_launch = SessionService.create_and_launch
    with (
        patch("agent_harness.assembly.create_chat_model", side_effect=_model_factory),
        patch.object(AgentRuntime, "_save_checkpoint", save_checkpoint),
        patch.object(SessionService, "create_and_launch", create_and_launch),
    ):
        await session_service(state).scan_interrupted()
        await state.watchtower.start()
        if crash_point is not None:
            from agent_harness.session.event import RUN_STARTED

            for _ in range(500):
                execution = await state.watchtower_store.get_execution(
                    "fire-process-kill",
                )
                events = state.store.read_events(session_id)
                if execution is not None and execution.state == "launch_failed":
                    raise RuntimeError(f"dispatch failed: {execution.error}")
                if any(event.type == RUN_STARTED for event in events):
                    raise RuntimeError("run started before the kill hook fired")
                await asyncio.sleep(0.01)
            raise TimeoutError("USER_ACCEPTED kill hook did not fire")
        try:
            return await _wait_for_terminal(state, session_id)
        finally:
            await state.shutdown()


if __name__ == "__main__":
    mode = sys.argv[2]
    crash_point = mode.removeprefix("crash-") if mode.startswith("crash-") else None
    requested_session_id = sys.argv[3]
    events = asyncio.run(
        _run(Path(sys.argv[1]), crash_point=crash_point, session_id=requested_session_id),
    )
    if crash_point is None:
        from agent_harness.session.event import RUN_STARTED, USER_MESSAGE

        print(
            sum(event.type == USER_MESSAGE for event in events),
            sum(event.type == RUN_STARTED for event in events),
            sum(event.type == "run/completed" for event in events),
        )
