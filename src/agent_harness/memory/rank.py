"""记忆排序的共享口径（ADR-0031 D1/D2）。

provider 自动注入与 `retrieve_memory` 工具共用**同一份**排序函数——两套检索
口径迟早对不上账（`capability.py` 既有措辞的担忧），所以排序只落这一处。
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime

from agent_harness.memory.types import MemoryEntry


def rank_entries(entries: Iterable[MemoryEntry], *, now: datetime | None = None) -> list[MemoryEntry]:
    """按 0.7*score + 0.2*importance + 0.1*recency 排序（降序）。

    provider 与 `retrieve_memory` 工具共用；**禁止**在调用方重写一份公式。

    recency 锚（#416）：`now` 是**确定性时间锚**，不是 wall clock——调用方禁止传
    `datetime.now()`。缺省（None）= 候选集批内最新 `created_at`（durable per-entry
    时间）；provider 路径显式传会话事件流最新 durable 事件时间。同一事件流 +
    同一候选集 ⇒ 输出逐位相同 ⇒ 注入前缀稳定（KV-cache 前提）。
    """
    materialized = list(entries)  # entries 可能是单次 iterable；锚与排序共用一份
    if not materialized:
        return []
    current = now or max(_parse_created_at(e) for e in materialized)

    def key(entry: MemoryEntry) -> float:
        parsed = _parse_created_at(entry)
        age_days = max(0, (current - parsed).total_seconds() / 86400)
        importance = max(0, min(1, float(entry.metadata.get("importance", 0.5))))
        return 0.7 * (entry.score or 0) + 0.2 * importance + 0.1 / (1 + age_days)

    # 确定性 tie-break：key 相同按 id 升序（近平票压平的最后一道；替代
    # 「stable-sort 保输入序」——输入序来自向量库返回序，不是稳定契约）。
    return sorted(materialized, key=lambda entry: (-key(entry), entry.id))


def _parse_created_at(entry: MemoryEntry) -> datetime:
    """created_at（types.py 的 ISO 字符串）→ aware UTC datetime。"""
    parsed = datetime.fromisoformat(entry.created_at)
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)
