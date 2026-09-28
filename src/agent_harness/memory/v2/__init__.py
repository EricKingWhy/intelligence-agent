"""Memory V2（MEM-V2-1 起）的公共契约面。

对外只导出**契约**（类型、枚举、校验入口、能力 Protocol）；实现细节（存储、outbox、
检索）由后续票据按需补充，并保持在此处显式列名——避免"包里的任何东西都能被
`from agent_harness.memory.v2 import *` 拿到"这种隐性扩大面。

上界常量（`SCHEMA_VERSION_V2` / `CONTENT_MAX_CHARS` / `EVIDENCE_EXCERPT_MAX_CHARS` /
`PAYLOAD_FIELD_MAX_CHARS`）刻意**不**在这里再导出：它们已被字段定义自己消费
（`Literal[2]` 与各个 `Field(max_length=...)`），调用方不需要第二个入口；
真要引用就从 `agent_harness.memory.v2.types` 取。
"""

from __future__ import annotations

from agent_harness.memory.v2.capability import MemoryV2Capability
from agent_harness.memory.v2.types import (
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
    "EpisodicPayload",
    "EvidenceItem",
    "MemoryDraftV2",
    "MemoryKind",
    "MemoryPayload",
    "MemoryRecordV2",
    "MemoryScope",
    "MemoryStatus",
    "MemoryTier",
    "MemoryV2Capability",
    "ProceduralPayload",
    "SemanticCategory",
    "SemanticPayload",
    "SourceType",
    "TrustedMemoryIdentity",
    "UntrustedIdentityError",
    "assert_trusted_identity",
]
