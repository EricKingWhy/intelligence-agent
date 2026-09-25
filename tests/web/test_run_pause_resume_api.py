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
    """AC 主线：ceiling=1 当场暂停 → ceiling=4 恢复 → 同一个 run 完成并接上真实工作。

    账（`02 §5.1` 的七个 counter 互不混同）：`ceiling=1` 连一个产出轮都放行不了
    （判定含预留：`0 + 1 >= 1`），所以暂停时 `consumed.agent_turns == 0`；closeout
    那一次模型调用是 `model_requests`，**不**进这个 counter（它的位置由预留表达）。
    恢复要求 `ceiling > consumed + 预留`，所以 4 是合法起点，恢复后的执行才真正产出。
    """
    _, client = _web(tmp_path)
    session_id = _create_idle_session(client)
    probe = _ScriptedProbe([_continuation_json()], [AIMessage(content="A 已改完")])

    with probe:
        paused = _pause_the_run(client, session_id)
        run_id = paused[RUN_ID_KEY]
        data = dict(paused["data"])
        # trace_id 恒在这条事件上（`03 §3.4` 的归因面）。它的**值**取决于观测端口
        # 是否装配（NullTracer ⇒ None，别的用例开了 sink 则是真 trace id）⇒ 只钉
        # "键在 + 形状对"，钉死 None 会让本用例在整套同跑时误红。
        assert "trace_id" in data, "暂停事件必须带本次执行的 trace 归因面"
        trace_id = data.pop("trace_id")
        assert trace_id is None or isinstance(trace_id, str), trace_id
        assert data == {
            "reason": "budget_exhausted",
            "trigger_dimension": "run.max_agent_turns_total",
            "budget_version": 1,
            "consumed": {"agent_turns": 0},
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
        "consumed": {"agent_turns": 0},
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
            # ceiling 恰好等于 consumed + 预留（这里 consumed=0 ⇒ 1）⇒ 下次准入立刻再次
            # 暂停，"恢复成功却什么都没发生"。它同时是暂停前的那个值 ⇒ 没有真提高。
            {RUN_ID_KEY: paused[RUN_ID_KEY], "budget": {"expected_version": 1}},
        ]
        ceilings = [4, 1]
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


# ── AC-8：并发恢复只允许一个赢家 ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_concurrent_resume_with_the_same_version_has_exactly_one_winner(
    tmp_path, monkeypatch,
):
    """两个同 `expected_version` 的 resume 并发 ⇒ 恰好一个 200、一个 409、一条 `run/resumed`（AC-8）。

    为什么必须是**真并发**而不是"先后发两次"：`expected_version` 是 CAS 的比较对象
    （`03 §3.4`），它只在"读 version → 写 `run/resumed`"之间不被别人插进来时才成立。
    顺序发两次永远不会同时读到同一个 version，也就测不到那把锁——上面
    `test_stale_expected_version_is_rejected_without_any_work` 只证明"陈旧值被拒"，
    证明不了"同一版本至多一个成功者"。所以这里起真 uvicorn（真并发连接）并对齐发令。

    `TestClient` 换成真服务器是必要的：它的同步请求模型让"两个请求同时在锁上排队"
    这个窗口不可构造。
    """
    import asyncio

    import httpx2
    import uvicorn

    from agent_harness.config import Settings
    from agent_harness.web.app import create_app

    def _factory(config: Any, **kwargs: Any) -> ScriptedModel:
        # 每次**执行**装配模型：第一条剧本给那次低预算执行（它只做 closeout），
        # 之后的执行（唯一赢家那次）拿到收尾回答。
        return ScriptedModel([AIMessage(content="A 已改完")])

    monkeypatch.setattr("agent_harness.assembly.create_chat_model", _factory)
    app = create_app(Settings(
        _env_file=None, workspace_dir=str(tmp_path),
        model_api_key="sk-test", enable_cors=False,
    ))
    server = uvicorn.Server(uvicorn.Config(
        app, host="127.0.0.1", port=0, log_level="error", lifespan="on",
    ))
    serve_task = asyncio.create_task(server.serve())
    try:
        for _ in range(100):
            if server.started:
                break
            await asyncio.sleep(0.05)
        assert server.started, "测试服务器未起来"
        port = server.servers[0].sockets[0].getsockname()[1]
        base = f"http://127.0.0.1:{port}"

        async with httpx2.AsyncClient(timeout=20.0) as client:
            created = await client.post(
                f"{base}/api/sessions", json={}, params={"launch": "false"},
            )
            assert created.status_code == 200, created.text
            session_id = created.json()["session_id"]

            launched = await client.post(
                f"{base}/api/sessions/{session_id}/messages",
                json={
                    "content": "把 A 改成 B",
                    "budget": {"run": {"max_agent_turns_total": 1}},
                },
            )
            assert launched.status_code == 200, launched.text
            assert "run/paused" in launched.text, launched.text[:400]
            pause_body = [
                json.loads(line.removeprefix("data:").strip())
                for line in launched.text.splitlines() if line.startswith("data:")
            ]
            paused = next(f for f in pause_body if f.get("type") == "run/paused")
            payload = {
                RUN_ID_KEY: paused["run_id"],
                "resume_basis": "budget_increase",
                "budget": {"run": {"max_agent_turns_total": 4}, "expected_version": 1},
            }
            # 对齐发令：两个请求同时进入服务（同一 run_id / 同一版本 / 同一 ceiling）
            first, second = await asyncio.gather(
                client.post(f"{base}/api/sessions/{session_id}/resume", json=payload),
                client.post(f"{base}/api/sessions/{session_id}/resume", json=payload),
            )
            statuses = sorted([first.status_code, second.status_code])

        assert statuses == [200, 409], (
            f"并发恢复必须恰好一个赢家：{first.status_code} / {second.status_code}"
            f" · {first.text[:200]} · {second.text[:200]}"
        )
        loser = first if first.status_code == 409 else second
        assert "version" in loser.json()["detail"] or "暂停" in loser.json()["detail"]
        assert "data:" not in loser.text, "被拒的请求不是一条流（没有开工）"

        events = app.state.agent.store.read_events(session_id)
        types = [event.type for event in events]
        # `run/resumed` 在任何工作开始**之前**落盘 ⇒ 它恰好一条就是"只有一个赢家开工"
        # 的结构证据（第二次生效的恢复必然先写第二条，无论它后面跑成什么样）。
        assert types.count("run/resumed") == 1, "同一版本至多一个成功者（CAS 的唯一胜者）"
        assert types.count("run/paused") == 1
        assert types.count("run/started") == 1
        assert types.count("run/completed") == 1
        # 只跑了一次产出轮（暂停那次执行 0 轮：ceiling=1 连一轮都不放行；赢家 1 轮）。
        # 若有第二个执行被启动，这里会出现第二条 model/completed。
        assert types.count("model/completed") == 1, types
    finally:
        server.should_exit = True
        serve_task.cancel()
        try:
            await serve_task
        except asyncio.CancelledError:
            pass
