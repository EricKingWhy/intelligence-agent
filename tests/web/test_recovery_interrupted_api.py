"""GET /api/recovery/interrupted 只读快照端点（#357 W-13 R15-R16）。

端点返回 lifespan 启动扫描（``scan_interrupted``）的**快照**——启动时已把
扫描结果存入 ``app.state``；端点只读快照并做只读富化（Task / 无终态 run /
工作目录锚 / 进度文件版本），**绝不重跑** ``scan_interrupted()``：后者会写
``run/interrupted`` 并跑 reconcile，有写副作用（R15 反证基础）。

fail-safe（R16，抄 Cline 默认亮 Resume）：进度文件缺失/不可读/schema 不匹配
的行 → ``resume_available=true``（状态不可读时给安全侧 affordance）；
版本字段如实标 ``missing/unreadable/invalid_schema``，不伪造「最新」。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agent_harness.config import Settings
from agent_harness.session import (
    TOOL_CALL,
    USER_MESSAGE,
    Session,
)
from agent_harness.session.event import RUN_INTERRUPTED
from agent_harness.session.store import JsonlSessionStore
from agent_harness.storage import Operation, OperationState, SqliteOperationLedger
from agent_harness.web.app import create_app


@pytest.fixture
def app(tmp_path):
    settings = Settings(
        _env_file=None, workspace_dir=str(tmp_path), model_api_key="sk-test"
    )
    return create_app(settings, enable_cors=False)


def _seed_interrupted_session(
    root: Path,
    *,
    cwd: str | None = None,
    user_message: str | None = None,
    ledger_unknown: bool = False,
) -> str:
    """直接在磁盘上制造「执行中崩溃」现场（app 创建之前落盘）。"""
    store = JsonlSessionStore(root / "sessions")
    session = Session.start(store, cwd=cwd)
    if user_message:
        session.append(USER_MESSAGE, {"content": user_message})
    run_id, _ = session.begin_run()
    session.append(
        TOOL_CALL,
        {"tool_call_id": "call-x", "tool_name": "bash", "args": {}},
        run_id=run_id,
    )
    if ledger_unknown:
        ledger = SqliteOperationLedger(root / "harness.db")
        asyncio.run(ledger.initialize())
        asyncio.run(ledger.create(
            Operation(
                tool_call_id="call-x",
                session_id=session.session_id,
                run_id=run_id,
                agent_id=None,
                tool_name="bash",
                args_identity="{}",
                state=OperationState.PENDING,
                started_at="2026-10-06T00:00:00Z",
            )
        ))
        asyncio.run(ledger.update_state(
            session.session_id, "call-x", OperationState.RUNNING))
        asyncio.run(ledger.update_state(
            session.session_id, "call-x", OperationState.UNKNOWN))
    return session.session_id


def _interrupted_count(events: list) -> int:
    return len([e for e in events if e.type == RUN_INTERRUPTED])


def test_endpoint_returns_lifespan_snapshot_without_write_side_effects(
    app, tmp_path
):
    """R15：快照端点零写副作用——调用前后事件数不变、无新增 run/interrupted。"""
    root = Path(app.state.agent.settings.workspace_dir)
    session_id = _seed_interrupted_session(
        root, user_message="修复登录页崩溃", cwd=str(tmp_path / "project")
    )

    with TestClient(app) as client:  # lifespan：启动扫描恰一次（允许写）
        store = JsonlSessionStore(root / "sessions")
        events_after_scan = store.read_events(session_id)
        assert _interrupted_count(events_after_scan) == 1, (
            "前置：lifespan 扫描应已补记 run/interrupted"
        )

        resp = client.get("/api/recovery/interrupted")

    assert resp.status_code == 200
    body = resp.json()
    assert body["snapshot_available"] is True
    row = next(i for i in body["items"] if i["session_id"] == session_id)

    # 四要素：上次 Task（first_user_message 口径）、无终态 run、工作目录锚。
    assert row["task"] == "修复登录页崩溃"
    assert row["interrupted_runs"], "无终态 run 必须出现在行里"
    assert row["interrupted_runs"][0]["run_id"] is not None
    assert row["workspace_root"] == str(tmp_path / "project")
    assert row["recovery"] in {"recovered", "needs_manual_reconcile", "failed"}

    # R15 反证：GET 前后事件流逐条一致（端点没有重跑扫描 / 没有 reconcile 写入）。
    events_after_get = store.read_events(session_id)
    assert len(events_after_get) == len(events_after_scan)
    assert _interrupted_count(events_after_get) == _interrupted_count(
        events_after_scan
    )


def test_endpoint_rows_carry_honest_progress_version_and_resume_failsafe(
    app, tmp_path
):
    """R16：进度文件缺失/schema 不匹配 → 如实标注 + resume_available=true。"""
    root = Path(app.state.agent.settings.workspace_dir)
    # 会话 A：cwd 有锚但从未写过进度文件 → missing。
    sid_missing = _seed_interrupted_session(
        root, user_message="missing 进度", cwd=str(tmp_path / "proj-a")
    )
    # 会话 B：进度文件存在但 schema_version 不是当前版本 → invalid_schema。
    proj_b = tmp_path / "proj-b"
    proj_b.mkdir(parents=True)
    sid_invalid = _seed_interrupted_session(
        root, user_message="invalid 进度", cwd=str(proj_b), ledger_unknown=True,
    )
    progress_md = (
        proj_b / "agent-progress" / sid_invalid / "progress.md"
    )
    progress_md.parent.mkdir(parents=True, exist_ok=True)
    progress_md.write_text(
        "# 会话进度（progress.md）\n\n"
        "- schema_version: 999\n- session_id: " + sid_invalid + "\n\n"
        "## 原目标\n- （无）\n",
        encoding="utf-8",
    )

    with TestClient(app) as client:
        resp = client.get("/api/recovery/interrupted")

    assert resp.status_code == 200
    rows = {i["session_id"]: i for i in resp.json()["items"]}

    missing_row = rows[sid_missing]
    assert missing_row["progress"]["status"] == "missing"
    # fail-safe（抄 Cline）：状态不可读仍默认亮 Resume，安全侧 affordance。
    assert missing_row["resume_available"] is True

    invalid_row = rows[sid_invalid]
    assert invalid_row["progress"]["status"] == "invalid_schema"
    assert invalid_row["resume_available"] is True
    # 账行 UNKNOWN 的会话必须标 needs_manual_reconcile（不伪造已恢复）。
    assert invalid_row["recovery"] == "needs_manual_reconcile"


def test_valid_progress_version_is_read_from_file_header(app, tmp_path):
    """进度可读的行：版本逐字取文件头 schema_version + source_event_seq。"""
    root = Path(app.state.agent.settings.workspace_dir)
    proj = tmp_path / "proj-ok"
    proj.mkdir(parents=True)
    sid = _seed_interrupted_session(
        root, user_message="ok 进度", cwd=str(proj)
    )
    progress_md = proj / "agent-progress" / sid / "progress.md"
    progress_md.parent.mkdir(parents=True, exist_ok=True)
    progress_md.write_text(
        "# 会话进度（progress.md）\n\n"
        "- schema_version: 1\n- session_id: " + sid + "\n"
        "- source_event_seq: 42\n\n## 原目标\n- （无）\n",
        encoding="utf-8",
    )

    with TestClient(app) as client:
        resp = client.get("/api/recovery/interrupted")

    row = next(
        i for i in resp.json()["items"] if i["session_id"] == sid
    )
    # 版本从文件头读（此处手写头即手写值）；不因为投影不同而谎报。
    assert row["progress"]["schema_version"] == "1"
    assert row["progress"]["source_event_seq"] == 42


def test_endpoint_without_lifespan_reports_snapshot_unavailable(app):
    """lifespan 未跑（无快照）→ snapshot_available=false + 空列表：不伪造扫描。"""
    client = TestClient(app)  # 不进 lifespan：快照缺失
    resp = client.get("/api/recovery/interrupted")
    assert resp.status_code == 200
    body = resp.json()
    assert body["snapshot_available"] is False
    assert body["items"] == []
