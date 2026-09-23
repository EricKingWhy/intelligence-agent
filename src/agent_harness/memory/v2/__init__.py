"""Memory V2（MEM-V2-1 起）的公共契约面。

对外只导出**契约**（类型、枚举、校验入口）与必要常量；实现细节（存储、outbox、
检索）由后续票据按需补充，并保持在此处显式列名——避免"包里的任何东西都能被
`from agent_harness.memory.v2 import *` 拿到"这种隐性扩大面。
"""

from __future__ import annotations

from agent_harness.memory.v2.types import (
    CONTENT_MAX_CHARS,
    EVIDENCE_EXCERPT_MAX_CHARS,
    PAYLOAD_FIELD_MAX_CHARS,
    SCHEMA_VERSION_V2,
    EpisodicPayload,
    EvidenceItem,
    MemoryDraftV2,
    MemoryKind,
    MemoryPayload,
    MemoryRecordV2,
    MemoryScope,
    MemoryStatus,
    MemoryTier,
    ProceduralPayload,
    SemanticCategory,
    SemanticPayload,
    SourceType,
    TrustedMemoryIdentity,
    UntrustedIdentityError,
    assert_trusted_identity,
)

__all__ = [
    "CONTENT_MAX_CHARS",
    "EVIDENCE_EXCERPT_MAX_CHARS",
    "PAYLOAD_FIELD_MAX_CHARS",
    "SCHEMA_VERSION_V2",
    "EpisodicPayload",
    "EvidenceItem",
    "MemoryDraftV2",
    "MemoryKind",
    "MemoryPayload",
    "MemoryRecordV2",
    "MemoryScope",
    "MemoryStatus",
    "MemoryTier",
    "ProceduralPayload",
    "SemanticCategory",
    "SemanticPayload",
    "SourceType",
    "TrustedMemoryIdentity",
    "UntrustedIdentityError",
    "assert_trusted_identity",
]
