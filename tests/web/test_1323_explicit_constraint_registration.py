"""#1323：勾选「作为可复用规则」→ 会话级约束的确定性登记（HTTP 全链路）。

复现票面症状的路径：真实 `SessionService.send_message` + 真实 `AgentRuntime`，
模型替身只回一句 `done`（**从不调用 `register_constraint`**）——修复前 0/5，
修复后 5/5。覆盖 AC1–AC4/AC6 与 project/global 的分流边界（AC5 的回归面由
`tests/session/test_scoped_constraints.py` 与 `tests/web/test_scoped_constraints_api.py`
原样承担）。
"""

from __future__ import annotations

import time

from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage

from agent_harness.config import Settings
from agent_harness.session.event import TASK_PROTECTED_FACT, USER_MESSAGE
from agent_harness.session.scoped_constraints import ConstraintScope
from agent_harness.web.app import create_app
from tests.scripted_model import ScriptedModel

_BUDGET = {"local": {"max_agent_turns": 1}}


def _app(tmp_path, monkeypatch):
    """真实 app；模型永远不调工具（这正是修复前的失败场景）。"""
    monkeypatch.setattr(
        "agent_harness.assembly.create_chat_model",
        lambda *args, **kwargs: ScriptedModel(
            [AIMessage(content="done")] * 3
        ),
    )
    return create_app(Settings(
        _env_file=None,
        workspace_dir=str(tmp_path),
        model_api_key="test-key",
        model_name="deepseek-chat",
        model_provider="deepseek",
        capabilities="{}",
        enable_cors=False,
    ))


def _wait_for_run(
    store, session_id: str, minimum_completed: int, timeout: float = 20.0,
) -> None:
    """等 run/completed 累计到 `minimum_completed`；失败/暂停直接报出来。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        events = store.read_events(session_id)
        completed = sum(
            1 for event in events if event.type == "run/completed"
        )
        if completed >= minimum_completed:
            return
        terminal = next(
            (event.type for event in events
             if event.type in ("run/failed", "run/paused")),
            None,
        )
        if terminal is not None:
            raise AssertionError(f"run 未完成：{terminal}")
        time.sleep(0.05)
    raise AssertionError("等待 run/completed 超时")


def _send(client: TestClient, session_id: str, content: str, **extra) -> None:
    response = client.post(
        f"/api/sessions/{session_id}/messages",
        json={"content": content, "mode": "queue", "budget": _BUDGET, **extra},
    )
    assert response.status_code == 200, response.text


def _facts(store, session_id: str):
    return [
        event for event in store.read_events(session_id)
        if event.type == TASK_PROTECTED_FACT
        and event.data.get("fact_type") == "constraint"
    ]


def test_explicit_session_rule_registers_five_of_five(tmp_path, monkeypatch) -> None:
    """AC1/AC2：旧会话 3 次 + 新会话 2 次，5/5 必现且正文逐字一致（AC3）。"""
    app = _app(tmp_path, monkeypatch)
    old_rules = [
        "合并前必须跑完所有测试。",
        "提交信息里必须写明对应的 issue 号。",
        "不要自动推送 main 分支。",
    ]
    new_rules = [
        "回复代码审查意见时先给结论。",
        "不要用 git stash。",
    ]

    with TestClient(app) as client:
        store = app.state.agent.store
        old_session = client.post("/api/sessions?launch=false", json={}).json()["session_id"]
        # 先制造历史，模拟票面的「已有历史会话」。
        completed = 0
        _send(client, old_session, "先热身一下。")
        completed += 1
        _wait_for_run(store, old_session, completed)
        for rule in old_rules:
            _send(
                client, old_session, rule,
                remember_as_procedural_rule=True,
            )
            completed += 1
            _wait_for_run(store, old_session, completed)

        # 新会话：第一条走真实创建路径（前端 submitTask），第二条续聊。
        created = client.post(
            "/api/sessions",
            json={
                "task": new_rules[0],
                "remember_as_procedural_rule": True,
                "budget": _BUDGET,
            },
        )
        assert created.status_code == 200, created.text
        new_session = created.json()["session_id"]
        completed = 0
        completed += 1
        _wait_for_run(store, new_session, completed)
        _send(client, new_session, new_rules[1], remember_as_procedural_rule=True)
        completed += 1
        _wait_for_run(store, new_session, completed)

    print(f"AC2 session ids: old={old_session} new={new_session}")
    old_values = [event.data["value"] for event in _facts(store, old_session)]
    new_values = [event.data["value"] for event in _facts(store, new_session)]
    assert old_values == old_rules, f"旧会话读数 {len(old_values)}/3"
    assert new_values == new_rules, f"新会话读数 {len(new_values)}/2"
    # 逐字一致（AC3）：上面按顺序逐条相等已证明，不额外派生改写。

    # 每条约束的来源必须是那条真实 user/message（可对账，不是凭空造事件）。
    for session_id, rules in (
        (old_session, old_rules), (new_session, new_rules),
    ):
        user_events = [
            event for event in store.read_events(session_id)
            if event.type == USER_MESSAGE
            and event.data.get("remember_as_procedural_rule") is True
        ]
        assert [event.data["content"] for event in user_events] == rules
        assert [event.event_id for event in user_events] == [
            fact.data["source_event_id"] for fact in _facts(store, session_id)
        ]


def test_unchecked_message_does_not_register(tmp_path, monkeypatch) -> None:
    """AC4：未勾选 ⇒ 不产生会话级约束。"""
    app = _app(tmp_path, monkeypatch)
    with TestClient(app) as client:
        store = app.state.agent.store
        session_id = client.post("/api/sessions?launch=false", json={}).json()["session_id"]
        _send(client, session_id, "这条只是普通消息，不该被登记。")
        _wait_for_run(store, session_id, 1)

    assert _facts(store, session_id) == []


def test_global_scope_does_not_also_register_a_session_fact(
    tmp_path, monkeypatch,
) -> None:
    """AC5 边界：全局档只走规则层，不得在 SESSION 组多出一条。"""
    app = _app(tmp_path, monkeypatch)
    content = "所有项目都不要自动合并 PR。"
    with TestClient(app) as client:
        store = app.state.agent.store
        session_id = client.post("/api/sessions?launch=false", json={}).json()["session_id"]
        _send(
            client, session_id, content,
            remember_as_procedural_rule=True,
            protected_facts=[
                {"fact_type": "constraint", "value": content, "scope": "global"},
            ],
        )
        _wait_for_run(store, session_id, 1)

    assert _facts(store, session_id) == []
    global_rows = app.state.agent.scoped_constraint_store.list(
        ConstraintScope.GLOBAL.value,
    )
    assert [row.value for row in global_rows] == [content]


def test_rejected_steer_leaves_no_constraint(tmp_path, monkeypatch) -> None:
    """AC6：发送被拒（idle 上 steer）⇒ 不落 user/message、不登记任何约束。"""
    app = _app(tmp_path, monkeypatch)
    with TestClient(app) as client:
        store = app.state.agent.store
        session_id = client.post("/api/sessions?launch=false", json={}).json()["session_id"]
        response = client.post(
            f"/api/sessions/{session_id}/messages",
            json={
                "content": "这条不该被登记。",
                "mode": "steer",
                "remember_as_procedural_rule": True,
            },
        )
        assert response.status_code == 409, response.text

    assert _facts(store, session_id) == []
    assert not any(
        event.type == USER_MESSAGE for event in store.read_events(session_id)
    )
