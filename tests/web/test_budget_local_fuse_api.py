"""T3（#308）：`budget.local.max_agent_turns` 的 HTTP 语义 + "被拒请求零副作用"。

车票 AC 里属于 HTTP 边界的那几条（`#308` Acceptance Criteria）：

- legacy-only / 新字段-only / 相等双字段 / 冲突双字段 / 越权配置**都有测试**；
- 冲突与越权必须在 model / tool / session run **启动前**失败（`R4`：拒绝，不静默截断）；
- legacy-only 要有 deprecation signal（`R5`）；
- effective local fuse 要有**只读投影**（Must Do：供客户端显示，`11 §6.1`）。

判定规则本身住在 `agent_harness.agent.budget.resolve_local_fuse`（单一规则来源），
其穷举单测在 `tests/agent/test_local_budget_resolution.py`。本文件**不重测规则**，
只锁 HTTP 层三件事：

1. 422 的映射（`web/domain_errors.py` 里 `BudgetRejection` 家族 → 422）；
2. 投影通道（响应头 `X-Local-Max-Agent-Turns` / `X-Local-Fuse-Source` +
   `Deprecation` / `Warning: 299`）；
3. **零副作用**：被拒请求不建 workspace、不落 session JSONL、不构造模型
   （"启动 run 前失败"的可观测定义——三样都动不了，run 自然没启动）。

WS 帧（第四个续聊入口）单独覆盖：形状错误与语义拒绝都必须回错误帧，
不能静默丢弃（`web/websocket.py::ws_budget_claims`）。
"""

from __future__ import annotations

from typing import Any, Self
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage

from agent_harness.config import Settings
from agent_harness.web.app import create_app
from tests.scripted_model import ScriptedModel

_DEFAULT_CEILING = 500


def _web(tmp_path, *, ceiling: int | None = None) -> tuple[Any, TestClient]:
    """隔离 app + client；`ceiling` 显式模拟运维压低 Deployment 上限。"""
    overrides: dict[str, Any] = {
        "_env_file": None,  # 不吃仓库根 .env（测试必须自足）
        "workspace_dir": str(tmp_path),
        "model_api_key": "sk-test",
        "enable_cors": False,
    }
    if ceiling is not None:
        overrides["local_max_agent_turns"] = ceiling
    app = create_app(Settings(**overrides), enable_cors=False)
    return app, TestClient(app)


class _ModelProbe:
    """模型工厂探针：既提供确定性剧本，又记录"模型真的被构造过"。

    "启动 run 前失败"不能只靠状态码证明——被拒请求若已经构造了模型，说明
    校验点在副作用之后。`calls` 为空的断言就是那条边界的可观测证据。
    """

    def __init__(self) -> None:
        self.calls: list[Any] = []
        self._patcher: Any = None

    def __enter__(self) -> Self:
        def _factory(config: Any, **kwargs: Any) -> ScriptedModel:
            self.calls.append(config)
            return ScriptedModel(responses=[AIMessage(content="ok")])

        self._patcher = patch(
            "agent_harness.assembly.create_chat_model", side_effect=_factory,
        )
        self._patcher.start()
        return self

    def __exit__(self, *exc: object) -> bool:
        self._patcher.stop()
        return False


def _assert_rejected_without_side_effects(app: Any, probe: _ModelProbe) -> None:
    """被拒请求：不建 workspace、不落 session JSONL、不构造模型。"""
    assert probe.calls == [], "被拒请求不得构造模型（校验必须早于任何工作）"
    assert list(app.state.agent.workspaces_root.iterdir()) == [], "不得创建 workspace"
    assert list(app.state.agent.sessions_root.iterdir()) == [], "不得落盘 session"


# ── 接受路径：投影 + deprecation signal ──


def test_default_request_resolves_to_deployment_ceiling(tmp_path):
    """缺省请求（不带任何 fuse 字段）→ 生效 500、来源 deployment。"""
    _, client = _web(tmp_path)
    probe = _ModelProbe()
    with probe:
        resp = client.post("/api/sessions", json={"task": "hi"})
    assert resp.status_code == 200, resp.text
    assert resp.headers["x-local-max-agent-turns"] == str(_DEFAULT_CEILING)
    assert resp.headers["x-local-fuse-source"] == "deployment"
    assert "deprecation" not in resp.headers, "没用旧字段就不该报废弃"
    assert probe.calls, "合法请求必须真的构造模型（否则上面的头只是装饰）"


def test_new_field_only_accepted_without_deprecation_signal(tmp_path):
    """新字段-only → 接受，来源是请求字段本身，无 deprecation。"""
    _, client = _web(tmp_path)
    probe = _ModelProbe()
    with probe:
        resp = client.post(
            "/api/sessions",
            json={"task": "hi", "budget": {"local": {"max_agent_turns": 6}}},
        )
    assert resp.status_code == 200, resp.text
    assert resp.headers["x-local-max-agent-turns"] == "6"
    assert resp.headers["x-local-fuse-source"] == "budget.local.max_agent_turns"
    assert "deprecation" not in resp.headers
    assert "warning" not in resp.headers


def test_legacy_alias_only_accepted_with_deprecation_signal(tmp_path):
    """legacy-only（R5）：行为可预测（同一 effective 值）+ 废弃信号可见。"""
    _, client = _web(tmp_path)
    probe = _ModelProbe()
    with probe:
        resp = client.post("/api/sessions", json={"task": "hi", "max_steps": 7})
    assert resp.status_code == 200, resp.text
    assert resp.headers["x-local-max-agent-turns"] == "7"
    assert resp.headers["x-local-fuse-source"] == "max_steps_alias"
    # R5 的 deprecation signal：老客户端不改代码也能看到自己踩了废弃字段。
    assert resp.headers["deprecation"] == "true"
    assert resp.headers["warning"].startswith("299")
    assert "budget.local.max_agent_turns" in resp.headers["warning"]


def test_equal_double_fields_accepted_and_deprecation_still_visible(tmp_path):
    """相等双字段：接受（不因"两个都传"就拒绝），且废弃信号仍在。"""
    _, client = _web(tmp_path)
    probe = _ModelProbe()
    with probe:
        resp = client.post(
            "/api/sessions",
            json={
                "task": "hi",
                "max_steps": 8,
                "budget": {"local": {"max_agent_turns": 8}},
            },
        )
    assert resp.status_code == 200, resp.text
    assert resp.headers["x-local-max-agent-turns"] == "8"
    assert resp.headers["x-local-fuse-source"] == "max_steps_alias"
    assert resp.headers["deprecation"] == "true"


def test_request_equal_to_deployment_ceiling_accepted(tmp_path):
    """边界：恰好等于 ceiling 是收紧到顶，不是越权（无 off-by-one）。"""
    _, client = _web(tmp_path, ceiling=5)
    probe = _ModelProbe()
    with probe:
        resp = client.post(
            "/api/sessions",
            json={"task": "hi", "budget": {"local": {"max_agent_turns": 5}}},
        )
    assert resp.status_code == 200, resp.text
    assert resp.headers["x-local-max-agent-turns"] == "5"


# ── 拒绝路径：422 + 零副作用 ──


def test_conflicting_double_fields_rejected_before_any_work(tmp_path):
    """冲突双字段 → 422，且发生在任何工作开始之前（R4 / AC）。"""
    app, client = _web(tmp_path)
    probe = _ModelProbe()
    with probe:
        resp = client.post(
            "/api/sessions",
            json={
                "task": "hi",
                "max_steps": 8,
                "budget": {"local": {"max_agent_turns": 9}},
            },
        )
    assert resp.status_code == 422, resp.text
    detail = resp.json()["detail"]
    assert "8" in detail and "9" in detail, f"错误文案要指出冲突的两个值：{detail}"
    _assert_rejected_without_side_effects(app, probe)


def test_request_over_deployment_ceiling_rejected_before_any_work(tmp_path):
    """越权（request > Deployment ceiling）→ 422，且不启动任何工作。"""
    app, client = _web(tmp_path, ceiling=5)
    probe = _ModelProbe()
    with probe:
        resp = client.post(
            "/api/sessions",
            json={"task": "hi", "budget": {"local": {"max_agent_turns": 6}}},
        )
    assert resp.status_code == 422, resp.text
    assert "5" in resp.json()["detail"], "错误文案要给出生效 ceiling"
    _assert_rejected_without_side_effects(app, probe)


def test_alias_over_deployment_ceiling_rejected_before_any_work(tmp_path):
    """越权走旧字段同样拒绝：alias 是同一语义，不是绕过 ceiling 的后门。"""
    app, client = _web(tmp_path, ceiling=5)
    probe = _ModelProbe()
    with probe:
        resp = client.post("/api/sessions", json={"task": "hi", "max_steps": 6})
    assert resp.status_code == 422, resp.text
    _assert_rejected_without_side_effects(app, probe)


def test_request_over_profile_ceiling_rejected_before_any_work(tmp_path, monkeypatch):
    """PRD 决策 3 / R3：请求不得越过 **AgentProfile 政策**——生效上层 = min(各层)。

    内置三档位都声明 `None`（出厂不写死数字），所以这里把一个档位的声明值调到 40：缺省
    Deployment 500 仍在上，**档位**才是生效上层，请求 300 必须 422（而不是静默取 40）。
    请求 30 是合法的收窄 ⇒ 200 且投影回 30。
    """
    from dataclasses import replace

    from agent_harness.agent.profiles import BUILTIN_PROFILES

    monkeypatch.setitem(
        BUILTIN_PROFILES, "coding",
        replace(BUILTIN_PROFILES["coding"], max_agent_turns=40),
    )
    app, client = _web(tmp_path)
    probe = _ModelProbe()
    with probe:
        rejected = client.post(
            "/api/sessions",
            json={
                "task": "hi",
                "agent_profile": "coding",
                "budget": {"local": {"max_agent_turns": 300}},
            },
        )
    assert rejected.status_code == 422, rejected.text
    assert "40" in rejected.json()["detail"], "错误文案要给出生效 ceiling（档位声明）"
    _assert_rejected_without_side_effects(app, probe)

    with probe:
        accepted = client.post(
            "/api/sessions",
            json={
                "task": "hi",
                "agent_profile": "coding",
                "budget": {"local": {"max_agent_turns": 30}},
            },
        )
    assert accepted.status_code == 200, accepted.text
    assert accepted.headers["x-local-max-agent-turns"] == "30"


def test_steer_judges_the_budget_body_before_the_target_check(tmp_path):
    """非启动分支（steer）也要判请求体，且**判定先于**"有没有在途 run"这条分支。

    空转会话上 `mode=steer` 本来是 409（`SteerTargetNotFound`）；请求体自身矛盾时
    必须是 **422**。若判定被放到分支之后，这里读到的就是 409——同一份 body 的语义
    取决于运行态，客户端与复核者都无从预期。

    "在途 run 的 queued / steer" 那条分支需要把 run 真钉在在途（本文件的
    `TestClient` 会等 SSE 流跑完，拿不到那个窗口），证据在会话层：
    `tests/session/test_multiturn_delivery.py::test_in_flight_input_still_judges_the_budget_body`。
    """
    from agent_harness.session.event import MESSAGE_QUEUED, STEER_REQUESTED

    app, client = _web(tmp_path)
    session_id = _create_idle_session(client)
    before = [e.type for e in app.state.agent.store.read_events(session_id)]

    conflict = client.post(
        f"/api/sessions/{session_id}/messages",
        json={
            "content": "you are confused",
            "mode": "steer",
            "max_steps": 8,
            "budget": {"local": {"max_agent_turns": 9}},
        },
    )
    assert conflict.status_code == 422, conflict.text
    assert "8" in conflict.json()["detail"] and "9" in conflict.json()["detail"]

    over_ceiling = client.post(
        f"/api/sessions/{session_id}/messages",
        json={
            "content": "you are confused",
            "mode": "steer",
            "budget": {"local": {"max_agent_turns": 10_000}},
        },
    )
    assert over_ceiling.status_code == 422, over_ceiling.text

    # 被拒请求零副作用（会话已存在，所以这里看事件流而不是 workspace）：事件流**逐条**
    # 不变——只断言"某两个类型不在列表里"会放过 queue/cancelled、message/superseded
    # 这类被拒请求本不该写下的记录。
    after = [e.type for e in app.state.agent.store.read_events(session_id)]
    assert after == before, f"{before} → {after}"
    assert MESSAGE_QUEUED not in after and STEER_REQUESTED not in after

    # 对照组：合法 body 在同一个空转会话上仍是 409——判定没把目标检查吞掉，
    # 也没把"没在途 run"这件事实改写成别的状态码。
    legal = client.post(
        f"/api/sessions/{session_id}/messages",
        json={"content": "you are confused", "mode": "steer", "max_steps": 9},
    )
    assert legal.status_code == 409, legal.text


@pytest.mark.parametrize("turns", [0, -1])
def test_non_positive_turns_rejected_before_any_work(tmp_path, turns):
    """非正数 → 422（形状闸门在 pydantic，领域层还有一层 `_positive`）。"""
    app, client = _web(tmp_path)
    probe = _ModelProbe()
    with probe:
        resp = client.post(
            "/api/sessions",
            json={"task": "hi", "budget": {"local": {"max_agent_turns": turns}}},
        )
    assert resp.status_code == 422, resp.text
    _assert_rejected_without_side_effects(app, probe)


def test_unknown_key_in_budget_rejected_not_silently_ignored(tmp_path):
    """budget 里的未知键 → 422。

    拼错字段名若被静默忽略，用户会以为自己设了预算——真正的上限仍是 500。
    这类"以为设了"比 422 危险得多，所以 `extra="forbid"`。
    """
    app, client = _web(tmp_path)
    probe = _ModelProbe()
    with probe:
        resp = client.post(
            "/api/sessions",
            json={
                "task": "hi",
                "budget": {"local": {"max_agent_turns": 5, "max_turn": 5}},
            },
        )
    assert resp.status_code == 422, resp.text
    _assert_rejected_without_side_effects(app, probe)


def test_run_and_session_budget_not_accepted_yet(tmp_path):
    """`budget.run` / `budget.session` 在 T4/T10 之前一律 422。

    "先看起来接受、其实不生效"是最坏的一种兼容：用户会以为预算在管。宁可
    显式拒绝（T4/T10 落地时再开这两个键）。
    """
    app, client = _web(tmp_path)
    probe = _ModelProbe()
    with probe:
        for payload in (
            {"budget": {"run": {"max_agent_turns_total": 5}}},
            {"budget": {"session": {"max_agent_turns_total": 5}}},
        ):
            resp = client.post("/api/sessions", json={"task": "hi", **payload})
            assert resp.status_code == 422, f"{payload} 应被拒：{resp.text}"
    _assert_rejected_without_side_effects(app, probe)


# ── resume / messages：同一条通道 ──


def _create_idle_session(client: TestClient) -> str:
    """只建会话不启动 run（launch=false）→ 得到可 resume 的空闲会话。"""
    resp = client.post("/api/sessions", json={}, params={"launch": "false"})
    assert resp.status_code == 200, resp.text
    # 只建会话**不投影** fuse：那个数字是请求级的、未被任何 run 消费，也不持久化
    # （后续 /messages 会按当时的 Deployment 重新解析）——回一个"会话级 ceiling"
    # 是假事实（不变量 #21 同族：缺失不能被顶替成看起来有值）。
    assert "x-local-max-agent-turns" not in resp.headers, resp.headers
    assert "x-local-fuse-source" not in resp.headers, resp.headers
    return resp.json()["session_id"]


def test_queue_flush_projects_the_same_fuse(tmp_path):
    """队列重投（flush）的 launched 响应与 `/messages` 同一投影（同一段组装）。

    起点是**空会话**（`launch=false`）+ 手工 append 一条 `message/queued`（模拟"重启后
    手工投递"）：先跑一个真 run 再 append 会与终态回调的自动接力竞争（既有用例
    `test_multiturn_queue_http.py` 已记录过这个形状）。
    """
    from agent_harness.session import Session
    from agent_harness.session.event import MESSAGE_QUEUED
    from agent_harness.session.store import JsonlSessionStore

    app, client = _web(tmp_path)
    session_id = _create_idle_session(client)
    Session.append_event(
        JsonlSessionStore(root=app.state.agent.sessions_root), session_id,
        MESSAGE_QUEUED, {"queue_id": "q-flush", "content": "重启前的消息"},
    )
    probe = _ModelProbe()
    with probe:
        resp = client.post(f"/api/sessions/{session_id}/queue/flush")
    assert resp.status_code == 200, resp.text
    assert probe.calls, "flush 必须真的拉起 run（否则响应头只是装饰）"
    assert resp.headers["x-local-max-agent-turns"] == str(_DEFAULT_CEILING)
    assert resp.headers["x-local-fuse-source"] == "deployment"


def test_resume_honors_budget_and_projects_fuse(tmp_path):
    """显式恢复也接受 budget（`11 §6.1`），投影与创建路径同形。"""
    _, client = _web(tmp_path)
    session_id = _create_idle_session(client)
    probe = _ModelProbe()
    with probe:
        resp = client.post(
            f"/api/sessions/{session_id}/resume",
            json={"task": "继续", "budget": {"local": {"max_agent_turns": 9}}},
        )
    assert resp.status_code == 200, resp.text
    assert resp.headers["x-local-max-agent-turns"] == "9"
    assert resp.headers["x-local-fuse-source"] == "budget.local.max_agent_turns"


def test_resume_accepts_legacy_alias_with_deprecation_signal(tmp_path):
    """alias 规则是全局的（`02 §5.1` / D8）：恢复端点也认 `max_steps` 并给废弃信号。

    本端点此前没有该字段，但 alias 合并点若缺席，`{"max_steps": 8, "budget": {…9}}`
    会静默通过冲突校验——同一请求体在创建端点 422、在恢复端点静默生效。
    """
    _, client = _web(tmp_path)
    session_id = _create_idle_session(client)
    probe = _ModelProbe()
    with probe:
        resp = client.post(
            f"/api/sessions/{session_id}/resume", json={"task": "继续", "max_steps": 9},
        )
    assert resp.status_code == 200, resp.text
    assert resp.headers["x-local-max-agent-turns"] == "9"
    assert resp.headers["x-local-fuse-source"] == "max_steps_alias"
    assert resp.headers["deprecation"] == "true"


def test_rejected_resume_appends_nothing(tmp_path):
    """被拒的 resume 不得追加 `session/resumed`（拒绝早于 Session.resume）。"""
    _, client = _web(tmp_path)
    session_id = _create_idle_session(client)
    before = [e["type"] for e in client.get(f"/api/sessions/{session_id}/events").json()]

    probe = _ModelProbe()
    with probe:
        resp = client.post(
            f"/api/sessions/{session_id}/resume",
            json={
                "task": "继续",
                "max_steps": 8,
                "budget": {"local": {"max_agent_turns": 9}},
            },
        )
    assert resp.status_code == 422, resp.text
    after = [e["type"] for e in client.get(f"/api/sessions/{session_id}/events").json()]
    assert after == before, "被拒请求不得改动会话历史"
    assert "session/resumed" not in after
    assert probe.calls == []


def test_send_message_launched_projects_fuse(tmp_path):
    """续聊的 launched 分支与创建路径共用同一条投影通道。"""
    _, client = _web(tmp_path)
    probe = _ModelProbe()
    with probe:
        created = client.post("/api/sessions", json={"task": "首轮"})
        assert created.status_code == 200, created.text
        session_id = client.get("/api/sessions").json()[0]["session_id"]
        resp = client.post(
            f"/api/sessions/{session_id}/messages",
            json={"content": "继续", "budget": {"local": {"max_agent_turns": 12}}},
        )
    assert resp.status_code == 200, resp.text
    assert resp.headers["x-local-max-agent-turns"] == "12"
    assert resp.headers["x-local-fuse-source"] == "budget.local.max_agent_turns"


# ── WS 帧：第四个续聊入口 ──


def test_ws_budget_shape_error_returns_error_frame(tmp_path):
    """WS 帧里的形状错误当场回错误帧，连接保持（不静默丢弃、不静默死掉）。"""
    import json

    _, client = _web(tmp_path)
    session_id = _create_idle_session(client)
    with client.websocket_connect("/api/ws") as ws:
        ws.send_text(json.dumps({
            "type": "send_message",
            "session_id": session_id,
            "content": "继续",
            "budget": {"local": {"max_agent_turns": 5, "max_turn": 5}},
        }))
        payload = json.loads(ws.receive_text())
        assert payload["type"] == "error", payload
        ws.send_text(json.dumps({"type": "ping"}))
        assert json.loads(ws.receive_text())["type"] == "pong"


def test_ws_semantic_rejection_returns_error_frame(tmp_path):
    """WS 上的 alias 冲突同样有回声（第四个入口不能绕过 422 语义）。"""
    import json

    _, client = _web(tmp_path)
    session_id = _create_idle_session(client)
    before = [e["type"] for e in client.get(f"/api/sessions/{session_id}/events").json()]

    probe = _ModelProbe()
    with probe, client.websocket_connect("/api/ws") as ws:
        ws.send_text(json.dumps({
            "type": "send_message",
            "session_id": session_id,
            "content": "继续",
            "max_steps": 8,
            "budget": {"local": {"max_agent_turns": 9}},
        }))
        payload = json.loads(ws.receive_text())
    assert payload["type"] == "error", payload
    assert "8" in payload["message"] and "9" in payload["message"]
    after = [e["type"] for e in client.get(f"/api/sessions/{session_id}/events").json()]
    assert after == before, "被拒帧不得改动会话历史"
    assert probe.calls == []
