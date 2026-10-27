"""#1488: SessionArchive ownership and production shutdown paths."""

from __future__ import annotations

import asyncio

import pytest

from agent_harness.config import Settings
from agent_harness.session.event import SessionEvent
from agent_harness.web.app import AppState, create_app


def _settings(tmp_path) -> Settings:
    return Settings(
        _env_file=None,
        workspace_dir=str(tmp_path),
        model_api_key="sk-test",
        enable_cors=False,
    )


def _materialize_archive(state: AppState):
    state.store.append_event(
        "archive-lifecycle",
        SessionEvent(
            type="user/message", session_id="archive-lifecycle", seq=0,
            data={"content": "lifecycle"},
        ),
    )
    return state.session_archive_observer.archive


@pytest.mark.asyncio
async def test_app_state_shutdown_closes_owned_archive(tmp_path) -> None:
    state = AppState(_settings(tmp_path))
    archive = _materialize_archive(state)

    await state.shutdown()
    await state.shutdown()

    assert state.store._event_observer is None
    with pytest.raises(RuntimeError, match="closed"):
        archive.get_session("archive-lifecycle")


@pytest.mark.asyncio
async def test_lifespan_startup_failure_still_closes_archive(
    tmp_path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fail_ensure_stores(_self: AppState) -> None:
        raise RuntimeError("startup failure")

    monkeypatch.setattr(AppState, "ensure_stores", fail_ensure_stores)
    app = create_app(_settings(tmp_path), enable_cors=False)
    state = app.state.agent
    archive = _materialize_archive(state)

    with pytest.raises(RuntimeError, match="startup failure"):
        async with app.router.lifespan_context(app):
            pass

    assert state.store._event_observer is None
    with pytest.raises(RuntimeError, match="closed"):
        archive.get_session("archive-lifecycle")


@pytest.mark.asyncio
async def test_lifespan_startup_cancellation_still_closes_archive(
    tmp_path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def cancel_ensure_stores(_self: AppState) -> None:
        raise asyncio.CancelledError

    monkeypatch.setattr(AppState, "ensure_stores", cancel_ensure_stores)
    app = create_app(_settings(tmp_path), enable_cors=False)
    state = app.state.agent
    archive = _materialize_archive(state)

    with pytest.raises(asyncio.CancelledError):
        async with app.router.lifespan_context(app):
            pass

    assert state.store._event_observer is None
    with pytest.raises(RuntimeError, match="closed"):
        archive.get_session("archive-lifecycle")
