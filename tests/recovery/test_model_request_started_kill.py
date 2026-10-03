"""#604：真实 Provider 在途硬杀后，启动恢复保留 unmatched request start。"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path
from uuid import uuid4

import pytest

from agent_harness.session import (
    MODEL_COMPLETED,
    MODEL_FAILED,
    MODEL_REQUEST,
    MODEL_REQUEST_STARTED,
    RUN_INTERRUPTED,
    JsonlSessionStore,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_CHILD = Path(__file__).with_name("_model_request_kill_child.py")


@pytest.mark.asyncio
async def test_hard_kill_preserves_unmatched_start_without_resubmitting_provider(
    tmp_path: Path,
) -> None:
    session_id = f"model-request-kill-{uuid4().hex}"
    marker = tmp_path / "provider-calls.txt"
    config = json.dumps({
        "root": str(tmp_path),
        "session_id": session_id,
        "provider_marker": str(marker),
    })
    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    process = await asyncio.create_subprocess_exec(
        sys.executable, str(_CHILD), config,
        cwd=_REPO_ROOT,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    store = JsonlSessionStore(tmp_path / "sessions")
    deadline = time.monotonic() + 30
    try:
        while time.monotonic() < deadline:
            events = store.read_events(session_id)
            started = [event for event in events if event.type == MODEL_REQUEST_STARTED]
            if started and marker.exists():
                break
            if process.returncode is not None:
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(), timeout=5,
                )
                raise AssertionError(
                    f"child exited before reaching Provider barrier: "
                    f"rc={process.returncode}, stdout={stdout!r}, stderr={stderr!r}"
                )
            await asyncio.sleep(0.02)
        else:
            raise AssertionError("child did not durably start a Provider request")

        # Marker is fsynced from inside ainvoke, after the durable start callback.
        assert marker.read_text(encoding="utf-8") == "called\n"
        process.kill()
        await asyncio.wait_for(process.wait(), timeout=10)
        await asyncio.wait_for(process.communicate(), timeout=5)
        assert process.returncode not in (None, 0), "child 必须由父测试硬杀"
    finally:
        if process.returncode is None:
            process.kill()
            await asyncio.wait_for(process.wait(), timeout=10)
            await asyncio.wait_for(process.communicate(), timeout=5)

    crashed = store.read_events(session_id)
    starts = [event for event in crashed if event.type == MODEL_REQUEST_STARTED]
    assert len(starts) == 1
    assert not [event for event in crashed if event.type == MODEL_REQUEST]
    assert not [event for event in crashed if event.type == MODEL_COMPLETED]
    assert not [event for event in crashed if event.type == MODEL_FAILED]
    assert starts[0].data["request_id"]

    # A fresh application lifespan performs the real startup scan/recovery path.
    from agent_harness.config import Settings
    from agent_harness.web.app import create_app

    restart_app = create_app(Settings(
        _env_file=None,
        workspace_dir=str(tmp_path),
        model_api_key="sk-test",
        enable_cors=False,
    ))
    async with restart_app.router.lifespan_context(restart_app):
        recovered = store.read_events(session_id)

    recovered_starts = [
        event for event in recovered if event.type == MODEL_REQUEST_STARTED
    ]
    assert len(recovered_starts) == 1
    assert recovered_starts[0].data["request_id"] == starts[0].data["request_id"]
    assert any(event.type == RUN_INTERRUPTED for event in recovered)
    assert not [event for event in recovered if event.type == MODEL_REQUEST]
    assert not [event for event in recovered if event.type == MODEL_COMPLETED]
    assert not [event for event in recovered if event.type == MODEL_FAILED]
    assert marker.read_text(encoding="utf-8") == "called\n", (
        "startup recovery must not resubmit an unknown Provider request"
    )
