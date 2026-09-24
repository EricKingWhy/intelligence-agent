"""MEM-V2 测试共用的记录工厂。

放在 `_` 前缀模块里（pytest 不会把它当测试文件收集），避免每个测试文件各抄一份
构造代码——契约字段有 20+ 个，抄三份之后"改契约要改三处"必然漏。
"""

from __future__ import annotations

from agent_harness.memory.v2 import (
    EpisodicPayload,
    EvidenceItem,
    MemoryDraftV2,
    MemoryKind,
    MemoryRecordV2,
    MemoryScope,
    MemoryStatus,
    MemoryTier,
    ProceduralPayload,
    SemanticCategory,
    SemanticPayload,
    SourceType,
)

NOW = "2026-09-24T00:00:00+00:00"


def semantic_payload(**overrides) -> SemanticPayload:
    values = {"subject": "回答风格", "fact": "用户偏好简洁直接的回答",
              "category": SemanticCategory.PREFERENCE}
    values.update(overrides)
    return SemanticPayload(**values)


def episodic_payload(**overrides) -> EpisodicPayload:
    values = {"situation": "CI 在 Windows 上挂起", "action": "查 worker 退出日志",
              "outcome": "定位到端口占用", "lesson": "先确认端口再重跑"}
    values.update(overrides)
    return EpisodicPayload(**values)


def procedural_payload(**overrides) -> ProceduralPayload:
    values = {"trigger": "需要重跑前端单测", "procedure": "先清 test-results 再跑 vitest",
              "success_condition": "全绿且无残留"}
    values.update(overrides)
    return ProceduralPayload(**values)


def payload_for(kind: MemoryKind, **overrides):
    return {
        MemoryKind.SEMANTIC: semantic_payload,
        MemoryKind.EPISODIC: episodic_payload,
        MemoryKind.PROCEDURAL: procedural_payload,
    }[kind](**overrides)


def make_draft(**overrides) -> MemoryDraftV2:
    """写入意图：只有内容，没有 id / 版本 / 身份 / 时间戳（这些由存储层补齐）。"""
    kind = overrides.pop("kind", MemoryKind.SEMANTIC)
    values: dict = {
        "kind": kind,
        "tier": MemoryTier.COLLECTION,
        "scope": MemoryScope.USER_GLOBAL,
        "content": "用户偏好简洁直接的回答",
        "payload": payload_for(kind),
        "importance": 0.8,
        "strength": 0.9,
        "source_type": SourceType.AUTOMATIC,
        "source_session_id": "session-1",
        "source_event_ids": ["event-1"],
        "evidence": [EvidenceItem(role="user", excerpt="请简洁点", hash="hash-1")],
    }
    values.update(overrides)
    return MemoryDraftV2(**values)


def make_record(*, tenant_id: str = "tenant-a", user_id: str = "user-a",
                memory_id: str = "mem-1", root_id: str | None = None, version: int = 1,
                status: MemoryStatus = MemoryStatus.ACTIVE, **draft_overrides) -> MemoryRecordV2:
    """一条完整的信封（类型层测试用；存储层测试应走 draft）。"""
    draft = make_draft(**draft_overrides)
    return MemoryRecordV2(
        **draft.model_dump(),
        id=memory_id,
        root_id=root_id or memory_id,
        version=version,
        tenant_id=tenant_id,
        user_id=user_id,
        status=status,
        created_at=NOW,
        updated_at=NOW,
    )
