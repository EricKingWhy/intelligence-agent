"""#298 / MEM-V2-2 T7b：Runtime 终结臂 → 记忆形成的接缝（AC1 的生产入口）。

**为什么要单独一层**：`tests/agent/test_terminal_arms.py` 钉的是每条臂自身的收尾序列，
`test_event_sequence_golden.py` 钉的是全链等价性。本文件钉的是**这一笔新加的那一行**：
它在哪两条语句之间、拿什么当输入、坏了会怎样。三层的分工是刻意的——接缝的语义
（"先入队再镜像"）只在臂这一层能用共享时间线证明，全链基线与它无关。

**都是臂级直调，不起真模型**：`_terminal_completed` / `_terminal_failed_run` 在 loop
之外（模型调用早已结束或失败），所以这里 `model=object()`；真 `AgentRuntime` 的端到端
（含"慢记忆模型不挡可见答复"）在 `test_v2_wiring.py`。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest

from agent_harness.agent.runtime import (
    AgentRuntime,
    _RunFinalizer,
    _Telemetry,
    _TerminalArms,
)
from agent_harness.agent.types import (
    STATUS_COMPLETED,
    STATUS_IDENTICAL_TOOL_FAILURE_LOOP,
    STATUS_MAX_STEPS_EXCEEDED,
    AgentRunResult,
)
from agent_harness.memory.v2.roles import MemoryModelRoles
from agent_harness.memory.v2.runner import (
    MemoryFormationNotifier,
    MemoryJobRunner,
)
from agent_harness.session import (
    MODEL_COMPLETED,
    RUN_COMPLETED,
    RUN_FAILED,
    RUN_STARTED,
    USER_MESSAGE,
    Session,
)
from agent_harness.tooling import ToolExecutor, ToolRegistry
from tests.conftest import make_session

RUN_ID = "run-1"


# --------------------------------------------------------------------------------------
# 替身与装配
# --------------------------------------------------------------------------------------


class _NotifierSpy:
    """记录接缝递过来的东西；可选在回调里炸掉（失败隔离用例）。

    `watch` 给会话对象时，在**收到通知那一刻**快照盘上的事件类型——"终态已落盘才通知"
    这条因此是当场取证，而不是事后从最终事件列表里反推（后者对顺序不敏感）。
    """

    def __init__(
        self, *, order: list[str] | None = None, explode: bool = False,
        watch: Session | None = None,
    ) -> None:
        self.calls: list[dict[str, Any]] = []
        self.snapshot: list[str] | None = None
        self._order = order
        self._explode = explode
        self._watch = watch

    async def notify_run_finished(
        self, *, session_id: str, run_id: str, terminal_status: str,
        events: list[Any],
    ) -> object | None:
        if self._order is not None:
            self._order.append("notify")
        if self._watch is not None:
            self.snapshot = [event.type for event in self._watch.events]
        self.calls.append({
            "session_id": session_id, "run_id": run_id,
            "terminal_status": terminal_status, "events": list(events),
        })
        if self._explode:
            raise RuntimeError("memory formation host is broken")
        return None


@dataclass
class _Seeded:
    """一轮"看起来合格"的会话 + **本轮事件的起点**（= `memory_event_start`）。

    起点必须由产出的那一侧交回来：让用例自己数事件下标，等于把"本 run 从哪条开始"
    这条事实抄了第二遍（runtime 那边是 `session.mark()`，见 `_drive`）。
    """

    session: Session
    run_mark: int


@pytest.fixture
def seeded(tmp_path: Any) -> _Seeded:
    """run/started → 真实用户发言 → 模型成功回复（eligibility 眼里的最小合格形状）。

    **事件顺序与 `_drive` 逐条一致**，包括那条容易写错的事实：user 消息在 `begin_run`
    之前写，因此它的 `run_id` 是 `None`（`Session.append` 的 run_id 没有默认值）。
    fixture 若在这里补上 run_id，就等于把接缝描述成一个产出方到不了的世界。
    """
    session = make_session(tmp_path)
    run_mark = session.mark()
    session.append(USER_MESSAGE, {"content": "以后都用 pnpm 装依赖"})
    session.append(RUN_STARTED, {"turn_index": 1}, run_id=RUN_ID)
    session.append(MODEL_COMPLETED, {"content": "好的"}, run_id=RUN_ID)
    return _Seeded(session=session, run_mark=run_mark)


@dataclass
class _Kit:
    runtime: AgentRuntime
    session: Session
    arms: _TerminalArms
    result_holder: list[AgentRunResult] = field(default_factory=list)

    def types(self) -> list[str]:
        return [event.type for event in self.session.events]


def _kit(
    seeded: _Seeded, *, notifier: Any = None, run_id: str | None = RUN_ID,
) -> _Kit:
    """只提供 `_write_memories` / `_notify_memory_formation` / `_save_checkpoint` 三个钩子。

    `model=object()`：终结臂在 loop 之外，模型调用早已结束或失败。
    """
    registry = ToolRegistry()
    runtime = AgentRuntime(
        model=object(), registry=registry, executor=ToolExecutor(registry),
        memory_formation=notifier,
    )
    usage_total: dict[str, int] = {}
    result_holder: list[AgentRunResult] = []
    terminal = _RunFinalizer(seeded.session, usage_total)
    if run_id is not None:
        terminal.begin_run(run_id)
    arms = _TerminalArms(
        session=seeded.session, terminal=terminal, usage_total=usage_total,
        model_coord=SimpleNamespace(), result_holder=result_holder,
        cancel_reason_supplier=None,
        memory_event_start=seeded.run_mark,
        telemetry=_Telemetry(),
    )
    return _Kit(runtime, seeded.session, arms, result_holder)


async def _drain(agen: Any) -> list[Any]:
    return [event async for event in agen]


# --------------------------------------------------------------------------------------
# 端口本身
# --------------------------------------------------------------------------------------


def test_the_runner_satisfies_the_notifier_port() -> None:
    """`MemoryJobRunner` 必须在**结构上**满足 `MemoryFormationNotifier`。

    这条防的是"端口与宿主悄悄分了叉"：改掉 `notify_run_finished` 的关键字参数名时，
    只有 `runtime` 的调用点与这里会同时变红——注解不参与运行期检查，`isinstance` 参与。
    """
    runner = MemoryJobRunner(
        jobs=object(), sessions=object(), executor=object(),
        roles=MemoryModelRoles(primary=None, fallback=None),
    )
    assert isinstance(runner, MemoryFormationNotifier)


# --------------------------------------------------------------------------------------
# 完成臂
# --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_completed_arm_hands_the_run_over_exactly_once(seeded: _Seeded) -> None:
    """一次合格终结 ⇒ 恰好一次通知，且带的是**全**参数（session / run / 终态 / 本轮事件）。"""
    notifier = _NotifierSpy()
    kit = _kit(seeded, notifier=notifier)

    emitted = await _drain(
        kit.runtime._terminal_completed(kit.arms, steps=0, final="最终回答"),
    )

    assert [event.type for event in emitted] == [RUN_COMPLETED]
    assert len(notifier.calls) == 1
    call = notifier.calls[0]
    assert call["session_id"] == seeded.session.session_id
    assert call["run_id"] == RUN_ID
    assert call["terminal_status"] == STATUS_COMPLETED
    # 本轮事件切片：从 memory_event_start 起（含用户发言与模型回复），到终态为止
    types = [event.type for event in call["events"]]
    assert types[0] == USER_MESSAGE, "起点必须在本轮用户发言之前（否则资格判定看不到输入）"
    assert MODEL_COMPLETED in types
    assert types[-1] == RUN_COMPLETED
    # 那条容易被写错的生产事实：**本轮 user 事件没有 run_id**（`_drive` 在 begin_run
    # 之前写它）。把它钉在这里，是因为下游的 run 切片不能靠 run_id 找本轮用户发言——
    # 靠它会静默丢掉用户原话（T7b 实测，见 `run_slice_bounds` 的 docstring）。
    assert [event.run_id for event in call["events"]][:2] == [None, RUN_ID]


@pytest.mark.asyncio
async def test_notification_happens_before_the_visible_answer(seeded: _Seeded) -> None:
    """**先入队，再镜像**——ticket 的两条 Must Do 的交点（AC10 与"交出所有权前落盘"）。

    共享时间线才有区分力：只断言"调用发生了"看不出这一行被挪到 `yield` 之后，而那个
    位置有一个静默的丢失窗口（消费方收到终态就断连 ⇒ 生成器关闭 ⇒ 镜像之后的行不执行）。
    """
    order: list[str] = ["engine"]
    notifier = _NotifierSpy(order=order)
    kit = _kit(seeded, notifier=notifier)

    async for _event in kit.runtime._terminal_completed(kit.arms, steps=0, final="答案"):
        order.append("visible")

    assert order == ["engine", "notify", "visible"]


@pytest.mark.asyncio
async def test_the_terminal_event_is_already_durable_when_we_notify(
    seeded: _Seeded,
) -> None:
    """通知那一刻，`run/completed` 必须已经落盘（"交出所有权之前"的字面判据）。

    取证方式是**当场快照**：事后看最终事件列表对顺序不敏感（通知与镜像谁先谁后都一样）。
    """
    notifier = _NotifierSpy(watch=seeded.session)
    kit = _kit(seeded, notifier=notifier)

    await _drain(kit.runtime._terminal_completed(kit.arms, steps=0, final="答案"))

    assert notifier.snapshot is not None
    assert notifier.snapshot[-1] == RUN_COMPLETED
    assert kit.types().count(RUN_COMPLETED) == 1, "镜像之后不得再补第二条终态"


# --------------------------------------------------------------------------------------
# 失败终态臂
# --------------------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reason", [STATUS_MAX_STEPS_EXCEEDED, STATUS_IDENTICAL_TOOL_FAILURE_LOOP],
)
async def test_failed_run_arm_hands_over_its_own_reason(
    seeded: _Seeded, reason: str,
) -> None:
    """两张获批的受控失败各把自己的 `reason` 交出去——AC1 的"每张各建一个 job"。

    这里只证"送到的终态是对的"；"该不该建"由 `eligibility` 判（未获批的失败终态在
    同一入口被它挡掉），所以本行不是重复判定。
    """
    notifier = _NotifierSpy()
    kit = _kit(seeded, notifier=notifier)

    emitted = await _drain(
        kit.runtime._terminal_failed_run(
            kit.arms, steps=3, reason=reason, message="兜底",
        ),
    )

    assert [event.type for event in emitted] == [RUN_FAILED]
    assert [call["terminal_status"] for call in notifier.calls] == [reason]


@pytest.mark.asyncio
async def test_a_run_that_never_began_is_not_handed_over(seeded: _Seeded) -> None:
    """`run_id is None`（begin_run 之前就失败）⇒ 零通知：没有 run 就没有可切片的轮次。"""
    notifier = _NotifierSpy()
    kit = _kit(seeded, notifier=notifier, run_id=None)

    emitted = await _drain(
        kit.runtime._terminal_failed_run(kit.arms, steps=0, reason="failed"),
    )

    assert emitted == []
    assert notifier.calls == []


# --------------------------------------------------------------------------------------
# 失败隔离与向后兼容
# --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_broken_host_does_not_poison_the_run_result(seeded: _Seeded) -> None:
    """宿主炸了也只落日志：终态只有一条，run 结果照常产出。

    这是"记忆是旁路"的硬判据。放开这道隔离，异常会被顶层异常臂接住并补一条
    `run/failed`——一个 run 出现双终态，历史不可对账。
    """
    notifier = _NotifierSpy(explode=True)
    kit = _kit(seeded, notifier=notifier)

    emitted = await _drain(
        kit.runtime._terminal_completed(kit.arms, steps=0, final="答案"),
    )

    assert len(notifier.calls) == 1, "必须真的调用过（否则这条用例什么都没证）"
    assert [event.type for event in emitted] == [RUN_COMPLETED]
    assert RUN_FAILED not in kit.types()
    assert kit.types().count(RUN_COMPLETED) == 1
    assert len(kit.result_holder) == 1
    assert kit.result_holder[0].status == STATUS_COMPLETED


@pytest.mark.asyncio
async def test_without_a_host_the_arms_keep_their_old_shape(seeded: _Seeded) -> None:
    """未装配（默认 `None`）= 既有形状：零通知、零异常、事件序列逐字不变。"""
    kit = _kit(seeded, notifier=None)
    before = kit.types()

    await _drain(kit.runtime._terminal_completed(kit.arms, steps=0, final="答案"))

    assert kit.types() == [*before, RUN_COMPLETED]
    assert len(kit.result_holder) == 1
