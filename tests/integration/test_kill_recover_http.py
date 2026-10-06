"""R20 全环（#357 W-13）：真实 kill → 启动扫描 → 409（含展示字段）→ decisions → 200。

真实 Python 子进程在 ``running`` 注入点 os._exit（Ledger 行 RUNNING、tool/call
悬空、副作用未发生），父进程以真实 Web 组合根（``create_app`` + lifespan 启动
扫描）恢复：

1. 启动扫描标记 ``run/interrupted`` + reconcile 需人工（快照端点可见）；
2. 未带 decisions 的 ``POST /recover`` → 409，``pending_decisions`` 携带 W-13
   展示字段（default_action/risk_level/probe），且**零准入**：事件流零新增、
   无 tool/result、无工具副作用（issue AC：用户裁决前 model/tool calls = 0）；
3. 带 ``decisions=[{tool_call_id, CONFIRM_SUCCESS, source}]`` 重发 → 200；
   DB 行 SUCCEEDED + ``reconcile_meta``（verdict + source）。

继承 tests/integration/test_kill_resume.py 的真实子进程探针形态。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from fastapi.testclient import TestClient

from agent_harness.config import Settings
from agent_harness.session import TOOL_RESULT, JsonlSessionStore
from agent_harness.storage import OperationState, SqliteOperationLedger
from agent_harness.tools.write import WriteTool
from agent_harness.web.app import create_app
from tests.integration.test_kill_resume import _discover_session_id, _run_child


def test_kill_full_loop_409_with_display_fields_then_decisions_200(tmp_path: Path):
    root = tmp_path / "root"
    returncode = _run_child(
        root,
        {
            "root": str(root),
            # web 组合根的账本固定为 <workspace_dir>/harness.db：子进程写同一份，
            # 启动扫描才能对同一份 Ledger 事实做 reconcile（needs_manual_reconcile）。
            "db": "harness.db",
            "calls": [
                {
                    "id": "call-1",
                    "name": "write",
                    "args": {"path": "hello.txt", "content": "written-by-child"},
                }
            ],
            "kill_stage": "running",
            "kill_call_id": "call-1",
        },
    )
    assert returncode == 137
    session_id = _discover_session_id(root)

    settings = Settings(
        _env_file=None, workspace_dir=str(root), model_api_key="sk-test"
    )
    app = create_app(settings, enable_cors=False)
    with TestClient(app) as client:  # lifespan：启动扫描恰一次
        # 启动扫描已把该会话标记为需人工 reconcile，快照端点可见。
        snapshot = client.get("/api/recovery/interrupted").json()
        row = next(
            i for i in snapshot["items"] if i["session_id"] == session_id
        )
        assert row["recovery"] == "needs_manual_reconcile"

        store = JsonlSessionStore(root / "sessions")
        events_before = store.read_events(session_id)

        # 未带 decisions → 409；pending_decisions 含 W-13 展示字段。
        r409 = client.post(f"/api/sessions/{session_id}/recover")
        assert r409.status_code == 409
        pending = r409.json()["detail"]["pending_decisions"]
        assert [p["tool_call_id"] for p in pending] == ["call-1"]
        assert pending[0]["state"] == "RUNNING"
        # write 未声明 replay_safe → 安全侧 DEFER/high（#14；Pi 双 safe 形状）。
        assert pending[0]["default_action"] == "DEFER"
        assert pending[0]["risk_level"] == "high"
        # probe 逐字取 WriteTool 既有 ReconcileHint（不伪造「已查到」结论）。
        hint = WriteTool(None).reconcile_hint
        assert pending[0]["probe"] == {
            "verifiable": hint.verifiable,
            "suggested_action": hint.suggested_action,
        }

        # 用户裁决前零准入：事件流零新增、无 tool/result、无工具副作用。
        events_after_409 = store.read_events(session_id)
        assert len(events_after_409) == len(events_before)
        assert not [e for e in events_after_409 if e.type == TOOL_RESULT]
        assert not (root / "ws" / "workspaces" / session_id / "hello.txt").exists()

        # 带 decisions 重发 → 200（同一端点、同一 DecisionsReconcileCallback）。
        r200 = client.post(
            f"/api/sessions/{session_id}/recover",
            json={"decisions": [
                {
                    "tool_call_id": "call-1",
                    "verdict": "CONFIRM_SUCCESS",
                    "source": "我查了文件/命令输出",
                }
            ]},
        )
        assert r200.status_code == 200, r200.json()
        events = r200.json()
        assert any(e["type"] == "operation/reconciled" for e in events)
        assert any(
            e["type"] == "tool/result" and e["data"].get("tool_call_id") == "call-1"
            for e in events
        )

        # DB 证据：operations 行 state + reconcile_meta（verdict + source）。
        async def _row():
            ledger = SqliteOperationLedger(root / "harness.db")
            await ledger.initialize()
            operation = await ledger.get(session_id, "call-1")
            return operation.state, json.loads(operation.reconcile_meta)

        state, meta = asyncio.run(_row())
        assert state is OperationState.SUCCEEDED
        assert meta["verdict"] == "CONFIRM_SUCCESS"
        assert meta["source"] == "我查了文件/命令输出"
