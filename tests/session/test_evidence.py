"""#352 [W-08]：验收项 ↔ 真实证据的服务端投影。

写侧：`evidence/recorded` append-only typed 事件（payload = 票面 14 字段 DTO）；
读侧：`derive_evidence_state(events)` 纯投影（按 criterion_id 聚合，幂等、可 replay）；
陈旧层：`compute_evidence_manifest` + `evaluate_evidence_freshness`（读取时求值，
产出明确过期原因；绝不沿用旧"通过"）。

`VerificationEntry.evidence`（`str|None` 人类摘要/指针）维持不变；`#524`
completion_evidence（Runtime 完成门）与本票（服务端投影）严格区分。
"""

from __future__ import annotations


def test_evidence_recorded_is_durable_event_type() -> None:
    """新事件 `evidence/recorded` 必须进 durable 词汇表（append-only，#3）。"""
    from agent_harness.session import event as event_mod

    assert event_mod.EVIDENCE_RECORDED == "evidence/recorded"
    assert event_mod.EVIDENCE_RECORDED in event_mod.EVENT_TYPES
    assert event_mod.EVIDENCE_RECORDED not in event_mod.STREAM_ONLY_TYPES
