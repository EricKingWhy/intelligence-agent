"""W-22（#366）HTTP 语义：`client_absent` 暂停 ⇄ `client_return` 显式恢复（一成一拒）。

本文件锁 HTTP + 服务这一层的**扩展值**（`02 §5.2.1` / `11 §6.2` / ADR-0046）：

- `resume_basis=client_return` 只对 `run/paused(reason=client_absent)` 有效；
  重连 / 刷新不自动恢复——显式请求就是那个显式动作；
- 两个续跑请求至多一个成功：CAS（budget.expected_version）在第一个恢复后 +1，
  第二个同版本请求 ⇒ 409 且**零副作用**；
- 形状合法、状态对不上（client_absent 暂停收到 `budget_increase`）⇒ 409。

422/409 分界与"被拒请求不得开工"的穷举钉在 `tests/agent/test_run_budget.py` 与
`tests/web/test_run_pause_resume_api.py`（#312/#313 的既有面，本票不改它们）。

client_absent 暂停事件由**契约构造器**（`build_pause_data`）落进真实 store：
生产里这条暂停来自 W-12 要接的在场协议（W-22 的 API 面不新增触发入口——登记
协议属 W-12），而恢复走的是从 #312 起就存在的 resume 端点，零改动。
"""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage

from agent_harness.agent.budget import LocalFuse
from agent_harness.agent.run_budget import (
    BudgetConsumed,
    RunLimits,
    build_limits_snapshot,
    build_pause_data,
)
from agent_harness.session import (
    MODEL_COMPLETED,
    MODEL_REQUEST,
    RUN_PAUSED,
    RUN_RESUMED,
    RUN_STARTED,
    Session,
)
from agent_harness.session.store import JsonlSessionStore
from tests.web.test_budget_local_fuse_api import _create_idle_session, _web
from tests.web.test_run_pause_resume_api import _ScriptedProbe

RUN_ID = "run-w22"

_CONTINUATION = {
    "completed": ["已消耗 1 个 agent turn"],
    "remaining": ["恢复后由模型从会话历史继续"],
    "blockers": ["最后一个产品客户端已明确退出或断线宽限到期"],
    "next_safe_action": "待客户端回归在场后以同一 run_id 恢复",
}


def _seed_client_absent_pause(app: Any, session_id: str) -> dict:
    """把一条真实形状的 client_absent 暂停序列落进 web app 的 store。

    事件账与快照账**逐格一致**（run/started + model/request + model/completed
    ⇒ turns=1 / requests=1 / tokens=15）：快照是恢复的基数，两套账不能各说各话
    （`02 §5.1` 的计数点纪律）。"""
    store = JsonlSessionStore(root=app.state.agent.sessions_root)
    Session.append_event(store, session_id, RUN_STARTED, {}, run_id=RUN_ID)
    Session.append_event(
        store, session_id, MODEL_REQUEST,
        {"role": "primary", "outcome": "completed", "usage": {"total_tokens": 15}},
        run_id=RUN_ID,
    )
    Session.append_event(store, session_id, MODEL_COMPLETED, {}, run_id=RUN_ID)
    paused = Session.append_event(
        store, session_id, RUN_PAUSED, build_pause_data(
            reason="client_absent",
            trigger_dimension="client_presence",
            version=1,
            consumed=BudgetConsumed(agent_turns=1, model_requests=1, total_tokens=15),
            limits=build_limits_snapshot(
                run_limits=RunLimits(),
                local_fuse=LocalFuse(max_agent_turns=500, source="deployment"),
            ),
            continuation=dict(_CONTINUATION),
            closeout_source="deterministic",
        ),
        run_id=RUN_ID,
    )
    return paused.as_projection() if hasattr(paused, "as_projection") else dict(paused.data)


def _events(client: TestClient, session_id: str) -> list[dict]:
    resp = client.get(f"/api/sessions/{session_id}/events")
    assert resp.status_code == 200, resp.text
    return resp.json()


def _resume_body(version: int, *, basis: str = "client_return") -> dict:
    return {
        "run_id": RUN_ID,
        "resume_basis": basis,
        "budget": {"expected_version": version},
    }


def test_client_return_resume_completes_then_stale_version_is_rejected(tmp_path):
    """AC 主线：client_absent 暂停 → client_return 恢复（200，同 run_id、consumed
    不重置）→ 第二个同版本续跑请求 409 且零副作用（"两个续跑至多一个成功"）。"""
    app, client = _web(tmp_path)
    session_id = _create_idle_session(client)
    _seed_client_absent_pause(app, session_id)
    probe = _ScriptedProbe([AIMessage(content="回来继续，已完成")])  # 恢复执行的剧本

    with probe:
        resp = client.post(
            f"/api/sessions/{session_id}/resume", json=_resume_body(1),
        )
        assert resp.status_code == 200, resp.text
        assert "run/completed" in resp.text, resp.text[:400]
        assert len(probe.calls) == 1, "只有恢复这一次执行构造了模型"

    events = _events(client, session_id)
    resumed = [e for e in events if e["type"] == RUN_RESUMED]
    assert len(resumed) == 1
    data = resumed[0]["data"]
    assert data["resume_basis"] == "client_return"
    assert data["budget_version"] == 2
    assert data["previous_budget_version"] == 1
    assert resumed[0]["run_id"] == RUN_ID, "恢复沿用同一 run_id（不新建）"
    assert data["consumed"] == {
        "agent_turns": 1, "model_requests": 1, "total_tokens": 15,
        "cost_usd": "0", "tool_calls": 0, "tool_attempts": 0,
        "tool_calls_by_tool": {}, "tool_attempts_by_tool": {},
    }, "consumed 等于暂停快照：恢复不重置、也不预支"

    # 同版本续跑（例如另一个客户端先恢复过）：CAS 过期 ⇒ 409 + 零副作用。
    before = _events(client, session_id)
    resp = client.post(
        f"/api/sessions/{session_id}/resume", json=_resume_body(1),
    )
    assert resp.status_code == 409, resp.text
    assert _events(client, session_id) == before, "被拒请求不得改动会话历史"


def test_budget_increase_is_rejected_for_client_absent_pause(tmp_path):
    """client_absent 只认 client_return：形状合法的 budget_increase 请求在状态面
    被 409（不是 422），且零副作用——与纯函数层判定同源（validate_resume）。"""
    app, client = _web(tmp_path)
    session_id = _create_idle_session(client)
    _seed_client_absent_pause(app, session_id)

    probe = _ScriptedProbe()
    with probe:
        resp = client.post(
            f"/api/sessions/{session_id}/resume", json=_resume_body(1, basis="budget_increase"),
        )
    assert resp.status_code == 409, resp.text
    assert "client_return" in resp.text, "拒绝文案要指出唯一有效的依据"
    assert probe.calls == [], "被拒请求不得构造模型（校验先于开工）"
    assert [e for e in _events(client, session_id) if e["type"] == RUN_RESUMED] == []
