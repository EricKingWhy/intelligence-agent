"""EvidenceCompletionPolicy 单测（`#524` 设计定稿 §8 policy 层）。

只覆盖纯策略层（不做 runtime 循环——那条在 `test_completion_evidence.py`）：
规则构造校验（ConfigError）/ 理由稳定串 / 会话范围证据扫描（success 才算）/
`correction_feedback` 的各态 / `events=None` = 拿不到事实必须拒绝（不猜）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_harness.agent.completion import (
    QUIESCENCE_NEW_TOOL_CALLS,
    QuiescenceBlocker,
    QuiescenceReport,
)
from agent_harness.agent.completion_evidence import (
    EVIDENCE_MISSING_PREFIX,
    EvidenceCompletionPolicy,
    EvidenceRule,
)
from agent_harness.model.config import ConfigError
from agent_harness.session.event import (
    MODEL_COMPLETED,
    TOOL_CALL,
    TOOL_RESULT,
    SessionEvent,
)


def _report(quiescent: bool = True) -> QuiescenceReport:
    if quiescent:
        return QuiescenceReport()
    return QuiescenceReport(
        blockers=(QuiescenceBlocker(QUIESCENCE_NEW_TOOL_CALLS),),
    )


def _event(event_type: str, data: dict) -> SessionEvent:
    return SessionEvent(type=event_type, data=data)


def _probe_evidence(call_id: str = "call-1") -> list[SessionEvent]:
    """最小 durable 证据对：probe 的 tool/call + ok=true 的 tool/result。"""
    return [
        _event(MODEL_COMPLETED, {"content": "", "tool_calls": []}),
        _event(TOOL_CALL, {"tool_call_id": call_id, "tool_name": "probe", "args": {}}),
        _event(
            TOOL_RESULT,
            {"tool_call_id": call_id, "content": json.dumps({"ok": True})},
        ),
    ]


def _policy() -> EvidenceCompletionPolicy:
    return EvidenceCompletionPolicy(
        rules=(EvidenceRule(
            rule_id="demo", claim_pattern="全部完成", required_tool_name="probe",
        ),)
    )


class TestRuleValidation:
    def test_empty_rule_id_rejected(self) -> None:
        with pytest.raises(ConfigError, match="rule_id"):
            EvidenceRule(rule_id="", claim_pattern="x", required_tool_name="probe")

    def test_empty_tool_name_rejected(self) -> None:
        with pytest.raises(ConfigError, match="required_tool_name"):
            EvidenceRule(rule_id="r", claim_pattern="x", required_tool_name="")

    def test_bad_pattern_rejected(self) -> None:
        with pytest.raises(ConfigError, match="claim_pattern"):
            EvidenceRule(rule_id="r", claim_pattern="([)", required_tool_name="probe")

    def test_duplicate_rule_id_rejected(self) -> None:
        with pytest.raises(ConfigError, match="重复"):
            EvidenceCompletionPolicy(rules=(
                EvidenceRule(rule_id="r", claim_pattern="a", required_tool_name="probe"),
                EvidenceRule(rule_id="r", claim_pattern="b", required_tool_name="bash"),
            ))

    def test_iterator_rules_are_materialized(self) -> None:
        """两轴审查 P3 修复钉：生成器规则表不得被去重消费清空（静默变零规则
        = fail-open 陷阱）。"""
        def gen():
            yield EvidenceRule(
                rule_id="r", claim_pattern="a", required_tool_name="probe",
            )

        policy = EvidenceCompletionPolicy(rules=gen())
        assert len(policy.rules) == 1

    def test_non_string_pattern_rejected_as_config_error(self) -> None:
        """非字符串 pattern 抛 ConfigError（异常类型纪律），不是裸 TypeError。"""
        with pytest.raises(ConfigError, match="claim_pattern"):
            EvidenceRule(
                rule_id="r", claim_pattern=None,  # type: ignore[arg-type]
                required_tool_name="probe",
            )


class TestDecide:
    @pytest.mark.asyncio
    async def test_claim_without_evidence_rejected_with_stable_reason(self) -> None:
        policy = _policy()
        decision = await policy.decide(
            report=_report(), final_text="全部完成", run_id="run-1", events=[],
        )
        assert decision.accepted is False
        assert decision.reason == f"{EVIDENCE_MISSING_PREFIX}:demo"

    @pytest.mark.asyncio
    async def test_claim_with_success_evidence_accepted(self) -> None:
        policy = _policy()
        decision = await policy.decide(
            report=_report(), final_text="全部完成（probe ok）", run_id="run-1",
            events=_probe_evidence(),
        )
        assert decision.accepted is True

    @pytest.mark.asyncio
    async def test_failed_result_is_not_evidence(self) -> None:
        events = [
            _event(TOOL_CALL, {"tool_call_id": "c1", "tool_name": "probe", "args": {}}),
            _event(
                TOOL_RESULT,
                {"tool_call_id": "c1", "content": json.dumps({"ok": False})},
            ),
        ]
        decision = await _policy().decide(
            report=_report(), final_text="全部完成", run_id="run-1", events=events,
        )
        assert decision.accepted is False
        assert decision.reason == f"{EVIDENCE_MISSING_PREFIX}:demo"

    @pytest.mark.asyncio
    async def test_corrupt_result_content_is_not_evidence_and_does_not_raise(
        self,
    ) -> None:
        events = [
            _event(TOOL_CALL, {"tool_call_id": "c1", "tool_name": "probe", "args": {}}),
            _event(TOOL_RESULT, {"tool_call_id": "c1", "content": "not-json{"}),
        ]
        decision = await _policy().decide(
            report=_report(), final_text="全部完成", run_id="run-1", events=events,
        )
        assert decision.accepted is False

    @pytest.mark.asyncio
    async def test_claim_miss_is_accepted_without_evidence(self) -> None:
        """主张不命中 ⇒ 不要求证据（等同 default 的接受路径）。"""
        decision = await _policy().decide(
            report=_report(), final_text="还差一点", run_id="run-1", events=[],
        )
        assert decision.accepted is True

    @pytest.mark.asyncio
    async def test_zero_rules_accepts_everything(self) -> None:
        policy = EvidenceCompletionPolicy(rules=())
        decision = await policy.decide(
            report=_report(), final_text="任意文本", run_id="run-1", events=[],
        )
        assert decision.accepted is True

    @pytest.mark.asyncio
    async def test_events_none_is_rejection_not_pass(self) -> None:
        """拿不到 durable 事实必须拒绝而不是放行（不可得 ≠ 0，不猜）。"""
        decision = await _policy().decide(
            report=_report(), final_text="全部完成", run_id="run-1", events=None,
        )
        assert decision.accepted is False
        assert decision.reason == f"{EVIDENCE_MISSING_PREFIX}:demo"

    @pytest.mark.asyncio
    async def test_non_quiescent_report_rejected_with_quiescence_reason(self) -> None:
        decision = await _policy().decide(
            report=_report(quiescent=False), final_text="全部完成", run_id="run-1",
            events=None,
        )
        assert decision.accepted is False
        assert decision.reason is not None
        assert decision.reason.startswith("quiescence_blocked")


class TestCorrectionFeedback:
    @pytest.mark.asyncio
    async def test_accepted_decision_gets_none(self) -> None:
        policy = _policy()
        decision = await policy.decide(
            report=_report(), final_text="还差一点", run_id="run-1", events=[],
        )
        assert policy.correction_feedback(decision) is None

    @pytest.mark.asyncio
    async def test_evidence_missing_decision_gets_fragment(self) -> None:
        policy = _policy()
        decision = await policy.decide(
            report=_report(), final_text="全部完成", run_id="run-1", events=[],
        )
        feedback = policy.correction_feedback(decision)
        assert feedback is not None
        assert "demo" in feedback
        assert "probe" in feedback
        # 防重跑指令在片段里（设计 §3：纠正消息显式携带"引用既有结果、勿重复执行"）。
        assert "不需要重做" in feedback or "不要重复执行" in feedback

    @pytest.mark.asyncio
    async def test_non_evidence_rejection_gets_none(self) -> None:
        policy = _policy()
        decision = await policy.decide(
            report=_report(quiescent=False), final_text="全部完成", run_id="run-1",
            events=None,
        )
        assert policy.correction_feedback(decision) is None


class TestSessionScope:
    @pytest.mark.asyncio
    async def test_evidence_from_session_scope_counts(self, tmp_path: Path) -> None:
        """证据扫描是会话作用域：上次 run 留下的成功事实同样算数（ADR-0047 D1）。"""
        decision = await _policy().decide(
            report=_report(), final_text="全部完成", run_id="run-2",
            events=_probe_evidence(call_id="from-earlier-run"),
        )
        assert decision.accepted is True
