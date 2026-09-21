"""#204：`create_and_launch(launch=False)` 只建会话不启动 run（TDD 红绿）。

契约（#204 实现裁定 §2 / §3）：
- `launch=False`：只写 `session/started` + 会话元数据，**不启动 run**，
  RunManager.launch 不被调用（无 subscriber、无在途 run）；
- 会话元数据（workspace/cwd/权限/amend/model）与 `launch=True` 同一套路径；
- `launch=False` 返回束的 `run`/`subscriber` 为 None——调用方据此不组 SSE；
- `POST /api/sessions` 同时给 `task` 与 `launch=False` ⇒ 422（"给了任务却
  静默不执行"的矛盾组合必须显式拒绝）；
- 创建响应回传会话级 `permission_mode`——前端用它初始化 composer 权限 pill
  （#204 裁定 §3：不要各自取默认值，那正是不一致的来源）。
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage

from agent_harness.config import Settings
from agent_harness.session import SESSION_STARTED, Session
from agent_harness.tooling.contract import PermissionPolicy
from agent_harness.web.app import create_app, session_service
from tests.scripted_model import ScriptedModel


def _state(tmp_path) -> MagicMock:
    """与 test_model_change.py::_state 同构的隔离 AppState 替身。"""
    settings = Settings(
        _env_file=None,
        workspace_dir=str(tmp_path),
        model_api_key="sk-test",
        model_provider="deepseek",
        model_name="deepseek-chat",
    )
    state = MagicMock()
    state.settings = settings
    state.store = Session.__dict__  # placeholder, replaced below
    from agent_harness.session.store import JsonlSessionStore

    state.store = JsonlSessionStore(root=tmp_path / "sessions")
    state.workspaces_root = tmp_path
    state.workspace_registry = None
    state.workspace_index = None
    state.run_manager = MagicMock()
    state.run_manager.get_active = MagicMock(return_value=None)
    state.run_manager.launch = MagicMock(return_value=(MagicMock(), MagicMock()))
    state.get_wiring = AsyncMock(return_value=(MagicMock(), MagicMock()))
    state.ensure_stores = AsyncMock()
    state.stores = MagicMock()
    return state


# ── 领域层：launch=False ─────────────────────────────────────────────


def test_launch_false_does_not_start_run(tmp_path):
    """launch=False：session/started 落盘、RunManager.launch 不被调、
    返回束 run/subscriber 为 None。"""
    state = _state(tmp_path)
    with patch("agent_harness.session.service.build_runtime", new_callable=AsyncMock):
        service = session_service(state)
        result = asyncio.run(
            service.create_and_launch(task="hello", launch=False)
        )

    state.run_manager.launch.assert_not_called()
    assert result.run is None
    assert result.subscriber is None

    events = state.store.read_events(result.session.session_id)
    assert events[0].type == SESSION_STARTED
    # 只有 session/started：没有 user/message、没有 run 事件。
    assert len(events) == 1


def test_launch_false_with_explicit_workspace(tmp_path):
    """launch=False 与 launch=True 走同一套 workspace/元数据路径：
    显式 workspace 名仍建目录、仍归组（本测试无索引 → 不归组，但目录必须建）。"""
    state = _state(tmp_path)
    with patch("agent_harness.session.service.build_runtime", new_callable=AsyncMock):
        service = session_service(state)
        result = asyncio.run(
            service.create_and_launch(
                task="hello", workspace_name="proj-a", launch=False
            )
        )

    assert (tmp_path / "proj-a").is_dir()
    events = state.store.read_events(result.session.session_id)
    assert len(events) == 1
    assert events[0].type == SESSION_STARTED


def test_launch_false_session_metadata_matches_launch_true(tmp_path):
    """两种意图共用同一路径：session/started 的元数据（cwd 锚）逐字段一致。"""
    state = _state(tmp_path)
    with patch("agent_harness.session.service.build_runtime", new_callable=AsyncMock):
        service = session_service(state)
        launched = asyncio.run(
            service.create_and_launch(task="hi", workspace_name="w1")
        )
        created = asyncio.run(
            service.create_and_launch(task="hi", workspace_name="w2", launch=False)
        )

    launched_events = state.store.read_events(launched.session.session_id)
    created_events = state.store.read_events(created.session.session_id)
    launched_data = launched_events[0].data
    created_data = created_events[0].data
    # 元数据形状一致：cwd 都写进 session/started（键集合相同）。
    assert set(launched_data) == set(created_data)
    assert launched_data["cwd"] and created_data["cwd"]


# ── Web 层：launch=false 参数 + task/launch 互斥 + permission_mode 回传 ──


def _web_client(tmp_path):
    settings = Settings(workspace_dir=str(tmp_path), model_api_key="sk-test")
    app = create_app(settings, enable_cors=False)
    return app, TestClient(app)


def test_web_create_launch_false_builds_session_without_run(tmp_path):
    """POST /api/sessions?launch=false：返回会话 JSON（非 SSE），只有
    session/started，无在途 run。"""
    app, client = _web_client(tmp_path)
    with patch("agent_harness.assembly.create_chat_model",
               return_value=ScriptedModel(responses=[AIMessage(content="ok")])):
        resp = client.post("/api/sessions", json={"workspace": "proj-b"},
                           params={"launch": "false"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["session_id"]
    assert body["permission_mode"] == "workspace-write"

    store = app.state.agent.store
    events = store.read_events(body["session_id"])
    assert [e.type for e in events] == [SESSION_STARTED]
    assert app.state.agent.run_manager.get_active(body["session_id"]) is None


def test_web_create_launch_false_with_task_is_422(tmp_path):
    """给了任务却 launch=false ⇒ 422（矛盾组合显式拒绝，且不留落盘痕迹）。"""
    app, client = _web_client(tmp_path)
    resp = client.post("/api/sessions", json={"task": "hi"},
                       params={"launch": "false"})
    assert resp.status_code == 422
    assert list(app.state.agent.workspaces_root.iterdir()) == []
    assert list(app.state.agent.sessions_root.iterdir()) == []


def test_web_create_launch_default_still_sse(tmp_path):
    """launch 缺省（默认 true）：既有 SSE 契约逐字不变。"""
    _, client = _web_client(tmp_path)
    with patch("agent_harness.assembly.create_chat_model",
               return_value=ScriptedModel(responses=[AIMessage(content="ok")])):
        resp = client.post("/api/sessions", json={"task": "hi"})
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")


def test_web_create_response_carries_permission_mode(tmp_path):
    """创建响应回传会话级 permission_mode——显式选定的档位原样返回，
    前端用它初始化 composer 权限 pill（#204 裁定 §3）。"""
    _, client = _web_client(tmp_path)
    with patch("agent_harness.assembly.create_chat_model",
               return_value=ScriptedModel(responses=[AIMessage(content="ok")])):
        resp = client.post("/api/sessions",
                           json={"task": "hi",
                                 "permission_mode": "danger-full-access"})
    assert resp.status_code == 200
    # SSE 首帧之外的元数据通道：permission_mode 在响应头回传。
    assert resp.headers["x-permission-mode"] == "danger-full-access"


def test_web_create_launch_false_carries_explicit_permission_mode(tmp_path):
    """launch=false + 显式 permission_mode：JSON 体回传同一档位，且
    X-Permission-Mode 头**恒在**（裁定 §3：创建响应必须回传 permission_mode，
    不分路径）。"""
    _, client = _web_client(tmp_path)
    with patch("agent_harness.assembly.create_chat_model",
               return_value=ScriptedModel(responses=[AIMessage(content="ok")])):
        resp = client.post("/api/sessions", json={"permission_mode": "read-only"},
                           params={"launch": "false"})
    assert resp.status_code == 200
    assert resp.json()["permission_mode"] == "read-only"
    assert resp.headers["x-permission-mode"] == "read-only"


def test_launch_false_interactive_permission_does_not_leak_approval_queue(tmp_path):
    """review 修复：interactive 权限（显式非完全访问档）+ launch=False 时，
    _build_approval_callback 登记进 approval_queues 的队列必须当场撤掉——
    没有 run 就没有终结回调来 GC 它，留着就是到进程重启才清的泄漏。"""
    state = _state(tmp_path)
    state.approval_queues = {}
    from unittest.mock import patch as _patch

    with _patch("agent_harness.session.service.build_runtime", new_callable=AsyncMock):
        service = session_service(state)
        # 对照组：launch=True 时 interactive 分支真实登记队列（证明确实会登记）。
        launched = asyncio.run(
            service.create_and_launch(
                task="hi", permission_mode=PermissionPolicy.READ_ONLY,
                permission_mode_explicit=True,
            )
        )
        assert state.approval_queues.get(launched.session.session_id) is not None
        state.approval_queues.pop(launched.session.session_id)

        # 主张：launch=False 时队列当场撤掉，approval_queues 不留条目。
        result = asyncio.run(
            service.create_and_launch(
                task="hi", permission_mode=PermissionPolicy.READ_ONLY,
                permission_mode_explicit=True, launch=False,
            )
        )
    assert state.approval_queues.get(result.session.session_id) is None


def test_permission_policy_values_are_wire_format():
    """权限档位的 wire 值集合不变（三档）——前端 pill 与弹窗共用同一词汇。"""
    assert {p.value for p in PermissionPolicy} == {
        "read-only", "workspace-write", "danger-full-access",
    }
