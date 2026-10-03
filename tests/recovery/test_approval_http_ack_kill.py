"""#553：真实 HTTP 200 后硬杀，重启必须恢复已确认的审批决策。"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

from agent_harness.config import Settings
from agent_harness.session import JsonlSessionStore
from agent_harness.session.event import PERMISSION_RESOLVED, TOOL_APPROVAL_REQUESTED
from agent_harness.storage import SqliteOperationLedger
from agent_harness.web.app import create_app

_REPO_ROOT = Path(__file__).resolve().parents[2]
_CHILD = Path(__file__).with_name("_approval_http_ack_kill_child.py")


async def _read_child_line(process: asyncio.subprocess.Process) -> str:
    assert process.stdout is not None
    line = await asyncio.wait_for(process.stdout.readline(), timeout=20)
    if not line:
        assert process.stderr is not None
        stderr = (await process.stderr.read()).decode("utf-8", errors="replace")
        raise AssertionError(
            "barrier child exited before producing the requested signal "
            f"(rc={process.returncode}): {stderr}"
        )
    return line.decode("utf-8").rstrip("\r\n")


@pytest.mark.asyncio
async def test_http_200_approval_survives_barrier_kill_and_startup_recovery(
    tmp_path: Path,
) -> None:
    """屏障精确卡在 queue Future 已返回、审批 callback 尚未继续的位置。"""
    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        str(_CHILD),
        json.dumps({"root": str(tmp_path)}),
        cwd=_REPO_ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    session_id = approval_id = None
    try:
        ready = await _read_child_line(process)
        assert ready.startswith("READY:"), ready
        port = int(ready.partition(":")[2])

        async with httpx.AsyncClient(timeout=None) as client, client.stream(
            "POST",
            f"http://127.0.0.1:{port}/api/sessions",
            json={"task": "等待一次高危工具审批", "permission_mode": "workspace-write"},
        ) as response:
            assert response.status_code == 200
            async for line in response.aiter_lines():
                if not line.startswith("data:"):
                    continue
                frame = json.loads(line.removeprefix("data:").strip())
                session_id = session_id or frame.get("session_id")
                if frame.get("type") == TOOL_APPROVAL_REQUESTED:
                    approval_id = frame["data"]["approval_id"]
                    break
        assert session_id and approval_id, "must observe a durable approval request"

        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.post(
                f"http://127.0.0.1:{port}/api/sessions/{session_id}/approve",
                json={
                    "approval_id": approval_id,
                    "approved": True,
                    "reason": "barrier+kill acceptance",
                },
            )
            assert response.status_code == 200, response.text
            assert response.json()["status"] == "resolved"

        assert await _read_child_line(process) == f"BARRIER:{approval_id}"

        # 独立 Store 在硬杀前读取，钉住真实 200 对应的磁盘事实。
        store = JsonlSessionStore(tmp_path / "sessions")
        at_ack = store.read_events(session_id)
        assert any(
            event.type == TOOL_APPROVAL_REQUESTED
            and event.data.get("approval_id") == approval_id
            for event in at_ack
        )
        ack_decisions = [
            event.data["decision"]
            for event in at_ack
            if event.type == PERMISSION_RESOLVED
            and event.data.get("approval_id") == approval_id
        ]

        # 硬杀发生在真实 200 已收到、run callback 仍被屏障拦住之后。
        process.kill()
        await asyncio.wait_for(process.wait(), timeout=10)

        # 新 AppState/lifespan 执行真实启动扫描与恢复，不复用崩溃进程内存。
        restart_app = create_app(Settings(
            _env_file=None,
            workspace_dir=str(tmp_path),
            model_api_key="sk-test",
            enable_cors=False,
        ))
        async with restart_app.router.lifespan_context(restart_app):
            transport = httpx.ASGITransport(app=restart_app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://restart.test"
            ) as client:
                stale = await client.post(
                    f"/api/sessions/{session_id}/approve",
                    json={"approval_id": approval_id, "approved": False},
                )
                unknown = await client.post(
                    f"/api/sessions/{session_id}/approve",
                    json={"approval_id": "unknown-id", "approved": False},
                )
                assert stale.status_code == 409
                assert unknown.status_code == 404

        recovered = store.read_events(session_id)
        recovered_decisions = [
            event.data["decision"]
            for event in recovered
            if event.type == PERMISSION_RESOLVED
            and event.data.get("approval_id") == approval_id
        ]
        ledger = SqliteOperationLedger(tmp_path / "harness.db")
        await ledger.initialize()
        operation = await ledger.get(session_id, "call-approval-http-ack")
        marker_exists = (tmp_path / "tool-executed.txt").exists()

        observed = (ack_decisions, recovered_decisions, operation, marker_exists)
        expected = (["approve_once"], ["approve_once"], None, False)
        assert observed == expected, (
            "HTTP 200 must be backed by a durable approval before ack; startup recovery "
            "must preserve it without replaying the tool. "
            f"Observed {observed!r}"
        )
    finally:
        if process.returncode is None:
            process.kill()
            await asyncio.wait_for(process.wait(), timeout=10)
