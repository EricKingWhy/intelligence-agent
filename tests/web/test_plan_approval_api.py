"""#1356 方案 3：计划审批的恢复链路（web 全链路）。

钉住三件与票面 AC 一一对应的事：

- **AC6（硬顺序）**：批准时 `workflow/mode-changed(normal)` 的 seq 严格早于恢复 run
  的任何事件，且恢复后第一个写工具**不被** PLAN 门禁拦截。这条与 #1193 同构
  （"mode 是 run 的初始条件"）——放行时现派生 mode，必须已经读到 normal。
- **AC7**：要求修改 / 拒绝**不切档**，模型收到含用户反馈原文的 user 消息。
- 形状：`extra="forbid"` 下拼错字段即 422（wire 契约）。

与 #1193 的边界（代码里也有注释）：那里的裁决是"**异常路径**不得自动回滚 mode"；
这里的切档发生在用户显式点"批准"之后，是 resume 请求的一部分，不是异常臂。
"""

from __future__ import annotations

import json

from langchain_core.messages import AIMessage

from agent_harness.session.event import TASK_PLAN_UPDATED
from agent_harness.session.plan import derive_plan
from tests.web.test_budget_local_fuse_api import _web
from tests.web.test_constraint_resolution_api import _ModelProbe

_PLAN = "# 计划\n\n## 目标\n把 A 改成 B。\n\n## 分步改动\n1. 改 a.py\n2. 跑测试\n"
_MODE_EVENT = "workflow/mode-changed"
PLAN_GATE_MESSAGE = "会话处于 Plan 模式"


def _web_run(tmp_path):
    return _web(tmp_path)


def _submit_plan_call(call_id: str = "plan-1") -> AIMessage:
    return AIMessage(content="", tool_calls=[{
        "id": call_id,
        "name": "submit_plan",
        "args": {"plan_markdown": _PLAN, "touched_files": ["src/a.py"]},
    }])


def _write_call() -> AIMessage:
    return AIMessage(content="", tool_calls=[{
        "id": "write-1",
        "name": "write",
        "args": {"path": "note.txt", "content": "hello"},
    }])


def _probe(*, resumed: list[AIMessage]) -> _ModelProbe:
    return _ModelProbe([[_submit_plan_call()], resumed])


def _events(client, session_id) -> list[dict]:
    resp = client.get(f"/api/sessions/{session_id}/events")
    assert resp.status_code == 200, resp.text
    return resp.json()


def _pause(client, session_id):
    events = _events(client, session_id)
    request = next(e for e in events if e["type"] == "user/input-requested")
    pause = next(e for e in events if e["type"] == "run/paused")
    assert request["data"]["kind"] == "plan_approval"
    assert pause["data"]["reason"] == "user_input"
    assert pause["data"]["input_request_id"] == request["data"]["request_id"]
    return request, pause


def _start(client):
    """autonomy=plan 建会话（plan 档是 run 的初始条件，见 #1193），run 跑到 submit_plan 暂停。"""
    return client.post("/api/sessions", json={
        "task": "先调研，再交计划。",
        "autonomy": "plan",
        "sandbox_backend": "local",
        "budget": {
            "run": {"max_agent_turns_total": 8, "max_model_requests": 8},
            "local": {"max_agent_turns": 8},
        },
    })


def _session_id(resp) -> str:
    frames = [
        json.loads(line[len("data:"):].strip())
        for line in resp.text.splitlines() if line.startswith("data:")
    ]
    sid = next((f["session_id"] for f in frames if f.get("session_id")), None)
    assert sid, f"SSE 流里没有 session_id：{frames[:3]}"
    return str(sid)


def _resume_for(client, session_id, pause, request, input_request):
    return client.post(
        f"/api/sessions/{session_id}/resume",
        json={
            "run_id": pause["run_id"],
            "resume_basis": "user_input",
            "budget": {"expected_version": pause["data"]["budget_version"], "run": {}},
            "input_request": {"request_id": request["data"]["request_id"], **input_request},
        },
    )


def _tool_result(events: list[dict], call_id: str) -> dict:
    event = next(
        e for e in events
        if e["type"] == "tool/result" and e["data"]["tool_call_id"] == call_id
    )
    return json.loads(event["data"]["content"])


def test_approve_switches_to_normal_before_the_resumed_run(tmp_path):
    """AC6：mode(normal) 的 seq 严格早于恢复 run 的任何事件，写工具随即放行。

    AC6 原文提到"用 monkeypatch 放大窗口"（仿 #1193 范式）。本实现**不需要**：
    最终预算检查与两个 append（`mode(normal)` + 答复 `user/message`）之间没有任何
    await，恢复 run 的全部事件又都在 `_commit` 返回之后才产生——窗口在结构上不存在，
    没有可以放大到可观测的东西。事后 seq 断言（下面 `normal_seq < run_resumed_seq`
    与"恢复 run 事件全部 > normal_seq"）已完整钉死 AC6 语义。
    """
    _app, client = _web_run(tmp_path)
    probe = _probe(resumed=[_write_call(), AIMessage(content="done")])

    with probe:
        started = _start(client)
        assert started.status_code == 200, started.text
        session_id = _session_id(started)
        request, pause = _pause(client, session_id)

        resumed = _resume_for(
            client, session_id, pause, request, {"decision": "approve"},
        )
        assert resumed.status_code == 200, resumed.text

    events = _events(client, session_id)
    normal_seqs = [
        int(e["seq"]) for e in events
        if e["type"] == _MODE_EVENT and e["data"].get("mode") == "normal"
    ]
    assert len(normal_seqs) == 1, f"批准必须恰好落一条 normal：{normal_seqs}"
    normal_seq = normal_seqs[0]

    run_resumed_seq = next(e["seq"] for e in events if e["type"] == "run/resumed")
    assert normal_seq < run_resumed_seq, (
        "批准时切档必须早于恢复 run（mode 是 run 的初始条件）："
        f"normal seq={normal_seq} run/resumed seq={run_resumed_seq}"
    )
    # 恢复 run 的所有事件都在切档之后（含答复 user 消息与写工具结果）。
    resumed_run_id = next(e["run_id"] for e in events if e["type"] == "run/resumed")
    for event in events:
        if (
            event.get("run_id") == resumed_run_id
            and int(event["seq"]) > run_resumed_seq
        ):
            assert int(event["seq"]) > normal_seq

    result = _tool_result(events, "write-1")
    assert result["ok"] is True, f"批准后写工具仍被拦：{result}"

    answer = next(
        e for e in events
        if e["type"] == "user/message"
        and e["data"].get("input_request_id") == request["data"]["request_id"]
    )
    assert answer["data"]["input_request_answer"] == {
        "request_id": request["data"]["request_id"], "decision": "approve",
    }
    assert answer["data"]["content"].startswith("计划已批准，现在开始执行。")
    assert request["data"]["artifact_id"] in answer["data"]["content"]


def test_revise_keeps_plan_mode_and_echoes_feedback(tmp_path):
    """AC7：要求修改不切档；模型收到反馈原文；可再次调用 submit_plan。"""
    _app, client = _web_run(tmp_path)
    feedback = "第 2 步改成先写测试。"
    probe = _probe(resumed=[_submit_plan_call("plan-2")])

    with probe:
        started = _start(client)
        assert started.status_code == 200, started.text
        session_id = _session_id(started)
        request, pause = _pause(client, session_id)

        resumed = _resume_for(
            client, session_id, pause, request,
            {"decision": "revise", "feedback": feedback},
        )
        assert resumed.status_code == 200, resumed.text

    events = _events(client, session_id)
    assert not [
        e for e in events
        if e["type"] == _MODE_EVENT and e["data"].get("mode") == "normal"
    ], "要求修改不得切档"

    resumed_messages = "\n".join(
        str(m.content) for m in probe.models[1].snapshots[0].messages
    )
    assert feedback in resumed_messages
    assert "submit_plan" in resumed_messages

    # 再次提交计划：每 run 单请求去重按「已答复」放行（新 request_id）。
    requests = [e for e in events if e["type"] == "user/input-requested"]
    assert len(requests) == 2, f"第二次 submit_plan 应落新请求：{len(requests)}"
    assert requests[0]["data"]["request_id"] != requests[1]["data"]["request_id"]


def test_reject_keeps_plan_mode(tmp_path):
    """AC7：拒绝不切档；模型收到拒绝理由。"""
    _app, client = _web_run(tmp_path)
    feedback = "方向不对，先别动文件。"
    probe = _probe(resumed=[AIMessage(content="好的，我重新调研。")])

    with probe:
        started = _start(client)
        assert started.status_code == 200, started.text
        session_id = _session_id(started)
        request, pause = _pause(client, session_id)

        resumed = _resume_for(
            client, session_id, pause, request,
            {"decision": "reject", "feedback": feedback},
        )
        assert resumed.status_code == 200, resumed.text

    events = _events(client, session_id)
    assert not [
        e for e in events
        if e["type"] == _MODE_EVENT and e["data"].get("mode") == "normal"
    ], "拒绝不得切档"
    resumed_messages = "\n".join(
        str(m.content) for m in probe.models[1].snapshots[0].messages
    )
    assert feedback in resumed_messages
    assert "用户拒绝了该计划" in resumed_messages


def test_new_message_while_plan_approval_pending_says_plan_not_question(tmp_path):
    """#1356 审查 P2-2：待审批计划时发新消息 → 409 文案说「计划/裁决」。

    同一时刻前端 composer 的锁定条写的是「请先裁决当前计划」，后端若沿用通用
    提问的中性句，用户会看到同一状态的两个名字。整句相等断言（弱断言钉不住
    动作子句），并复核零副作用。
    """
    _app, client = _web_run(tmp_path)
    probe = _probe(resumed=[AIMessage(content="ok")])

    with probe:
        started = _start(client)
        assert started.status_code == 200, started.text
        session_id = _session_id(started)
        _request, pause = _pause(client, session_id)
        del pause
        before = _events(client, session_id)

        blocked = client.post(
            f"/api/sessions/{session_id}/messages",
            json={"content": "先做别的。"},
        )

    assert blocked.status_code == 409, blocked.text
    assert blocked.json()["detail"] == (
        "当前有待裁决的计划；请先裁决该计划，再发送新消息"
    )
    assert _events(client, session_id) == before


def test_plan_approval_and_update_plan_are_orthogonal(tmp_path):
    """AC11：批准后再多次 `update_plan`——两类事件并存且互不触发。

    `submit_plan` 落 `user/input-requested(kind=plan_approval)`，`update_plan` 落
    `task/plan_updated`；`ConversationState.plan` 只由后者驱动。这条钉的是**事件流
    层面**的正交性：审批请求不因为清单更新而变化（不新增、不消失），清单投影也
    只读 `task/plan_updated`。
    """
    _app, client = _web_run(tmp_path)
    first_items = [
        {"id": "1", "content": "改 a.py", "activeForm": "改 a.py",
         "status": "in_progress", "source": "agent"},
    ]
    second_items = [
        {"id": "1", "content": "改 a.py", "activeForm": "改 a.py",
         "status": "completed", "source": "agent"},
        {"id": "2", "content": "跑测试", "activeForm": "跑测试",
         "status": "in_progress", "source": "agent"},
    ]
    probe = _probe(resumed=[
        AIMessage(content="", tool_calls=[{
            "id": "plan-upd-1", "name": "update_plan", "args": {"items": first_items},
        }]),
        AIMessage(content="", tool_calls=[{
            "id": "plan-upd-2", "name": "update_plan", "args": {"items": second_items},
        }]),
        AIMessage(content="done"),
    ])

    with probe:
        started = _start(client)
        assert started.status_code == 200, started.text
        session_id = _session_id(started)
        request, pause = _pause(client, session_id)

        resumed = _resume_for(
            client, session_id, pause, request, {"decision": "approve"},
        )
        assert resumed.status_code == 200, resumed.text

    events = _events(client, session_id)
    approvals = [
        e for e in events
        if e["type"] == "user/input-requested"
        and e["data"].get("kind") == "plan_approval"
    ]
    assert len(approvals) == 1, "清单更新不得新增/抹掉审批请求"
    assert approvals[0]["data"]["request_id"] == request["data"]["request_id"]

    plan_updates = [e for e in events if e["type"] == TASK_PLAN_UPDATED]
    assert len(plan_updates) == 2
    assert [e["data"]["items"] for e in plan_updates] == [first_items, second_items]
    # 投影只由 `task/plan_updated` 驱动：last-wins = 第二次整表。
    plan = derive_plan(_app.state.agent.store.read_events(session_id))
    assert [item.id for item in plan.items] == ["1", "2"]
    assert plan.items[0].status == "completed"


def test_misspelled_decision_field_is_422(tmp_path):
    """`extra="forbid"`：拼错字段直接 422，不静默忽略。"""
    _app, client = _web_run(tmp_path)
    probe = _probe(resumed=[AIMessage(content="ok")])

    with probe:
        started = _start(client)
        assert started.status_code == 200, started.text
        session_id = _session_id(started)
        request, pause = _pause(client, session_id)

        resp = client.post(
            f"/api/sessions/{session_id}/resume",
            json={
                "run_id": pause["run_id"],
                "resume_basis": "user_input",
                "budget": {
                    "expected_version": pause["data"]["budget_version"], "run": {},
                },
                "input_request": {
                    "request_id": request["data"]["request_id"],
                    "decison": "approve",
                },
            },
        )
    assert resp.status_code == 422, resp.text
