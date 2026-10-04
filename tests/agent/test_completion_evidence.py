"""完成门证据验证 + 纠偏臂（`#524`，选项 A：可选 domain policy）。

三层：
- **seam 纠偏臂**（runtime）：声明了 `correction_feedback` 的策略拒绝时，注入
  `completion/evidence-blocked` + 纠正 USER_MESSAGE（`injected_by=` 标记）并继续
  循环；未声明反馈的策略拒绝 ⇒ 既有 blocked 臂逐字不变（零写入、零注入）。
- **EvidenceCompletionPolicy**：声明式规则（主张正则 → 要求某工具的 success
  `tool/result`），证据只认 durable 事实；拒绝理由 `evidence_missing:<rule_id>`。
- **装配**：`build_runtime` / `AgentFactory` 透传 `completion_policy`，默认
  `None` = `DefaultCompletionPolicy`，全链行为零变化。

机制与取舍见 `docs/tickets/524-evidence-completion-policy-design.md`（ADR-0047
增补节随实现落盘）。
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage
from pydantic import BaseModel

from agent_harness.agent import AgentRuntime
from agent_harness.agent.completion import (
    CompletionDecision,
    CompletionPolicy,
    QuiescenceReport,
)
from agent_harness.agent.types import STATUS_COMPLETED, STATUS_QUIESCENCE_BLOCKED
from agent_harness.session.event import (
    RUN_COMPLETED,
    TOOL_CALL,
    TOOL_RESULT,
    USER_MESSAGE,
    SessionEvent,
)
from agent_harness.tooling import Tool, ToolExecutor, ToolRegistry, ToolResult
from tests.conftest import make_session
from tests.scripted_model import ScriptedModel


class _NoArgs(BaseModel):
    pass


class _ProbeTool(Tool):
    """证据工具替身：一次真实（无副作用）调用，成功结果即 durable 证据。"""

    @property
    def name(self) -> str:
        return "probe"

    @property
    def description(self) -> str:
        return "Return a fixed payload."

    @property
    def args_schema(self) -> type[BaseModel]:
        return _NoArgs

    async def execute(self, args: BaseModel) -> ToolResult:
        return ToolResult.success("probe ok")


class _FeedbackOncePolicy(CompletionPolicy):
    """第一次拒绝并给纠偏反馈，第二次接受（seam 级替身：duck-typed 反馈方法）。

    `correction_feedback` 是 #524 给 seam 增加的**非抽象默认方法**——本替身用它
    验证 runtime 纠偏臂的接线，不依赖 EvidenceCompletionPolicy 本体。
    """

    def __init__(self) -> None:
        self.decide_calls = 0
        self.feedback_asked = 0

    async def decide(
        self, *, report: QuiescenceReport, final_text: str, run_id: str,
        events: Sequence[SessionEvent] | None = None,
    ) -> CompletionDecision:
        self.decide_calls += 1
        if self.decide_calls == 1:
            return CompletionDecision(accepted=False, reason="evidence_missing:demo")
        return CompletionDecision(accepted=True)

    def correction_feedback(self, decision: CompletionDecision) -> str | None:
        if decision.accepted:
            return None
        self.feedback_asked += 1
        return "缺少 demo 证据：请先运行 probe 并引用其结果（勿重复已完成的工作）。"


def _runtime(
    policy: CompletionPolicy, responses: list[AIMessage],
) -> AgentRuntime:
    registry = ToolRegistry()
    registry.register(_ProbeTool())
    return AgentRuntime(
        ScriptedModel(responses),
        registry,
        ToolExecutor(registry),
        completion_policy=policy,
    )


def _injected_messages(session) -> list:
    return [
        event for event in session.events
        if event.type == USER_MESSAGE and event.data.get("injected_by")
    ]


@pytest.mark.asyncio
async def test_corrective_rejection_injects_feedback_and_continues(tmp_path: Path):
    """纠偏臂（seam 红先行的主判据）：拒绝 + 声明反馈 ⇒ 注入并继续，补证后完成。

    实现前形态（红）：第一次拒绝直接落 blocked 臂（零写入、零注入），run 结束、
    第二个模型响应永远不被消费。实现后：`completion/evidence-blocked` + 纠正
    USER_MESSAGE 落盘并镜像，回循环顶部（预算照判），probe 工具执行留下 durable
    证据，第二次 decide 接受 ⇒ `run/completed`。
    """
    policy = _FeedbackOncePolicy()
    runtime = _runtime(policy, [
        AIMessage(content="全部完成"),
        AIMessage(
            content="",
            tool_calls=[{"id": "call-1", "name": "probe", "args": {}}],
        ),
        AIMessage(content="全部完成（probe ok）"),
    ])
    session = make_session(tmp_path)

    result = await runtime.run(session, "把事情做完")

    assert policy.decide_calls == 2, "拒绝后必须回循环重新走到完成门"
    assert policy.feedback_asked == 1
    # 纠正消息 durable 且带注入标记（非真实用户发言——记忆抽取按标记单点过滤）。
    injected = _injected_messages(session)
    assert len(injected) == 1
    assert "demo" in injected[0].data["content"]
    assert injected[0].data["injected_by"] == "completion_evidence_policy"
    # 结构化拒绝事实：无主张原文、无参数值（D3 纪律）；rule_id 载于稳定 reason 串。
    blocked = [
        event for event in session.events
        if event.type == "completion/evidence-blocked"
    ]
    assert len(blocked) == 1
    assert blocked[0].data["reason"] == "evidence_missing:demo"
    assert blocked[0].data["policy"] == "_FeedbackOncePolicy"
    # 证据工具真的执行过一次（durable 事实对：TOOL_CALL 的 tool_name 配对
    # TOOL_RESULT 的 ok=True），不是靠文本"通过"。
    call_ids = [
        event.data["tool_call_id"] for event in session.events
        if event.type == TOOL_CALL
        and event.data.get("tool_name") == "probe"
    ]
    probe_results = [
        event for event in session.events
        if event.type == TOOL_RESULT
        and event.data["tool_call_id"] in call_ids
        and json.loads(event.data["content"])["ok"] is True
    ]
    assert len(probe_results) == 1
    assert any(event.type == RUN_COMPLETED for event in session.events)
    assert result.status == STATUS_COMPLETED


@pytest.mark.asyncio
async def test_plain_rejecting_policy_still_ends_blocked_without_injection(
    tmp_path: Path,
):
    """回归钉：未声明反馈的策略拒绝 ⇒ 既有 blocked 臂逐字不变（#316 契约）。"""
    class _PlainReject(CompletionPolicy):
        def __init__(self) -> None:
            self.decide_calls = 0

        async def decide(
            self, *, report: QuiescenceReport, final_text: str, run_id: str,
            events: Sequence[SessionEvent] | None = None,
        ) -> CompletionDecision:
            self.decide_calls += 1
            return CompletionDecision(accepted=False, reason="nope")

    policy = _PlainReject()
    runtime = _runtime(policy, [AIMessage(content="done")])
    session = make_session(tmp_path)

    result = await runtime.run(session, "把事情做完")

    assert result.status == STATUS_QUIESCENCE_BLOCKED
    assert policy.decide_calls == 1, "拒绝即结束，不回循环"
    assert _injected_messages(session) == []
    assert not any(
        event.type == "completion/evidence-blocked" for event in session.events
    )
    assert not any(event.type == RUN_COMPLETED for event in session.events)
