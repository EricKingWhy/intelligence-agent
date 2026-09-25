"""T4（`#312`）HTTP 语义：低预算暂停 → 抬高绝对 ceiling → 同一 run_id 续跑完成。

车票 AC 里属于 HTTP / 服务边界的那几条：

- 暂停**可见**：`run/paused` 进事件流（durable）并镜像给 live SSE，且**不是**终态
  （没有 `run/completed` / `run/failed` / `run/interrupted`）；
- 恢复**沿用同一 run_id**（不新建 run、不落新 `user/message`），`run/resumed` 记
  `from_pause_seq` / `previous_budget_version` / `consumed` 快照 / `resume_basis` /
  绝对 ceiling；
- 状态对不上（版本过期 / run 对不上 / ceiling 没真提高 / 没有暂停态）⇒ **409**；
- 形状缺字段（`run_id` / `expected_version` / `resume_basis`）与"新任务 + 同 run 声明"
  混搭 ⇒ **422**；
- 被拒请求**零副作用**：不落任何事件、不构造模型（= 没有 model/tool/child 工作）。

领域规则的穷举单测在 `tests/agent/test_run_budget.py`（`validate_resume` 的 422/409
分界）；runtime 层跨执行的语义在 `tests/agent/test_run_pause_resume.py`。本文件只锁
HTTP + 服务这一层，并把"被拒请求不得开工"变成**可观测证据**（事件条数 + 模型构造次数）。

`run/resumed` 只进事件日志、不进 live 流（与既有 `session/resumed` 同一形状——两者都由
服务在 launch 之前落盘，订阅者那时还不存在）。所以客户端靠 GET /events 重放重建，这正是
AC 的 "refresh/replay reconstruction"。
"""

from __future__ import annotations

import json
from typing import Any, Self
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage

from tests.scripted_model import ScriptedModel
from tests.web.test_budget_local_fuse_api import _create_idle_session, _web

RUN_ID_KEY = "run_id"


def _continuation_json() -> AIMessage:
    """合法的 closeout 产出（模型 closeout 花掉预留那一轮）。"""
    return AIMessage(content=json.dumps({
        "completed": ["已读完配置"],
        "remaining": ["还要改 A", "再跑测试"],
        "blockers": [],
        "next_safe_action": "先改 A",
    }, ensure_ascii=False))


class _ScriptedProbe:
    """按序给每次**模型构造**发一份剧本，并记录构造次数。

    模型构造 = "这次请求真的开工了"的可观测定义（校验必须早于它）。
    剧本用尽时回一句通用答复并继续记录：这样"多构造了几次"会表现为
    `len(probe.calls)` 断言失败，而不是请求里冒出一个难读的 500。
    """

    def __init__(self, *scripts: list[AIMessage]) -> None:
        self._scripts = list(scripts)
        self.calls: list[Any] = []
        self._patcher: Any = None

    def __enter__(self) -> Self:
        def _factory(config: Any, **kwargs: Any) -> ScriptedModel:
            self.calls.append(config)
            responses = self._scripts.pop(0) if self._scripts else [AIMessage(content="ok")]
            return ScriptedModel(responses=responses)

        self._patcher = patch(
            "agent_harness.assembly.create_chat_model", side_effect=_factory,
        )
        self._patcher.start()
        return self

    def __exit__(self, *exc: object) -> bool:
        self._patcher.stop()
        return False


def _events(client: TestClient, session_id: str) -> list[dict]:
    resp = client.get(f"/api/sessions/{session_id}/events")
    assert resp.status_code == 200, resp.text
    return resp.json()


def _types(events: list[dict]) -> list[str]:
    return [e["type"] for e in events]


def _one(events: list[dict], event_type: str) -> dict:
    matched = [e for e in events if e["type"] == event_type]
    assert len(matched) == 1, f"{event_type} 应恰好一条，实际 {len(matched)}"
    return matched[0]


def _pause_the_run(
    client: TestClient, session_id: str, *, task: str = "把 A 改成 B",
    ceiling: int = 1,
) -> dict:
    """启动一个 ceiling 极低的 run，让它当场暂停；返回那条 `run/paused` 事件。"""
    resp = client.post(
        f"/api/sessions/{session_id}/messages",
        json={"content": task, "budget": {"run": {"max_agent_turns_total": ceiling}}},
    )
    assert resp.status_code == 200, resp.text
    assert "run/paused" in resp.text, resp.text[:400]
    events = _events(client, session_id)
    paused = _one(events, "run/paused")
    assert paused["data"]["trigger_dimension"] == "run.max_agent_turns_total"
    return paused


def _assert_no_new_work(client: TestClient, session_id: str, before: list[dict]) -> None:
    """被拒请求的零副作用：事件日志逐条相同（不落 `run/resumed`，更不落 model/tool 事件）。"""
    after = _events(client, session_id)
    assert after == before, "被拒请求不得改动会话历史（含 run/resumed）"
    assert [e["type"] for e in after].count("run/resumed") == 0


# ── 接受路径：暂停 → 抬高 ceiling → 同 run 完成 ──────────────────────────


def test_low_ceiling_pauses_then_raised_ceiling_completes_the_same_run(tmp_path):
    """AC 主线：ceiling=1 暂停（closeout 花掉预留那一轮）→ ceiling=4 恢复 → 同一个 run 完成。

    账（`02 §5.2`）：暂停时 consumed=1（0 个产出轮 + 1 次模型 closeout）；恢复要求
    `ceiling > consumed + 1`，所以 4 是合法起点，恢复后的执行再消耗 1 轮。
    """
    _, client = _web(tmp_path)
    session_id = _create_idle_session(client)
    probe = _ScriptedProbe([_continuation_json()], [AIMessage(content="A 已改完")])

    with probe:
        paused = _pause_the_run(client, session_id)
        run_id = paused[RUN_ID_KEY]
        assert paused["data"] == {
            "reason": "budget_exhausted",
            "trigger_dimension": "run.max_agent_turns_total",
            "budget_version": 1,
            "consumed": {"agent_turns": 1},
            "limits": {
                "local": {"max_agent_turns": 500, "source": "deployment"},
                "run": {"max_agent_turns_total": 1},
            },
            "continuation": {
                "completed": ["已读完配置"],
                "remaining": ["还要改 A", "再跑测试"],
                "blockers": [],
                "next_safe_action": "先改 A",
            },
            "closeout_source": "model",
            "resume_requirements": [],
            "trace_id": None,
        }

        # 暂停不是终态，且没有第二条 run/started（新 run 由 /resume 之后的执行接上）
        events = _events(client, session_id)
        assert _types(events).count("run/started") == 1
        for terminal in ("run/completed", "run/failed", "run/interrupted"):
            assert terminal not in _types(events)

        resp = client.post(
            f"/api/sessions/{session_id}/resume",
            json={
                RUN_ID_KEY: run_id,
                "resume_basis": "budget_increase",
                "budget": {"run": {"max_agent_turns_total": 4}, "expected_version": 1},
            },
        )
        assert resp.status_code == 200, resp.text
        assert "run/completed" in resp.text, resp.text[:400]

    assert len(probe.calls) == 2, "恰好两次执行：暂停那次 + 续跑那次（不多构造模型）"

    events = _events(client, session_id)
    types = _types(events)
    # run 身份单一：同 run 续跑不新建 run、不落新 user/message
    assert types.count("run/started") == 1
    assert types.count("user/message") == 1
    assert types.count("run/paused") == 1
    assert types.count("run/resumed") == 1
    assert types.count("run/completed") == 1
    assert {e[RUN_ID_KEY] for e in events if e.get(RUN_ID_KEY)} == {run_id}
    assert _one(events, "run/completed")["data"]["final_text"] == "A 已改完"

    resumed = _one(events, "run/resumed")
    assert resumed[RUN_ID_KEY] == run_id
    assert resumed["data"] == {
        "from_pause_seq": paused["seq"],
        "previous_budget_version": 1,
        "budget_version": 2,
        # 恢复**不重置**消耗：快照等于暂停那一刻的账（新工作之后由 model/completed 累加）
        "consumed": {"agent_turns": 1},
        "limits": {
            "local": {"max_agent_turns": 500, "source": "deployment"},
            "run": {"max_agent_turns_total": 4},
        },
        "resume_basis": "budget_increase",
    }
    assert "把 A 改成 B" in json.dumps(events, ensure_ascii=False)


def test_resumed_stream_does_not_replay_the_new_run_sequence(tmp_path):
    """续跑的 live 流从**本次执行**开始：没有第二条 `run/started`、没有新 `user/message`。

    这是"客户端不必自己拼 run 身份"的证据面：流里第一帧就是本次执行产生的
    durable 事件（`model/started` 是流式专属，不进事件日志）。重连/刷新走
    GET /events 全量重建（`11 §6.1`）。"""
    _, client = _web(tmp_path)
    session_id = _create_idle_session(client)
    probe = _ScriptedProbe([_continuation_json()], [AIMessage(content="收尾完成")])

    with probe:
        paused = _pause_the_run(client, session_id)
        resp = client.post(
            f"/api/sessions/{session_id}/resume",
            json={
                RUN_ID_KEY: paused[RUN_ID_KEY],
                "resume_basis": "budget_increase",
                "budget": {"run": {"max_agent_turns_total": 3}, "expected_version": 1},
            },
        )

    assert resp.status_code == 200, resp.text
    body = resp.text
    assert "run/started" not in body, "续跑不是新 run，流里不该有第二条 run/started"
    assert "user/message" not in body, "同 run 续跑不带新任务文本"
    assert "run/completed" in body


# ── 拒绝路径：409 / 422 且零副作用 ──────────────────────────────────────


def test_stale_expected_version_is_rejected_without_any_work(tmp_path):
    """版本过期 ⇒ 409、零副作用：不落事件、不构造模型（并发 CAS 的读侧拒绝）。"""
    _, client = _web(tmp_path)
    session_id = _create_idle_session(client)
    probe = _ScriptedProbe([_continuation_json()])

    with probe:
        paused = _pause_the_run(client, session_id)
        before = _events(client, session_id)
        calls_before = len(probe.calls)

        resp = client.post(
            f"/api/sessions/{session_id}/resume",
            json={
                RUN_ID_KEY: paused[RUN_ID_KEY],
                "resume_basis": "budget_increase",
                # 客户端看到的是版本 1，却声称自己看到 2（例如另一个客户端先恢复过）
                "budget": {"run": {"max_agent_turns_total": 4}, "expected_version": 2},
            },
        )

    assert resp.status_code == 409, resp.text
    assert "version" in resp.json()["detail"]
    assert len(probe.calls) == calls_before, "被拒请求不得构造模型"
    _assert_no_new_work(client, session_id, before)


def test_wrong_run_id_and_unraised_ceiling_are_rejected(tmp_path):
    """`run_id` 对不上 / ceiling 没真提高到能继续 ⇒ 409，且每种都零副作用。"""
    _, client = _web(tmp_path)
    session_id = _create_idle_session(client)
    probe = _ScriptedProbe([_continuation_json()])

    with probe:
        paused = _pause_the_run(client, session_id)
        before = _events(client, session_id)
        calls_before = len(probe.calls)
        cases = [
            # 不是被暂停的那个 run（客户端拼错了 id / 拿的是上一条 run 的）
            {RUN_ID_KEY: "run-does-not-exist", "budget": {"expected_version": 1}},
            # ceiling 恰好等于 consumed+1 ⇒ 下次准入立刻再次暂停，"恢复成功却什么都没发生"
            {RUN_ID_KEY: paused[RUN_ID_KEY], "budget": {"expected_version": 1}},
        ]
        ceilings = [4, 2]
        for case, ceiling in zip(cases, ceilings, strict=True):
            resp = client.post(
                f"/api/sessions/{session_id}/resume",
                json={
                    **case,
                    "resume_basis": "budget_increase",
                    "budget": {
                        "run": {"max_agent_turns_total": ceiling},
                        **case["budget"],
                    },
                },
            )
            assert resp.status_code == 409, resp.text

    assert len(probe.calls) == calls_before
    _assert_no_new_work(client, session_id, before)


def test_error_mapping_splits_shape_from_state(tmp_path):
    """形状缺字段 ⇒ 422；状态对不上 ⇒ 409（`11 §6.1` 的分界，逐一取证）。"""
    _, client = _web(tmp_path)
    session_id = _create_idle_session(client)
    probe = _ScriptedProbe([_continuation_json()], [AIMessage(content="done")])

    with probe:
        paused = _pause_the_run(client, session_id)
        run_id = paused[RUN_ID_KEY]
        before = _events(client, session_id)
        calls_before = len(probe.calls)
        base_budget: dict[str, Any] = {"run": {"max_agent_turns_total": 4}}

        shape_errors: list[dict[str, Any]] = [
            # 缺 run_id（请求 MUST 标识被暂停的 run）
            {"resume_basis": "budget_increase", "budget": {**base_budget, "expected_version": 1}},
            # 缺 expected_version（CAS 没有比较对象）
            {RUN_ID_KEY: run_id, "resume_basis": "budget_increase", "budget": base_budget},
            # 缺 resume_basis（`03 §3.4` 的必填声明）
            {RUN_ID_KEY: run_id, "budget": {**base_budget, "expected_version": 1}},
            # resume_basis 不是四值之一
            {
                RUN_ID_KEY: run_id, "resume_basis": "vibes",
                "budget": {**base_budget, "expected_version": 1},
            },
            # 新任务 + 同 run 声明混搭：两个动作，必须二选一
            {
                "task": "顺便再做点别的", RUN_ID_KEY: run_id,
                "resume_basis": "budget_increase",
                "budget": {**base_budget, "expected_version": 1},
            },
        ]
        for payload in shape_errors:
            resp = client.post(f"/api/sessions/{session_id}/resume", json=payload)
            assert resp.status_code == 422, f"{payload} 应 422：{resp.text}"

        # 409 一族：暂停原因不是预算（这里用"没有暂停态"代表状态对不上——
        # 恢复一个已完成的会话，客户端拿的是过期事实）
        resp = client.post(
            f"/api/sessions/{session_id}/resume",
            json={
                RUN_ID_KEY: run_id, "resume_basis": "budget_increase",
                "budget": {**base_budget, "expected_version": 1},
            },
        )
        assert resp.status_code == 200, resp.text  # 先真的恢复一次
        resp = client.post(
            f"/api/sessions/{session_id}/resume",
            json={
                RUN_ID_KEY: run_id, "resume_basis": "budget_increase",
                "budget": {**base_budget, "expected_version": 1},
            },
        )
        assert resp.status_code == 409, resp.text
        assert "暂停" in resp.json()["detail"]

    assert len(probe.calls) == calls_before + 1, "只有那次成功的恢复开工了"
    # 被拒请求（422 五条 + 409 一条）没有留下任何痕迹：事件流里只有那一次恢复
    events = _events(client, session_id)
    assert _types(events).count("run/resumed") == 1
    assert _types(events).count("run/started") == 1
    assert _types(events).count("user/message") == 1
    assert before == events[: len(before)], "被拒请求不得改动既有历史（前缀逐条相同）"


@pytest.mark.parametrize("reason", ["stuck", "deadline"])
def test_non_budget_pause_reason_is_rejected(tmp_path, reason):
    """暂停原因不是预算 ⇒ 409 且不给假成功（`#315`/`#317` 的责任域，本票不假装支持）。

    构造：直接手工落一条 `run/started` + `run/paused`（reason 非预算）——服务只读事件流，
    不关心它是谁写的，所以这条用例精确命中"原因闸门"这一条判定。
    """
    from agent_harness.session import Session
    from agent_harness.session.event import RUN_PAUSED, RUN_STARTED
    from agent_harness.session.store import JsonlSessionStore

    app, client = _web(tmp_path)
    session_id = _create_idle_session(client)
    store = JsonlSessionStore(root=app.state.agent.sessions_root)
    # 先有 run/started 才有"一个 run"（暂停总是挂在某个 run 上；缺了它这条事件
    # 只是孤儿行——投影按"最新 run"读，孤儿行不构成任何暂停事实）
    Session.append_event(
        store, session_id, RUN_STARTED,
        {"turn_index": 1, "agent_profile": "main",
         "budget": {"run": {"max_agent_turns_total": 8}}},
        run_id="run-stuck-1",
    )
    Session.append_event(
        store, session_id, RUN_PAUSED,
        {
            "reason": reason, "trigger_dimension": "stuck.no_progress",
            "budget_version": 1, "consumed": {"agent_turns": 3},
            "limits": {"local": {"max_agent_turns": 500, "source": "deployment"},
                       "run": {"max_agent_turns_total": 8}},
            "continuation": {}, "closeout_source": "deterministic",
            "resume_requirements": ["需要人工裁决"],
        },
        run_id="run-stuck-1",
    )
    before = _events(client, session_id)

    probe = _ScriptedProbe()
    with probe:
        resp = client.post(
            f"/api/sessions/{session_id}/resume",
            json={
                RUN_ID_KEY: "run-stuck-1", "resume_basis": "budget_increase",
                "budget": {"run": {"max_agent_turns_total": 9}, "expected_version": 1},
            },
        )

    assert resp.status_code == 409, resp.text
    assert reason in resp.json()["detail"]
    assert probe.calls == []
    _assert_no_new_work(client, session_id, before)
