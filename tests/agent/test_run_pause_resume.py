"""`#312` T4：一个逻辑 run 的暂停与同 run 续跑（runtime 层，真实事件流 + 真实 Store）。

判据来源：`02 §5.2`（暂停 / 预留 closeout / 恢复沿用同一 run）、`03 §3.4`（两个事件的
字段与不变量）、`03 §5`（`paused` 可恢复、不是终态）、`11 §6.1`（投影可重建）、
ADR-0044 D2/D3。

本文件补的是**领域模块与 golden 之外的第三条腿**：
- 纯函数（`test_run_budget.py`）证明账本算术对；
- golden（`test_event_sequence_golden.py`）证明事件序列没变；
- 这里证明**跨执行**的语义对——暂停一次执行、用同一个 run_id 再跑一次，消耗累计、
  身份不变、重启后投影一致（"进程内记忆"在这条链上不参与任何判定）。

账目约定（多处断言的前提，`02 §5.2`）：closeout 是**预算内**的消耗。所以
`ceiling=N` 的 run 只接纳 N−1 个产出轮，第 N 轮归预留的收口调用。
"""

from __future__ import annotations

import json

import pytest
from langchain_core.messages import AIMessage

from agent_harness.agent import AgentRuntime
from agent_harness.agent.budget import SOURCE_DEPLOYMENT
from agent_harness.agent.run_budget import (
    CLOSEOUT_DETERMINISTIC,
    CLOSEOUT_MODEL,
    CONTINUATION_ACTION_KEY,
    REASON_BUDGET_EXHAUSTED,
    TRIGGER_LOCAL_TURNS,
    TRIGGER_RUN_TURNS,
    LaunchRunBudget,
    RunTurnLimits,
    derive_run_budget,
    latest_paused_run,
)
from agent_harness.agent.types import STATUS_COMPLETED, STATUS_PAUSED
from agent_harness.session import (
    MODEL_COMPLETED,
    RUN_COMPLETED,
    RUN_FAILED,
    RUN_PAUSED,
    RUN_RESUMED,
    RUN_STARTED,
    USER_MESSAGE,
    Session,
)
from agent_harness.session.store import JsonlSessionStore
from agent_harness.tooling import Tool, ToolExecutor, ToolRegistry, ToolResult
from pydantic import BaseModel
from tests.conftest import make_session
from tests.scripted_model import ScriptedModel

TOOL_ID = "call_t4"


class _EchoArgs(BaseModel):
    text: str = "x"


class _EchoTool(Tool):
    @property
    def name(self) -> str:
        return "echo"

    @property
    def description(self) -> str:
        return "原样回显。"

    @property
    def args_schema(self) -> type[BaseModel]:
        return _EchoArgs

    async def execute(self, args: _EchoArgs) -> ToolResult:
        return ToolResult.success(message=args.text)


def _runtime(
    model, *, ceiling: int | None, consumed: int = 0, run_id: str | None = None,
    version: int = 1, local_fuse: int = 500,
) -> AgentRuntime:
    """一个执行实例：`local_fuse` 是本实例的保险丝，`run_budget` 是启动时的 run 账本。"""
    registry = ToolRegistry()
    registry.register(_EchoTool())
    return AgentRuntime(
        model=model, registry=registry, executor=ToolExecutor(registry),
        max_agent_turns=local_fuse, local_fuse_source=SOURCE_DEPLOYMENT,
        run_budget=LaunchRunBudget(
            version=version, limits=RunTurnLimits(max_agent_turns_total=ceiling),
            consumed_turns=consumed, run_id=run_id,
        ),
    )


def _tool_round(idx: int) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[{"id": f"{TOOL_ID}_{idx}", "name": "echo", "args": {"text": f"t{idx}"}}],
    )


def _continuation_json(**overrides: object) -> AIMessage:
    payload: dict[str, object] = {
        "completed": ["已完成第一步"],
        "remaining": ["还剩第二步"],
        "blockers": [],
        "next_safe_action": "继续第二步",
    }
    payload.update(overrides)
    return AIMessage(content=json.dumps(payload, ensure_ascii=False))


class _ForbiddenModel:
    """被调用即失败的模型：用来证明"没有容量时一次调用都不发"。"""

    def __init__(self) -> None:
        self.calls = 0

    async def ainvoke(self, messages: list, **kwargs) -> AIMessage:
        self.calls += 1
        raise AssertionError("没有预留容量时不得调用模型")

    async def astream(self, messages: list, **kwargs):
        self.calls += 1
        raise AssertionError("没有预留容量时不得调用模型")
        yield AIMessage(content="")


def _run_id_of(session: Session) -> str:
    return next(e.run_id for e in session.events if e.type == RUN_STARTED and e.run_id)


# ── 暂停：预留容量内的有界 closeout ──────────────────────────────────────


@pytest.mark.asyncio
async def test_model_closeout_records_the_reserved_turn_inside_the_ceiling(tmp_path) -> None:
    """ceiling=3 且模型给了合法 continuation ⇒ 恰好用掉**预算内**的预留那一轮。

    账：2 个普通轮（被接纳） + 1 次 closeout = 3 = ceiling（`02 §5.2`：预留是容量
    不是豁免，它记进 consumed）。暂停不是终态：既没有 `run/completed`，也没有
    `run/failed`——"fuse 到顶"本身不是失败（PRD #305 EB-2）。
    """
    scripted = ScriptedModel([_tool_round(1), _tool_round(2), _continuation_json()])
    runtime = _runtime(scripted, ceiling=3)
    session = make_session(tmp_path)

    result = await runtime.run(session, "两轮之后该收口了")

    assert result.status == STATUS_PAUSED
    paused = next(e for e in session.events if e.type == RUN_PAUSED)
    assert paused.data["reason"] == REASON_BUDGET_EXHAUSTED
    assert paused.data["trigger_dimension"] == TRIGGER_RUN_TURNS
    assert paused.data["closeout_source"] == CLOSEOUT_MODEL
    assert paused.data["consumed"] == {"agent_turns": 3}
    assert paused.data["limits"]["run"]["max_agent_turns_total"] == 3
    assert paused.data["continuation"] == {
        "completed": ["已完成第一步"],
        "remaining": ["还剩第二步"],
        "blockers": [],
        CONTINUATION_ACTION_KEY: "继续第二步",
    }
    assert paused.data["resume_requirements"] == []
    assert [e.type for e in session.events if e.type in (RUN_COMPLETED, RUN_FAILED)] == []

    state = derive_run_budget(session.events, _run_id_of(session))
    assert state.version == 1
    assert state.consumed_turns == 3, "派生账本 = 事件说的事实"
    assert state.paused is not None
    assert state.resumable is True


@pytest.mark.asyncio
async def test_paused_projection_reads_the_consumed_snapshot(tmp_path) -> None:
    """`PausedRun.consumed_turns` 取的是**事件里的快照**，不是"重数一遍事件"。

    两者在"暂停之后又续跑"时会分道扬镳：重算值继续涨，快照停在暂停那一刻。拿重算值
    当"暂停时的消耗"会让 `resume_ceiling_ok` 与 `run/resumed.consumed` 一起漂移
    ——那正是"恢复不得重置/放大消耗"这条不变量会破的地方。
    """
    scripted = ScriptedModel([_tool_round(1), _continuation_json()])
    runtime = _runtime(scripted, ceiling=2)
    session = make_session(tmp_path)

    await runtime.run(session, "一轮就收口")

    run_id = _run_id_of(session)
    paused = latest_paused_run(session.events)
    assert paused is not None
    assert paused.run_id == run_id
    # ceiling=2 ⇒ 1 个普通轮 + 1 次 closeout = 2
    assert paused.consumed_turns == 2
    assert derive_run_budget(session.events, run_id).consumed_turns == paused.consumed_turns


@pytest.mark.asyncio
async def test_no_model_call_at_all_when_the_closeout_has_no_capacity(tmp_path) -> None:
    """`consumed >= ceiling` ⇒ 一次模型调用都不发，直接落确定性 continuation。

    这是"closeout 不得超出适用预算"的边界（`02 §5.2`）。生产上对应"活动 run 的
    ceiling 被下压到恰好等于已消耗"（PRD §3 允许下压到不低于已消耗，下压入口本身
    不在本票内），所以用启动上下文直接摆到这个边界上。`budget_version` 如实回传启动
    上下文的值（暂停臂不自己加一）。
    """
    model = _ForbiddenModel()
    runtime = _runtime(
        model, ceiling=1, consumed=1, run_id="run-lowered-ceiling", version=2,
    )
    session = make_session(tmp_path)

    result = await runtime.run(session, None)

    assert model.calls == 0
    assert result.status == STATUS_PAUSED
    paused = next(e for e in session.events if e.type == RUN_PAUSED)
    assert paused.data["closeout_source"] == CLOSEOUT_DETERMINISTIC
    assert paused.data["consumed"] == {"agent_turns": 1}, "快照沿用启动上下文，不重数"
    assert paused.data["budget_version"] == 2
    assert paused.data["trigger_dimension"] == TRIGGER_RUN_TURNS


# ── 恢复：同一个 run_id、消耗不重置 ──────────────────────────────────────


@pytest.mark.asyncio
async def test_same_run_resume_completes_without_resetting_accounting(tmp_path) -> None:
    """暂停 → 抬高绝对 ceiling → 同 run 续跑完成：run_id 不变、消耗继续累加。

    续跑执行的输入是 `LaunchRunBudget(run_id=..., version+1, consumed 快照)`——runtime
    因此**不**再 `begin_run`（事件流里只有一个 `run/started`），也不落 `user/message`
    （`user_input=None`：普通预算恢复不需要新任务文本）。
    """
    first = ScriptedModel([_tool_round(1), _tool_round(2), _continuation_json()])
    session = make_session(tmp_path)
    await _runtime(first, ceiling=3).run(session, "先跑两轮")

    paused = latest_paused_run(session.events)
    assert paused is not None
    pause_seq = len(session.events)
    assert paused.consumed_turns == 3

    second = ScriptedModel([AIMessage(content="第二步也做完了")])
    resumed_runtime = _runtime(
        second, ceiling=5, consumed=paused.consumed_turns, run_id=paused.run_id,
        version=paused.version + 1,
    )
    result = await resumed_runtime.run(session, None)

    assert result.status == STATUS_COMPLETED
    assert result.final_text == "第二步也做完了"

    new_events = list(session.events[pause_seq:])
    assert [e.type for e in new_events].count(RUN_RESUMED) == 0, (
        "run/resumed 由 SessionService 在 launch 之前落盘（这里直接驱动 runtime）"
    )
    assert new_events[0].type == MODEL_COMPLETED
    assert new_events[-1].type == RUN_COMPLETED
    # run 身份单一：恢复沿用同一逻辑 run——没有第二条 run/started、也没有新 user/message
    assert [e.type for e in session.events].count(RUN_STARTED) == 1
    assert [e.type for e in session.events].count(USER_MESSAGE) == 1
    assert {e.run_id for e in session.events if e.run_id} == {paused.run_id}

    state = derive_run_budget(session.events, paused.run_id)
    assert state.consumed_turns == paused.consumed_turns + 1, "消耗继续累加，绝不重置"
    # version 只由 `run/resumed` 推进（CAS 的比较对象），不是"执行了几次"——这条链上
    # 没有那个事件（服务层才落），所以仍是 1。反过来说：任何实现都不能靠 +1 猜版本。
    assert state.version == 1
    assert state.terminal is True
    assert state.resumable is False

    # 续跑那次模型看到的最后一条消息是**已有历史**（工具结果），不是伪造的新任务文本
    assert second.snapshots[0].messages[-1].type == "tool", (
        "普通预算恢复不伪一条新用户消息（票面明文）"
    )


@pytest.mark.asyncio
async def test_resume_without_new_task_text_keeps_the_original_instruction(tmp_path) -> None:
    """续跑不重落 `user/message`：原任务文本仍在上下文里（不是"没任务可做"）。"""
    first = ScriptedModel([_tool_round(1), _continuation_json()])
    session = make_session(tmp_path)
    original = "把 A 改成 B"
    await _runtime(first, ceiling=2).run(session, original)

    paused = latest_paused_run(session.events)
    assert paused is not None
    second = ScriptedModel([AIMessage(content="done")])
    await _runtime(
        second, ceiling=4, consumed=paused.consumed_turns, run_id=paused.run_id,
        version=paused.version + 1,
    ).run(session, None)

    texts = [m.content for m in second.snapshots[0].messages if m.type == "human"]
    assert original in texts


@pytest.mark.asyncio
async def test_local_fuse_counts_the_current_execution_not_the_run_ledger(tmp_path) -> None:
    """local fuse 是**实例级**的：续跑执行从 0 起步数它自己的步数。

    构造：没有 run ceiling（`max_agent_turns_total=None`），local fuse = 2，启动账本
    已有 3 轮 ⇒ 续跑执行跑满 1 轮普通轮就触发 local fuse（1+1>=2），而 run 账本
    （3）与本判定无关。命中维度如实回传 `local.max_agent_turns`——两个作用域互不替代
    （`02 §5.1`），投影上必须能分辨是谁到顶。closeout 仍可发生（run 侧无 ceiling ⇒
    不设限），并记进快照。
    """
    session = make_session(tmp_path)
    scripted = ScriptedModel([_tool_round(1), _continuation_json()])
    runtime = _runtime(
        scripted, ceiling=None, consumed=3, run_id="run-existing", version=2, local_fuse=2,
    )

    result = await runtime.run(session, None)

    assert result.status == STATUS_PAUSED
    paused = next(e for e in session.events if e.type == RUN_PAUSED)
    assert paused.data["trigger_dimension"] == TRIGGER_LOCAL_TURNS
    # run 账本：启动 3 + 本执行 1 个普通轮 + 1 次 closeout = 5
    assert paused.data["consumed"] == {"agent_turns": 5}
    assert paused.data["closeout_source"] == CLOSEOUT_MODEL
    assert paused.data["limits"]["run"]["max_agent_turns_total"] is None
    assert paused.data["limits"]["local"]["max_agent_turns"] == 2


# ── 进程重启 / 重放：投影可重建 ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_restart_rebuilds_the_same_paused_projection(tmp_path) -> None:
    """丢掉进程内对象、重新加载聚合 ⇒ 同一份暂停投影（版本 / 消耗 / continuation / 位置）。

    这条是 AC "Process restart/replay reconstructs the same paused run, counters,
    version and continuation without resetting them" 的确定性版本：账本没有第二份
    真相（`derive_run_budget` 纯函数），所以"重建"就是"重读"。
    """
    scripted = ScriptedModel([_tool_round(1), _tool_round(2), _continuation_json()])
    session = make_session(tmp_path)
    await _runtime(scripted, ceiling=3).run(session, "撞到 ceiling")
    before = latest_paused_run(session.events)
    assert before is not None

    # "进程重启"：同一份 JSONL，新的聚合对象
    store = JsonlSessionStore(root=tmp_path)
    reloaded = Session.load(store, session.session_id)
    after = latest_paused_run(store.read_events(session.session_id))

    assert after is not None
    assert after.as_projection() == before.as_projection()
    assert after.version == before.version
    assert after.consumed_turns == before.consumed_turns
    assert after.pause_seq == before.pause_seq
    assert after.continuation == before.continuation

    # 重新加载的聚合还能继续追加（seq 计数器不落后 ⇒ 日志不重复、可再次 resume）
    reloaded.append(USER_MESSAGE, {"content": "reload 之后的追加"}, run_id=before.run_id)
    again = Session.load(store, session.session_id)
    assert again.events[-1].data["content"] == "reload 之后的追加"
    assert [e.seq for e in again.events] == list(range(len(again.events)))


@pytest.mark.asyncio
async def test_paused_stream_ends_cleanly_on_the_pause_frame(tmp_path) -> None:
    """暂停把**当前执行流**干净收口：最后一帧是 `run/paused`，之后没有终态帧。"""
    scripted = ScriptedModel([_continuation_json()])
    runtime = _runtime(scripted, ceiling=1)
    session = make_session(tmp_path)

    frames = [frame async for frame in runtime.run_stream(session, "流式撞顶")]

    assert frames[-1].type == RUN_PAUSED
    assert frames[-1].seq is not None, "暂停是 durable 事实（要参与重连重放）"
    assert [f.type for f in frames if f.type in (RUN_COMPLETED, RUN_FAILED)] == []
