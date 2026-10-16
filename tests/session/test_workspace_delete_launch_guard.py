from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_harness.sandbox.registry import WorkspaceRegistry
from agent_harness.session.errors import ActiveRunConflict
from agent_harness.session.service import SessionService


class _RunManager:
    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.launch_calls = 0
        self.launch_error: BaseException | None = None

    def session_lock(self, _session_id: str) -> asyncio.Lock:
        return self.lock

    def launch(self, *_args, **_kwargs) -> tuple[object, object]:
        self.launch_calls += 1
        if self.launch_error is not None:
            raise self.launch_error
        return object(), object()


def test_launch_fails_closed_when_workspace_delete_marker_is_set(
    tmp_path: Path,
) -> None:
    session_id = "launch-owner"
    registry = WorkspaceRegistry(root=tmp_path, backend="local")
    registry._write_mapping(session_id, {
        "session_id": session_id,
        "backend": "local",
        "workspace_root": str(tmp_path / "workspace"),
        "workspace_delete_in_progress": True,
    })
    run_manager = _RunManager()
    service = object.__new__(SessionService)
    service._workspace_registry = registry
    service._run_manager = run_manager

    with pytest.raises(ActiveRunConflict, match="hard-deleted"):
        service._bind_and_launch(
            session=SimpleNamespace(session_id=session_id),
            runtime=object(),
            task="resume",
            user_input_metadata=None,
            interactive=False,
            approval_callback=None,
            fuse=None,
            launch_budget=None,
        )

    assert run_manager.launch_calls == 0


def test_launch_does_not_translate_unrelated_key_error(tmp_path: Path) -> None:
    session_id = "launch-owner"
    registry = WorkspaceRegistry(root=tmp_path, backend="local")
    registry._write_mapping(session_id, {
        "session_id": session_id,
        "backend": "local",
        "workspace_root": str(tmp_path / "workspace"),
    })
    run_manager = _RunManager()
    run_manager.launch_error = KeyError("launch callback key")
    service = object.__new__(SessionService)
    service._workspace_registry = registry
    service._run_manager = run_manager

    with pytest.raises(KeyError, match="launch callback key"):
        service._bind_and_launch(
            session=SimpleNamespace(session_id=session_id),
            runtime=object(),
            task="resume",
            user_input_metadata=None,
            interactive=False,
            approval_callback=None,
            fuse=None,
            launch_budget=None,
        )


def test_delete_holds_session_lock_through_hard_delete_body() -> None:
    async def exercise() -> None:
        run_manager = _RunManager()
        service = object.__new__(SessionService)
        service._run_manager = run_manager
        delete_entered = asyncio.Event()
        allow_delete = asyncio.Event()
        lock_held = asyncio.Event()
        release_lock = asyncio.Event()
        contender_attempting = asyncio.Event()
        contender_entered = asyncio.Event()

        async def delete_body(_session_id: str) -> str:
            delete_entered.set()
            await allow_delete.wait()
            return "deleted"

        service._delete_session_locked = delete_body

        async def try_resume_lock() -> None:
            contender_attempting.set()
            async with run_manager.lock:
                contender_entered.set()

        async def hold_resume_lock() -> None:
            async with run_manager.lock:
                lock_held.set()
                await release_lock.wait()

        holder = asyncio.create_task(hold_resume_lock())
        await lock_held.wait()
        delete = asyncio.create_task(service.delete_session("same-session"))
        await asyncio.sleep(0)
        assert not delete_entered.is_set()

        release_lock.set()
        await holder
        await delete_entered.wait()
        contender = asyncio.create_task(try_resume_lock())
        await contender_attempting.wait()
        assert not contender_entered.is_set()

        allow_delete.set()

        assert await delete == "deleted"
        assert delete_entered.is_set()
        await contender_entered.wait()
        await contender

    asyncio.run(exercise())
