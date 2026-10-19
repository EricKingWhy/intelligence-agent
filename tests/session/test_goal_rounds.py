"""#1359 阶段 3：目标轮次续跑（AC7 / AC8 / AC9）。

三层断言：

- **纯函数**：`<goal_round>` 提示词、round_started 状态机、tool digest 无进展判据；
- **服务准入**：`_maybe_queue_goal_round` 的四个条件（active / armed / 轮次 /
  权限档）与"已有待投递 goal 轮次不重复入队"；
- **端到端**：真实 AppState + RunManager 终态接线 + 假模型，连续两轮空工具活动
  → goal paused（AC9），且 `rounds_started` 只在投递后增长（AC8）。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage

from agent_harness.agent.budget import BudgetConflict
from agent_harness.config import Settings
from agent_harness.model.scripted import ScriptedModel
from agent_harness.session import Session
from agent_harness.session.event import (
    MESSAGE_QUEUED,
    QUEUE_CONSUMED,
    RUN_STARTED,
    TOOL_CALL,
    TOOL_RESULT,
    USER_MESSAGE,
)
from agent_harness.session.goal import (
    APPROVAL_REQUIRED_CODE,
    GOAL_ORIGIN,
    NO_PROGRESS_CODE,
    GoalPhase,
    append_goal_change,
    effective_goal,
    goal_round_digests,
    goal_rounds_stalled,
    render_goal_round_prompt,
)
from agent_harness.session.service import SessionService
from agent_harness.tooling.contract import PermissionPolicy
from agent_harness.tooling.result import ToolResult
from agent_harness.web.app import AppState, session_service

_GOOD = """## Objective
把登录页修好

## Success criteria
登录成功跳转

## Verification
pytest tests/auth
"""


def _start(tmp_path: Path, *, permission_mode: str = "danger-full-access") -> tuple[AppState, Session]:
    settings = Settings(
        _env_file=None,
        workspace_dir=str(tmp_path),
        model_api_key="sk-test",
        model_name="test-model",
        model_provider="deepseek",
        enable_cors=False,
    )
    state = AppState(settings)
    session = Session.start(
        state.store,
        cwd=str(tmp_path),
        started_data={"permission_mode": permission_mode},
    )
    return state, session


@pytest.fixture(autouse=True)
def _isolate_goal_activation():
    """activation / 一轮额度是进程级 ClassVar；测试之间必须互不串味。"""
    SessionService._goal_armed.clear()
    SessionService._goal_resume_round_allowance.clear()
    yield
    SessionService._goal_armed.clear()
    SessionService._goal_resume_round_allowance.clear()


# ── 纯函数 ───────────────────────────────────────────────────────────


def test_round_prompt_copies_dsh_and_omp_guardrails() -> None:
    state = SimpleNamespace(objective='修好登录页\n"第二次"', max_goal_rounds=32)
    prompt = render_goal_round_prompt(state, 3)

    assert prompt.startswith("<goal_round>\n")
    assert prompt.endswith("</goal_round>")
    assert "Objective: " in prompt and "Round: 3/32" in prompt
    assert "Treat the current workspace, tool results, and durable session state as authoritative" in prompt
    assert "NEVER redefine success as a smaller, easier, or already-completed subset." in prompt
    assert "Budget exhaustion ≠ completion." in prompt


def test_round_started_increments_only_on_active_goal(tmp_path: Path) -> None:
    _, session = _start(tmp_path)
    assert append_goal_change(session, "create", objective=_GOOD, max_goal_rounds=2).ok

    started = append_goal_change(session, "round_started")
    assert started.ok and started.state.rounds_started == 1
    assert started.state.revision == 2
    assert session.events[-1].data["operation"] == "round_started"

    append_goal_change(session, "pause")
    rejected = append_goal_change(session, "round_started")
    assert not rejected.ok and rejected.error_kind == "conflict"
    assert effective_goal(session.events).rounds_started == 1

    append_goal_change(session, "resume")
    assert append_goal_change(session, "round_started").ok
    exhausted = append_goal_change(session, "round_started")
    assert not exhausted.ok and exhausted.error_kind == "conflict"
    assert effective_goal(session.events).rounds_started == 2


def test_pause_reason_is_durable_and_cleared_on_resume(tmp_path: Path) -> None:
    _, session = _start(tmp_path)
    append_goal_change(session, "create", objective=_GOOD)
    reason = {"code": NO_PROGRESS_CODE, "message": "两轮没有工具活动"}
    paused = append_goal_change(session, "pause", pause_reason=reason)
    assert paused.ok and paused.state.phase is GoalPhase.PAUSED
    assert paused.state.pause_reason == reason
    assert paused.state.to_payload()["pause_reason"] == reason

    resumed = append_goal_change(session, "resume")
    assert resumed.ok and resumed.state.pause_reason is None


def test_goal_round_digest_detects_empty_and_identical_activity(tmp_path: Path) -> None:
    _, session = _start(tmp_path)
    append_goal_change(session, "create", objective=_GOOD)

    # 第一轮：无工具活动。
    session.append(USER_MESSAGE, {"content": "round 1", "origin": GOAL_ORIGIN})
    assert goal_round_digests(session.events) == [""]

    # 第二轮：同样为空 ⇒ 无进展。
    session.append(USER_MESSAGE, {"content": "round 2", "origin": GOAL_ORIGIN})
    assert goal_rounds_stalled(session.events) is True

    # 第三轮：一模一样的一次工具调用 + 结果 ⇒ 仍然判无进展。
    session.append(USER_MESSAGE, {"content": "round 3", "origin": GOAL_ORIGIN})
    session.append(TOOL_CALL, {"tool_call_id": "c1", "tool_name": "glob", "args": {"pattern": "*"}})
    session.append(TOOL_RESULT, {"tool_call_id": "c1", "content": "same"})
    session.append(USER_MESSAGE, {"content": "round 4", "origin": GOAL_ORIGIN})
    session.append(TOOL_CALL, {"tool_call_id": "c2", "tool_name": "glob", "args": {"pattern": "*"}})
    session.append(TOOL_RESULT, {"tool_call_id": "c2", "content": "same"})
    assert goal_rounds_stalled(session.events) is True

    # 第四轮换了工具结果 ⇒ 有进展。
    session.append(USER_MESSAGE, {"content": "round 5", "origin": GOAL_ORIGIN})
    session.append(TOOL_CALL, {"tool_call_id": "c3", "tool_name": "glob", "args": {"pattern": "src/*"}})
    session.append(TOOL_RESULT, {"tool_call_id": "c3", "content": "different"})
    assert goal_rounds_stalled(session.events) is False


def test_goal_round_digest_ignores_volatile_tool_result_metadata(tmp_path: Path) -> None:
    """AC9：``tool/result`` 的 content 是整份 ``ToolResult`` JSON，含每次调用都变的
    耗时 / 尝试次数；两轮同样的调用必须仍判无进展（否则无进展检测永远不触发）。"""
    _, session = _start(tmp_path)
    append_goal_change(session, "create", objective=_GOOD)

    contents: list[str] = []

    def _activity(round_no: int, *, duration_ms: float, message: str) -> None:
        session.append(USER_MESSAGE, {"content": f"round {round_no}", "origin": GOAL_ORIGIN})
        session.append(
            TOOL_CALL,
            {"tool_call_id": f"c{round_no}", "tool_name": "glob", "args": {"pattern": "*"}},
        )
        result = ToolResult.success(
            message,
            {"matches": ["a.py"]},
            {"attempt": 1, "max_attempts": 3, "duration_ms": duration_ms,
             "total_duration_ms": duration_ms},
        )
        content = result.model_dump_json()
        contents.append(content)
        session.append(TOOL_RESULT, {"tool_call_id": f"c{round_no}", "content": content})

    _activity(1, duration_ms=12.5, message="找到 1 个文件")
    _activity(2, duration_ms=987.25, message="找到 1 个文件")
    # 前提校验：两轮 content 除了耗时以外逐字段相同（别让断言空转）。
    first, second = (json.loads(c) for c in contents[:2])
    assert first["metadata"]["duration_ms"] != second["metadata"]["duration_ms"]
    assert first["data"] == second["data"] and first["message"] == second["message"]

    # 只有耗时不同 ⇒ 仍是无进展；结果语义变了才算有进展。
    assert goal_rounds_stalled(session.events) is True
    _activity(3, duration_ms=3.0, message="找到 2 个文件")
    assert goal_rounds_stalled(session.events) is False


# ── 服务准入 ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_maybe_queue_requires_active_armed_and_danger_mode(tmp_path: Path) -> None:
    state, session = _start(tmp_path)
    service = session_service(state)
    append_goal_change(session, "create", objective=_GOOD)
    sid = session.session_id

    # disarmed（进程内 activation 为空）⇒ 不排队。
    await service._maybe_queue_goal_round(sid)
    assert not [e for e in state.store.read_events(sid) if e.type == MESSAGE_QUEUED]

    service._goal_armed.add(sid)
    await service._maybe_queue_goal_round(sid)
    queued = [e for e in state.store.read_events(sid) if e.type == MESSAGE_QUEUED]
    assert len(queued) == 1
    assert queued[0].data["origin"] == GOAL_ORIGIN
    assert queued[0].data["content"].startswith("<goal_round>")
    assert "Round: 1/32" in queued[0].data["content"]

    # 已有未投递 goal 轮次 ⇒ 不重复入队。
    await service._maybe_queue_goal_round(sid)
    assert len([e for e in state.store.read_events(sid) if e.type == MESSAGE_QUEUED]) == 1


@pytest.mark.asyncio
async def test_maybe_queue_respects_workspace_write_and_round_limit(tmp_path: Path) -> None:
    state, session = _start(tmp_path, permission_mode="workspace-write")
    service = session_service(state)
    append_goal_change(session, "create", objective=_GOOD)
    service._goal_armed.add(session.session_id)

    await service._maybe_queue_goal_round(session.session_id)
    assert not [e for e in state.store.read_events(session.session_id) if e.type == MESSAGE_QUEUED]

    # danger-full-access + 轮次用尽 ⇒ 也不排队。
    state2, session2 = _start(tmp_path / "second")
    service2 = session_service(state2)
    append_goal_change(session2, "create", objective=_GOOD, max_goal_rounds=1)
    append_goal_change(session2, "round_started")
    service2._goal_armed.add(session2.session_id)
    await service2._maybe_queue_goal_round(session2.session_id)
    assert not [e for e in state2.store.read_events(session2.session_id) if e.type == MESSAGE_QUEUED]


@pytest.mark.asyncio
async def test_maybe_queue_blocks_on_approval_and_resume_grants_one_round(tmp_path: Path) -> None:
    """决策 b：非 danger 档 ⇒ blocked + approval-required；显式 resume 给一轮。"""
    state, session = _start(tmp_path, permission_mode="workspace-write")
    service = session_service(state)
    append_goal_change(session, "create", objective=_GOOD)
    sid = session.session_id
    service._goal_armed.add(sid)

    # create 不授予额度 ⇒ 首个终态就 park，而不是排队。
    await service._maybe_queue_goal_round(sid)
    goal = effective_goal(state.store.read_events(sid))
    assert goal.phase is GoalPhase.BLOCKED
    assert goal.blocked_reason is not None
    assert goal.blocked_reason["code"] == APPROVAL_REQUIRED_CODE
    assert not [e for e in state.store.read_events(sid) if e.type == MESSAGE_QUEUED]
    assert service.goal_activation(sid) == "disarmed"

    # 显式 /goal resume ⇒ 武装 + 一轮额度 ⇒ 本轮正常入队。
    resumed = await service.set_goal(sid, "resume")
    assert resumed.ok
    assert sid in service._goal_resume_round_allowance
    await service._maybe_queue_goal_round(sid)
    queued = [e for e in state.store.read_events(sid) if e.type == MESSAGE_QUEUED]
    assert len(queued) == 1 and queued[0].data["origin"] == GOAL_ORIGIN
    assert sid not in service._goal_resume_round_allowance  # 额度已消费

    # 额度用完（该队列项已投递消费）、档位没变 ⇒ 重新 park。
    live = Session.load(state.store, sid)
    live.append(QUEUE_CONSUMED, {"queue_id": queued[0].data["queue_id"], "run_id": "run-1"})
    await service._maybe_queue_goal_round(sid)
    goal = effective_goal(state.store.read_events(sid))
    assert goal.phase is GoalPhase.BLOCKED
    assert goal.blocked_reason["code"] == APPROVAL_REQUIRED_CODE
    assert len([e for e in state.store.read_events(sid) if e.type == MESSAGE_QUEUED]) == 1


@pytest.mark.asyncio
async def test_resume_launch_disarms_only_on_resume_not_new_task(tmp_path, monkeypatch) -> None:
    """P0-3：普通新任务消息不能把 `/goal create` 的授权扔掉；真 resume 才 disarm。"""
    monkeypatch.setattr(
        "agent_harness.assembly.create_chat_model",
        lambda _config, **_kwargs: ScriptedModel([AIMessage(content="done")]),
    )
    state, session = _start(tmp_path)
    service = session_service(state)
    sid = session.session_id
    assert (await service.set_goal(sid, "create", objective=_GOOD)).ok
    service._goal_resume_round_allowance.add(sid)

    # 真正的 launch 不参与本断言（跑一轮真 run 只会拖住测试）：本用例只关心入口
    # 处的 disarm 判据，所以把提交点换成空桩。
    launches: list[dict[str, object]] = []

    def _fake_bind_and_launch(**kwargs: object) -> SimpleNamespace:
        launches.append(kwargs)
        return SimpleNamespace()

    monkeypatch.setattr(service, "_bind_and_launch", _fake_bind_and_launch)

    # 普通新任务（send_message / POST /resume 带 task / 内部 goal 轮次的形状）。
    await service.resume_and_launch(session_id=sid, task="普通新任务")
    assert launches and launches[-1]["task"] == "普通新任务"
    assert service.goal_activation(sid) == "armed"
    assert sid in service._goal_resume_round_allowance

    # 真 resume（带 resume_run_id）⇒ disarm + 一轮额度一并作废。disarm 发生在
    # 入口处，所以即便随后因"没有可恢复的暂停 run"被拒，也不该留着授权。
    with pytest.raises(BudgetConflict):
        await service.resume_and_launch(
            session_id=sid, task=None, resume_run_id="run-x",
            resume_basis="budget_increase", expected_version=1,
        )
    assert service.goal_activation(sid) == "disarmed"
    assert sid not in service._goal_resume_round_allowance


@pytest.mark.asyncio
async def test_no_progress_pauses_goal_and_disarms(tmp_path: Path) -> None:
    state, session = _start(tmp_path)
    service = session_service(state)
    append_goal_change(session, "create", objective=_GOOD)
    sid = session.session_id

    for _ in range(2):
        session.append(USER_MESSAGE, {"content": "goal round", "origin": GOAL_ORIGIN})
        assert append_goal_change(session, "round_started").ok
    service._goal_armed.add(sid)

    await service._maybe_queue_goal_round(sid)

    goal = effective_goal(state.store.read_events(sid))
    assert goal.phase is GoalPhase.PAUSED
    assert goal.pause_reason is not None
    assert goal.pause_reason["code"] == NO_PROGRESS_CODE
    assert service.goal_activation(sid) == "disarmed"
    assert not [e for e in state.store.read_events(sid) if e.type == MESSAGE_QUEUED]


@pytest.mark.asyncio
async def test_rebuild_restores_goal_origin(tmp_path: Path) -> None:
    state, session = _start(tmp_path)
    sid = session.session_id
    session.append(
        MESSAGE_QUEUED,
        {"queue_id": "q-goal", "content": "<goal_round>x</goal_round>", "origin": GOAL_ORIGIN},
    )

    await session_service(state).rebuild_message_queues()

    pending = await state.message_queues.list_pending(sid)
    assert [item.origin for item in pending] == [GOAL_ORIGIN]


# ── 端到端：AC7 / AC8 / AC9 ──────────────────────────────────────────


class _GateModel(ScriptedModel):
    """每个 run 一个实例；模型调用前等同一条共享 gate（构造在途 run）。"""

    def __init__(self, gate: asyncio.Event) -> None:
        super().__init__([AIMessage(content="done")])
        self._gate = gate

    async def astream(self, messages, **kwargs):
        await self._gate.wait()
        async for chunk in super().astream(messages, **kwargs):
            yield chunk


async def _wait_for(predicate, *, timeout: float = 20.0, what: str = "条件"):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        value = predicate()
        if value:
            return value
        if loop.time() >= deadline:
            raise AssertionError(f"超时 {timeout}s：{what} 未达成")
        await asyncio.sleep(0.02)


@pytest.mark.asyncio
async def test_auto_round_end_to_end_then_no_progress_pause(tmp_path, monkeypatch):
    """一次人工 run 后自动续跑两轮；两轮空 digest ⇒ paused + reason（AC7–AC9）。"""
    gate = asyncio.Event()

    def _factory(_config, **_kwargs):
        return _GateModel(gate)

    monkeypatch.setattr("agent_harness.assembly.create_chat_model", _factory)
    settings = Settings(
        _env_file=None,
        workspace_dir=str(tmp_path),
        model_api_key="sk-test",
        model_name="test-model",
        model_provider="deepseek",
        enable_cors=False,
    )
    state = AppState(settings)
    service = session_service(state)

    launched = await service.create_and_launch(
        task="do the work",
        permission_mode=PermissionPolicy.DANGER_FULL_ACCESS,
        permission_mode_explicit=True,
    )
    sid = launched.session.session_id
    created = await service.set_goal(sid, "create", objective=_GOOD)
    assert created.ok
    assert service.goal_activation(sid) == "armed"

    gate.set()
    await _wait_for(
        lambda: effective_goal(state.store.read_events(sid)).phase is GoalPhase.PAUSED,
        what="goal 因无进展暂停",
    )

    events = state.store.read_events(sid)
    goal = effective_goal(events)
    assert goal.rounds_started == 2
    assert goal.pause_reason is not None
    assert goal.pause_reason["code"] == NO_PROGRESS_CODE

    goal_user_messages = [
        e for e in events
        if e.type == USER_MESSAGE and e.data.get("origin") == GOAL_ORIGIN
    ]
    assert len(goal_user_messages) == 2
    assert all(str(e.data["content"]).startswith("<goal_round>") for e in goal_user_messages)

    goal_queue_events = [
        e for e in events
        if e.type == MESSAGE_QUEUED and e.data.get("origin") == GOAL_ORIGIN
    ]
    assert len(goal_queue_events) == 2
    assert len([e for e in events if e.type == QUEUE_CONSUMED]) == 2
    assert len([e for e in events if e.type == RUN_STARTED]) == 3  # 人工 1 + goal 2
    assert service.goal_activation(sid) == "disarmed"
