"""R20 全环（#357 W-13 / `#1327`）：真实 kill → 启动扫描自动裁决 → 幂等 200。

真实 Python 子进程在 ``running`` 注入点 os._exit（Ledger 行 RUNNING、tool/call
悬空、副作用未发生），父进程以真实 Web 组合根（``create_app`` + lifespan 启动
扫描）恢复：

1. 启动扫描标记 ``run/interrupted``，并以 ``AutoReconcileCallback`` 结清账本——
   write 未声明 ``replay_safe`` ⇒ ABANDON → CANCELLED（快照端点显示 recovered）；
2. 合成的 tool/result 是一张可忽略通知（"已按规则自动裁决"，不冒充用户裁决），
   且没有工具副作用；
3. 扫描后无待裁决：``POST /recover``（无 decisions）幂等 200。

W-13 展示字段（default_action / risk_level / probe）在混合形态 409 下由
``tests/test_web_api.py::test_recover_409_pending_decisions_carry_recovery_display_fields``
覆盖；显式用户裁决路径（decisions → 200）由同文件 decision matrix 覆盖（同一
``DecisionsReconcileCallback``），本文件不重复。

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
from agent_harness.web.app import create_app
from tests.integration.test_kill_resume import _discover_session_id, _run_child


def test_kill_scan_auto_adjudicates_write_call(tmp_path: Path):
    root = tmp_path / "root"
    returncode = _run_child(
        root,
        {
            "root": str(root),
            # web 组合根的账本固定为 <workspace_dir>/harness.db：子进程写同一份，
            # 启动扫描才能对同一份 Ledger 事实做 reconcile。
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
    app = create_app(settings, enable_cors=False,
    allow_testserver=True,
    )
    with TestClient(app) as client:  # lifespan：启动扫描恰一次
        # 启动扫描已用自动裁决结清 write（MUTATING + 未声明 replay_safe），
        # 快照端点不再显示 needs_manual_reconcile。
        snapshot = client.get("/api/recovery/interrupted").json()
        row = next(
            i for i in snapshot["items"] if i["session_id"] == session_id
        )
        assert row["recovery"] == "recovered"

        store = JsonlSessionStore(root / "sessions")
        events = store.read_events(session_id)

        # 自动裁决证据：operation/reconciled 落 ABANDON；合成的 tool/result
        # 自陈是规则裁决，不冒充用户裁决。
        reconciled = [e for e in events if e.type == "operation/reconciled"]
        assert reconciled and reconciled[0].data["verdict"] == "ABANDON"
        results = [
            e for e in events
            if e.type == TOOL_RESULT and e.data.get("tool_call_id") == "call-1"
        ]
        assert results
        content = results[0].data["content"]
        assert "已按规则自动裁决" in content
        assert "用户已裁决" not in content

        # DB 证据：operations 行终态 CANCELLED + reconcile_meta verdict。
        async def _row():
            ledger = SqliteOperationLedger(root / "harness.db")
            await ledger.initialize()
            operation = await ledger.get(session_id, "call-1")
            return operation.state, json.loads(operation.reconcile_meta)

        state, meta = asyncio.run(_row())
        assert state is OperationState.CANCELLED
        assert meta["verdict"] == "ABANDON"

        # 无工具副作用：write 在 sandbox 里从未落盘。
        assert not (root / "ws" / "workspaces" / session_id / "hello.txt").exists()

        # 扫描后无待裁决：无 decisions 的 recover 幂等 200。
        # W-13 展示字段（default_action/risk_level/probe）在混合形态 409 下由
        # tests/test_web_api.py::test_recover_409_pending_decisions_carry_recovery_display_fields
        # 覆盖；显式 decisions → 200 由同文件 decision matrix 覆盖（同一
        # DecisionsReconcileCallback），本文件不再重复。
        assert client.post(f"/api/sessions/{session_id}/recover").status_code == 200
