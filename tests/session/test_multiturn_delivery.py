"""ADR-0030（#196）T1/T2/T3/T9/T10：在途输入通道的**消费侧**端到端契约。

这一组测试的存在理由（issue #196 的根因）：契约层（事件常量、HTTP 字段）与
内存层的测试当时都是绿的，而"在途 run 期间发的消息最终被模型看到"这条**端到端
行为**没有任何断言。所以这里全部按外部行为断言——事件流 + 模型实际收到什么 +
投影结果，不碰内部实现细节。

接缝：真实 `AppState`（含真实 `RunManager` 的终态回调接线）+ 真实
`SessionService` + 真实 `AgentRuntime`，只把 `create_chat_model` 换成可控的
`GateScriptedModel`（确定性剧本 + 记录每次请求的 messages 快照 + 可用 gate 把
run 钉在"在途"状态）。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from agent_harness.config import Settings
from agent_harness.model.scripted import RequestSnapshot, ScriptedModel
from agent_harness.session import (
    RUN_COMPLETED,
    RUN_STARTED,
    USER_MESSAGE,
    Session,
    derive_messages,
)
from agent_harness.session.event import (
    MESSAGE_QUEUED,
    QUEUE_CANCELLED,
    QUEUE_CONSUMED,
    STEER_APPLIED,
    STEER_REQUESTED,
)
from agent_harness.session.queue import QueuedMessage
from agent_harness.session.service import SessionService
from agent_harness.web.app import AppState


class GateScriptedModel(ScriptedModel):
    """ScriptedModel + 可选 gate：每次模型调用前 await gate。

    gate 未 set 时 run 停在模型调用上——这就是"在途 run"窗口的构造方式；测试
    放行后 run 继续跑完。gate 是**共享**的（同一测试的多个 run 共用），放行一次
    之后后续 run 不再阻塞。

    快照写进调用方给的共享列表（跨 run 实例可见），而不是只看某一个模型实例。
    """

    def __init__(
        self, responses: list[AIMessage], gate: asyncio.Event | None,
        snapshots: list[RequestSnapshot],
    ) -> None:
        super().__init__(responses)
        self._gate = gate
        self.snapshots = snapshots

    async def astream(self, messages, **kwargs):
        if self._gate is not None:
            await self._gate.wait()
        async for chunk in super().astream(messages, **kwargs):
            yield chunk


class _Harness:
    """隔离环境：AppState + Service + 共享模型快照 + 事件流读取助手。"""

    def __init__(self, state: AppState, snapshots: list[RequestSnapshot]) -> None:
        self.state = state
        self.service = SessionService(state)
        self.snapshots = snapshots

    def events(self, session_id: str):
        return self.state.store.read_events(session_id)

    def types(self, session_id: str) -> list[str]:
        return [e.type for e in self.events(session_id)]

    def of_type(self, session_id: str, event_type: str):
        return [e for e in self.events(session_id) if e.type == event_type]

    def human_texts(self, session_id: str) -> list[str]:
        return [
            str(m.content) for m in derive_messages(self.events(session_id))
            if isinstance(m, HumanMessage)
        ]

    async def wait_for(self, predicate, *, timeout: float = 15.0, what: str = "条件"):
        """轮询直到 predicate 为真（异步断言重试，避免 sleep 猜时序）。"""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            value = predicate()
            if value:
                return value
            if loop.time() >= deadline:
                raise AssertionError(f"超时 {timeout}s：{what} 未达成")
            await asyncio.sleep(0.02)


def _build_harness(
    tmp_path: Path,
    monkeypatch,
    responses: list[AIMessage],
    *,
    gate: asyncio.Event | None = None,
    model_cls: type[GateScriptedModel] | None = None,
    model_kwargs: dict | None = None,
) -> _Harness:
    """构造隔离 harness：每个 run 的 build_runtime 各拿一个新模型实例。

    ``model_cls`` 允许注入自定义替身（如按 run 生成 tool_call_id 的
    TwoTurnScriptedModel）；``model_kwargs`` 透传给替身构造（如共享
    call_counter）。默认按 ``responses`` 剧本构造 GateScriptedModel。
    """
    snapshots: list[RequestSnapshot] = []
    model_kwargs = model_kwargs or {}

    def _factory(config, **kwargs):
        if model_cls is not None:
            return model_cls(responses, gate, snapshots, **model_kwargs)
        return GateScriptedModel(responses, gate, snapshots, **model_kwargs)

    monkeypatch.setattr("agent_harness.assembly.create_chat_model", _factory)
    settings = Settings(
        _env_file=None,
        workspace_dir=str(tmp_path),
        model_api_key="sk-test",
        model_name="test-model",
        model_provider="deepseek",
        enable_cors=False,
    )
    return _Harness(AppState(settings), snapshots)


#: 运行时上下文快照（ADR-0023 D8）以 meta_user 身份混在 HumanMessage 里——它不是
#: 用户输入，断言"模型收到哪些用户消息"时按它的固定开头剔除。
_RUNTIME_SNAPSHOT_PREFIX = "以下是本次运行的运行时事实"


def _human_texts(snapshot: RequestSnapshot) -> list[str]:
    """一次模型调用收到的**用户**消息（剔掉 runtime 快照）。"""
    return [
        str(m.content) for m in snapshot.messages
        if isinstance(m, HumanMessage)
        and not str(m.content).startswith(_RUNTIME_SNAPSHOT_PREFIX)
    ]


def _last_human_texts(harness: _Harness) -> list[str]:
    """最后一次模型调用实际收到的用户消息（模型视角的事实，不是投影推演）。"""
    return _human_texts(harness.snapshots[-1])


_TOOL_TURN = AIMessage(
    content="",
    tool_calls=[{"id": "tc-1", "name": "glob", "args": {"pattern": "*"}}],
)


class TwoTurnScriptedModel(GateScriptedModel):
    """带 tool 轮 + 收尾轮的剧本。

    ⚠ tool_call_id 在**同一个 session** 里是 Ledger 主键（UNIQUE），所以带 tool
    的剧本不能给第二个 run 复用——第二次 run 会再次产出 `tc-1`，撞
    `operations.tool_call_id` 唯一约束让 run 2 直接 run/failed。这个替身在每次
    模型调用时生成**新** tool_call_id（按调用计数），多 run 复用安全。

    ⚠ 剧本每次都吐 tool_call 会让模型永不收敛（第 11 次调用撞 max_steps 保险丝
    ⇒ run/failed）。``turns_with_tools`` 控制带 tool 的调用次数，之后吐纯文本
    收尾——"第二个 run 的第一次调用就收尾"用默认 1。
    """

    def __init__(
        self, responses: list[AIMessage], gate: asyncio.Event | None,
        snapshots: list[RequestSnapshot], *, turns_with_tools: int = 1,
        call_counter: list[int] | None = None,
    ) -> None:
        super().__init__([], gate, snapshots)
        # ⚠ 计数器**跨实例共享**（默认 None → 每个替身一个新 list）：build_runtime
        # 每个 run 都新建模型实例，实例各自从 0 起算会让第二个 run 的第一次调用
        # 又吐 tool 轮 ⇒ 永不收敛（run/failed max_steps_exceeded）。需要多 run
        # 收敛的测试传同一个 list 进来。
        self._shared_counter = call_counter if call_counter is not None else [0]
        self._turns_with_tools = turns_with_tools

    async def astream(self, messages, **kwargs):
        """每次模型调用重新排剧本：新 tool_call_id 的 tool 轮 + 收尾轮。

        ⚠ Runtime 经 `ModelFallbackCoordinator._guarded_stream` 消费
        ``model.astream(messages)``——它对返回值直接 ``async for``（要求
        __aiter__），所以本方法必须是**异步生成器**：直接把副产物 yield 出去，
        不能 ``await super().astream(...)`` 或 ``return`` 它（那会变成协程，
        run 直接 failed）。
        """
        self._shared_counter[0] += 1
        call_index = self._shared_counter[0]
        if call_index <= self._turns_with_tools:
            self._responses = [
                AIMessage(
                    content="",
                    tool_calls=[{
                        "id": f"tc-{call_index}", "name": "glob",
                        "args": {"pattern": "*"},
                    }],
                ),
                AIMessage(content="done"),
            ]
        else:
            self._responses = [AIMessage(content="done")]
        self._cursor = 0
        async for chunk in super().astream(messages, **kwargs):
            yield chunk


# ── T1：queue 自动接力 ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_queue_relays_to_new_run_without_client_action(tmp_path, monkeypatch):
    """run 1 在途时入队的消息，在 run 1 终态后**自动**变成 run 2 并进入模型输入。

    这是 issue #196 的核心复现路径：修复前 `message/queued` 落盘后永远无人消费。
    """
    gate = asyncio.Event()
    harness = _build_harness(
        tmp_path, monkeypatch, [AIMessage(content="答")], gate=gate,
    )
    launched = await harness.service.create_and_launch(task="A")
    session_id = launched.session.session_id
    await harness.wait_for(
        lambda: len(harness.of_type(session_id, RUN_STARTED)) == 1,
        what="run 1 起跑",
    )

    queued = await harness.service.send_message(
        session_id=session_id, content="B", mode="queue",
    )
    assert queued.status == "queued"
    queued_events = harness.of_type(session_id, MESSAGE_QUEUED)
    assert [e.data["queue_id"] for e in queued_events] == [
        queued.queued_message.queue_id
    ]
    assert queued_events[0].data["content"] == "B"

    # 放行 run 1 → 终态 → 终态驱动接力（全程无任何客户端动作）
    gate.set()
    await harness.wait_for(
        lambda: harness.of_type(session_id, QUEUE_CONSUMED),
        what="queue/consumed 落盘",
    )

    consumed = harness.of_type(session_id, QUEUE_CONSUMED)[0]
    assert consumed.data["queue_id"] == queued.queued_message.queue_id
    run_started = harness.of_type(session_id, RUN_STARTED)
    assert len(run_started) == 2, "应当由接力开出第二个 run"
    assert consumed.data["run_id"] == run_started[1].run_id
    assert run_started[1].run_id != run_started[0].run_id

    await harness.wait_for(
        lambda: len(harness.of_type(session_id, RUN_COMPLETED)) == 2,
        what="run 2 完成",
    )
    # 端到端可见：B 确实被第二个 run 的模型看到（A 作为历史仍在链上），也进入投影
    assert _last_human_texts(harness) == ["A", "B"]
    assert harness.human_texts(session_id) == ["A", "B"]


# ── T2 / T3：steer 注入同一 run，A+B 都在链上 ──────────────────────────


@pytest.mark.asyncio
async def test_steer_injected_at_loop_head_in_same_run(tmp_path, monkeypatch):
    """steer 在**同一 run** 的下一个循环头注入，且 A+B 同时进入模型输入。

    剧本第一个响应带 tool_call ⇒ 制造第二个循环迭代——循环头就是 steer 的投递
    边界（"下一个模型调用前"）。
    """
    gate = asyncio.Event()
    harness = _build_harness(
        tmp_path, monkeypatch, [_TOOL_TURN, AIMessage(content="done")], gate=gate,
    )
    launched = await harness.service.create_and_launch(task="A")
    session_id = launched.session.session_id
    run_id_1 = await launched.run.wait_run_id()
    assert run_id_1 is not None

    steered = await harness.service.send_message(
        session_id=session_id, content="B", mode="steer",
    )
    assert steered.status == "steered"
    steer_event = harness.of_type(session_id, STEER_REQUESTED)[0]
    assert steer_event.data["run_id"] == run_id_1

    gate.set()
    await harness.wait_for(
        lambda: harness.of_type(session_id, STEER_APPLIED),
        what="steer/applied 落盘",
    )

    applied = harness.of_type(session_id, STEER_APPLIED)[0]
    assert applied.data["run_id"] == run_id_1, "steer 必须在同一 run 内注入"
    assert len(harness.of_type(session_id, RUN_STARTED)) == 1, "steer 不得新开 run"
    steer_user = [
        e for e in harness.of_type(session_id, USER_MESSAGE)
        if e.data.get("steer_id") == steer_event.data["steer_id"]
    ]
    assert len(steer_user) == 1
    assert applied.data["applied_seq"] == steer_user[0].seq
    assert steer_user[0].run_id == run_id_1
    # D6：steer 是**用户自己的话**，绝不带 injected_by——否则记忆抽取会静默丢弃它
    assert "injected_by" not in steer_user[0].data

    await harness.wait_for(
        lambda: len(harness.of_type(session_id, RUN_COMPLETED)) == 1,
        what="run 完成",
    )
    # T3：A 未被剔除，B 追加在其后（query = A + B，不是取代）
    second_call_humans = _last_human_texts(harness)
    assert second_call_humans[0] == "A", "A 必须仍在链上（steer ≠ supersede）"
    assert second_call_humans[-1] == "B"
    assert harness.human_texts(session_id) == ["A", "B"]


@pytest.mark.asyncio
async def test_steer_with_mismatched_run_id_is_discarded_then_relayed(
    tmp_path, monkeypatch,
):
    """run_id 不匹配的 steer 不注入（ADR §4.3），但**不丢**——终态驱动当普通输入投递。

    ADR §4.3 要求 runtime 对"目标不是当前 run"的请求一律丢弃并记日志：给错误的
    run 注入等于让用户的话出现在无关上下文里。安全的前提是丢弃**不是终点**——
    该 `steer/requested` 仍未被 `steer/applied` 收口，所以它会在终态变成一个新 run。
    """
    gate = asyncio.Event()
    harness = _build_harness(
        tmp_path, monkeypatch, [], gate=gate, model_cls=TwoTurnScriptedModel,
        model_kwargs={"call_counter": [0]},  # 计数器跨 run 共享：run 2 首次调用就收尾
    )
    launched = await harness.service.create_and_launch(task="A")
    session_id = launched.session.session_id
    run_id_1 = await launched.run.wait_run_id()

    # 指向一个不存在的 run：内容仍登记（绕过 service 的在途 run 守卫，模拟陈旧请求）
    await harness.state.message_queues.register_steer(
        session_id=session_id, content="给别的 run 的引导",
        run_id="run-does-not-exist", created_at="t",
    )
    harness.service._append_session_event(
        session_id, STEER_REQUESTED, steer_id="steer-stale",
        content="给别的 run 的引导", run_id="run-does-not-exist",
    )

    gate.set()
    await harness.wait_for(
        lambda: harness.of_type(session_id, STEER_APPLIED),
        what="陈旧 steer 被降级投递",
    )

    applied = harness.of_type(session_id, STEER_APPLIED)[0]
    assert applied.data["steer_id"] == "steer-stale"
    # 降级路径：applied_seq 为 null（它自己就是新 run 的首条 user 消息）
    assert applied.data["applied_seq"] is None
    run_started = harness.of_type(session_id, RUN_STARTED)
    assert len(run_started) == 2, "降级投递必然开出新 run"
    assert applied.data["run_id"] == run_started[1].run_id
    assert applied.data["run_id"] != run_id_1
    # 不得出现"注入进 run 1"的 user/message（run_id 不匹配就该被丢弃）
    injected = [
        e for e in harness.of_type(session_id, USER_MESSAGE)
        if e.data.get("steer_id") == "steer-stale"
    ]
    assert injected == []
    await harness.wait_for(
        lambda: len(harness.of_type(session_id, RUN_COMPLETED)) == 2,
        what="run 2 完成",
    )
    assert "给别的 run 的引导" in _last_human_texts(harness)


# ── T9：重启重建（不自动起 run）──────────────────────────────────────


@pytest.mark.asyncio
async def test_rebuild_restores_pending_inputs_without_launching(tmp_path, monkeypatch):
    """未消费的 message/queued 重启后仍列在待发送项里，且**不**自动开 run。"""
    harness = _build_harness(tmp_path, monkeypatch, [AIMessage(content="x")])
    session_id = "sess-restart"
    session = Session.start(harness.state.store, session_id=session_id)
    session.append(RUN_STARTED, {})
    session.append(RUN_COMPLETED, {"status": "completed"})
    session.append(MESSAGE_QUEUED, {"queue_id": "q-restart", "content": "别忘了我"})

    # 模拟重启：内存镜像是空的
    await harness.state.message_queues.cleanup(session_id)
    assert await harness.state.message_queues.list_pending(session_id) == []

    rebuilt = await harness.service.rebuild_message_queues()
    assert rebuilt >= 1
    pending = await harness.service.list_undelivered_inputs(session_id)
    assert [(p.kind, p.input_id, p.content) for p in pending] == [
        ("queue", "q-restart", "别忘了我"),
    ]
    assert harness.state.message_queues.list_pending(session_id) != [], "镜像已重建"
    # 只重建，不自动起 run：刚启动无订阅者（起了会被 orphan 回收），用户也不在场
    assert harness.state.run_manager.get_active(session_id) is None
    assert harness.of_type(session_id, RUN_STARTED)[0].run_id is None
    assert len(harness.of_type(session_id, RUN_STARTED)) == 1, "不得自动开新 run"


@pytest.mark.asyncio
async def test_rebuild_is_idempotent(tmp_path, monkeypatch):
    """重建是替换而非追加：重复执行得到同一集合。"""
    harness = _build_harness(tmp_path, monkeypatch, [AIMessage(content="x")])
    session_id = "sess-idem"
    session = Session.start(harness.state.store, session_id=session_id)
    session.append(MESSAGE_QUEUED, {"queue_id": "q1", "content": "一"})
    session.append(MESSAGE_QUEUED, {"queue_id": "q2", "content": "二"})

    for _ in range(2):
        await harness.service.rebuild_message_queues()
    pending = await harness.service.list_undelivered_inputs(session_id)
    assert [p.input_id for p in pending] == ["q1", "q2"]
    assert await harness.state.message_queues.pending_count(session_id) == 2


# ── deliver_next_undelivered：一次一条 + 跳过已取消 ───────────────────


@pytest.mark.asyncio
async def test_deliver_skips_cancelled_and_delivers_one(tmp_path, monkeypatch):
    """已取消的排队项不投递；一次只投递一条（按到达顺序）。

    ⚠ `deliver_next_undelivered` 走 `resume_and_launch`，而 resume 需要
    WorkspaceRegistry 里的 sandbox 映射（"这个会话曾经跑过"）——所以本测试用
    真实 `create_and_launch` 建立的会话（映射在那时写入），而不是手工 `Session.start`。
    """
    gate = asyncio.Event()  # 不预设：先钉住预热 run（模拟在途），后面再放行
    harness = _build_harness(
        tmp_path, monkeypatch, [AIMessage(content="答")], gate=gate,
    )
    launched = await harness.service.create_and_launch(task="预热")
    session_id = launched.session.session_id
    gate.set()
    # 等预热 run **完全收口**（task done + get_active 为 None）——只看 run/completed
    # 落盘不够：run 的收尾 await 还在跑时 get_active 仍返回在途，deliver 会被
    # ActiveRunConflict 挡住（终态驱动同款守卫，竞态护栏的存在理由）。
    await harness.wait_for(
        lambda: harness.state.run_manager.get_active(session_id) is None
        and harness.of_type(session_id, RUN_COMPLETED),
        what="预热 run 完全收口",
    )
    # 再等一拍：_drive 的 finally 里 on_run_terminal 排在 finish() 之后 await，
    # 上面的 wait_for（只看 get_active）通过时它可能还没跑——此刻 append 的
    # queued 会被它当成"终态后的待投递输入"接力掉，与测试自己的 deliver 撞车。
    await asyncio.sleep(0.3)
    harness.service._append_session_event(
        session_id, MESSAGE_QUEUED, queue_id="q1", content="第一条",
    )
    harness.service._append_session_event(
        session_id, MESSAGE_QUEUED, queue_id="q2", content="第二条",
    )
    await harness.state.message_queues.restore(
        session_id=session_id,
        queue_items=[
            QueuedMessage(queue_id="q1", content="第一条", session_id=session_id,
                          created_at="t"),
            QueuedMessage(queue_id="q2", content="第二条", session_id=session_id,
                          created_at="t"),
        ],
        steers=[],
    )
    await harness.service.cancel_queue(session_id=session_id, queue_id="q1")
    assert harness.of_type(session_id, QUEUE_CANCELLED)[0].data["queue_id"] == "q1"

    gate.set()  # 投递出来的 run 立刻跑完
    delivered = await harness.service.deliver_next_undelivered(session_id=session_id)
    assert delivered is not None
    assert delivered.session.session_id == session_id
    assert harness.of_type(session_id, QUEUE_CONSUMED)[0].data["queue_id"] == "q2"
    await harness.wait_for(
        lambda: harness.snapshots and "第二条" in _last_human_texts(harness),
        what="第二条被模型看到",
    )
    assert await harness.service.list_undelivered_inputs(session_id) == []


@pytest.mark.asyncio
async def test_on_run_terminal_is_noop_without_pending_input(tmp_path, monkeypatch):
    """无待投递输入时终态驱动是 no-op（幂等，不报错、不开 run）。"""
    gate = asyncio.Event()
    gate.set()
    harness = _build_harness(
        tmp_path, monkeypatch, [AIMessage(content="答")], gate=gate,
    )
    launched = await harness.service.create_and_launch(task="A")
    session_id = launched.session.session_id
    await harness.wait_for(
        lambda: len(harness.of_type(session_id, RUN_COMPLETED)) == 1,
        what="run 完成",
    )
    before = len(harness.of_type(session_id, RUN_STARTED))
    await harness.service.on_run_terminal(session_id)
    assert len(harness.of_type(session_id, RUN_STARTED)) == before


# ── T10：steer 参与记忆抽取（injected_by 为空）────────────────────────


@pytest.mark.asyncio
async def test_steer_message_is_not_marked_as_runtime_injected(tmp_path, monkeypatch):
    """steer 的 user/message 不带 injected_by ⇒ 记忆抽取**不会**丢弃用户的话。

    `memory/extractor.py` 按 `injected_by` 排除 runtime 文案；若 steer 复用了那个
    标记，用户自己补充的引导就会被长期记忆静默丢弃（ADR-0030 D6 的具体故障模式）。
    """
    from agent_harness.memory.extractor import _is_runtime_injected

    gate = asyncio.Event()
    harness = _build_harness(
        tmp_path, monkeypatch, [_TOOL_TURN, AIMessage(content="done")], gate=gate,
    )
    launched = await harness.service.create_and_launch(task="A")
    session_id = launched.session.session_id
    await harness.service.send_message(session_id=session_id, content="B", mode="steer")
    gate.set()
    await harness.wait_for(
        lambda: harness.of_type(session_id, STEER_APPLIED), what="steer 注入",
    )

    steer_event = harness.of_type(session_id, STEER_REQUESTED)[0]
    steer_user = next(
        e for e in harness.of_type(session_id, USER_MESSAGE)
        if e.data.get("steer_id") == steer_event.data["steer_id"]
    )
    assert _is_runtime_injected(steer_user) is False
    # 用户首次发言（A）与引导（B）都参与抽取 ⇒ 两者口径一致
    first_user = harness.of_type(session_id, USER_MESSAGE)[0]
    assert _is_runtime_injected(first_user) is False
