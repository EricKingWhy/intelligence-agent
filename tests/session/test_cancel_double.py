"""#550（RL-01）：连续两次 /cancel —— 取消收尾期间被再次取消，run 终态不得丢失。

复现（audit 指定的 barrier 法，两个确定性钉点）：

1. 模型 astream 进入即置 ``model_entered``——测试等到它才取消，保证取消落在
   预算准入（``session_step_reserved=True``）**之后**、模型调用在途窗口内
   （探针证实：取消若落在准入之前，取消臂不经过 refund，复现不成立）。
2. 把取消臂唯一的 await（shield 的 ``refund_turn``）换成 gate 版本——"取消臂
   正在收尾"的窗口被钉成确定性事件，再注入第二次取消。

第二次 ``task.cancel()`` 会在该 await 上重新抛 CancelledError——shield 只保护
内层退回不被打断，不消除 caller 收到的取消；``except Exception`` 接不住
BaseException ⇒ ``_terminal_cancelled`` 被跳过，durable 日志永停悬空
run/started（票面现象：run 永久丢失终态事件）。

两个检查点各有判别力（协议 §8.3.4，失败集不同）：

- test A（公开 cancel 路径）：判别 ``RunManager.cancel`` 的幂等——重复 cancel
  返回 True 但**不得**再 ``task.cancel()``（``cancelling()==1`` 断言）；
  幂等缺失时该断言红（终态由运行时侧防线兜住）。
- test B（绕过幂等层的原始 ``task.cancel``）：判别取消臂自身的健壮性——即便
  再注入发生（reap / 关停 / 未来代码等旁路），收尾也要吞掉它、照常写唯一终态；
  缺失时终态丢失（test A 不红）。
"""

from __future__ import annotations

import asyncio

import pytest
from langchain_core.messages import AIMessage

from agent_harness.session.event import RUN_FAILED, RUN_STARTED
from agent_harness.storage.delegation_tree import SessionBudgetHandle
from tests.session.test_multiturn_delivery import (
    GateScriptedModel,
    _build_harness,
)


class _SignalGateModel(GateScriptedModel):
    """astream 进入即置位信号：确知 run 已过预算准入、即将钉在模型 gate 上。

    没有这个信号，取消可能落在准入之前的 await 上——那条路径本来就不经过
    refund，红测会假红（探针实证），所以"在途窗口"必须显式钉住。
    """

    def __init__(
        self, responses: list[AIMessage], gate: asyncio.Event | None,
        snapshots: list, model_entered: asyncio.Event,
    ) -> None:
        super().__init__(responses, gate, snapshots)
        self._model_entered = model_entered

    async def astream(self, messages, **kwargs):
        self._model_entered.set()
        async for chunk in super().astream(messages, **kwargs):
            yield chunk


def _arm_refund_on_gate(
    monkeypatch, refund_gate: asyncio.Event, entered: asyncio.Event,
) -> None:
    """把 ``SessionBudgetHandle.refund_turn`` 换成 gate 版：取消臂走到这里就停住。

    "取消收尾进行中"窗口的确定性构造（audit 的 barrier 法）——不猜时序。放行后
    原版退回照常执行，账目语义（#318）不受本替身影响。
    """
    original = SessionBudgetHandle.refund_turn

    async def gated_refund(self):
        entered.set()
        await refund_gate.wait()
        return await original(self)

    monkeypatch.setattr(SessionBudgetHandle, "refund_turn", gated_refund)


async def _launch_run_parked_in_model(harness, model_entered: asyncio.Event) -> str:
    """开 run 并等到它钉进模型调用窗口（已过预算准入），返回 session_id。"""
    launched = await harness.service.create_and_launch(task="A")
    session_id = launched.session.session_id
    await harness.wait_for(
        lambda: model_entered.is_set()
        and len(harness.of_type(session_id, RUN_STARTED)) == 1,
        what="run 已过预算准入并钉在模型调用上",
    )
    return session_id


@pytest.mark.asyncio
async def test_repeat_cancel_is_idempotent_and_keeps_one_terminal(tmp_path, monkeypatch):
    """连续两次 cancel：第二次不得再注入取消，终态恰一条 reason=cancelled。"""
    model_gate = asyncio.Event()  # 钉住 run 在模型调用上（在途窗口，全程不放行）
    refund_gate = asyncio.Event()  # 钉住取消臂的 refund_turn（收尾窗口）
    refund_entered = asyncio.Event()
    model_entered = asyncio.Event()
    _arm_refund_on_gate(monkeypatch, refund_gate, refund_entered)
    harness = _build_harness(
        tmp_path, monkeypatch, [AIMessage(content="答")],
        gate=model_gate, model_cls=_SignalGateModel,
        model_kwargs={"model_entered": model_entered},
    )
    session_id = await _launch_run_parked_in_model(harness, model_entered)

    assert await harness.service.cancel(session_id) is True
    await harness.wait_for(lambda: refund_entered.is_set(), what="取消臂进入 refund 窗口")

    assert await harness.service.cancel(session_id) is True  # 幂等：True，但不再 task.cancel()
    run = harness.service._run_manager.get_active(session_id)
    assert run is not None and run.task is not None
    assert run.task.cancelling() == 1, "第二次 cancel 不得再请求取消（重复注入=终态丢失根因）"

    refund_gate.set()
    await harness.wait_for(
        lambda: harness.of_type(session_id, RUN_FAILED),
        timeout=5.0,
        what="取消终态 run/failed 落盘",
    )
    terminals = harness.of_type(session_id, RUN_FAILED)
    assert len(terminals) == 1, "终态必须恰一条（丢失/双终结都破坏可对账历史）"
    assert terminals[0].data["reason"] == "cancelled"


@pytest.mark.asyncio
async def test_second_raw_cancel_during_cleanup_still_writes_terminal(tmp_path, monkeypatch):
    """幂等层被绕过（原始 task.cancel 直接再注入）时，取消臂收尾也要自愈。

    吞掉再注入的取消、照常写唯一终态——运行时侧的最后防线：幂等层管不住所有
    取消来源（孤儿回收 / 关停 / 未来的旁路调用都直接 task.cancel()）。
    """
    model_gate = asyncio.Event()
    refund_gate = asyncio.Event()
    refund_entered = asyncio.Event()
    model_entered = asyncio.Event()
    _arm_refund_on_gate(monkeypatch, refund_gate, refund_entered)
    harness = _build_harness(
        tmp_path, monkeypatch, [AIMessage(content="答")],
        gate=model_gate, model_cls=_SignalGateModel,
        model_kwargs={"model_entered": model_entered},
    )
    session_id = await _launch_run_parked_in_model(harness, model_entered)

    assert await harness.service.cancel(session_id) is True
    await harness.wait_for(lambda: refund_entered.is_set(), what="取消臂进入 refund 窗口")

    run = harness.service._run_manager.get_active(session_id)
    run.task.cancel()  # 绕过 RunManager.cancel 的幂等层：任意旁路的再注入

    refund_gate.set()
    await harness.wait_for(
        lambda: harness.of_type(session_id, RUN_FAILED),
        timeout=5.0,
        what="取消终态 run/failed 落盘",
    )
    terminals = harness.of_type(session_id, RUN_FAILED)
    assert len(terminals) == 1
    assert terminals[0].data["reason"] == "cancelled"
