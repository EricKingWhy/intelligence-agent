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
    """
    current = now or datetime.now(UTC)

    def key(entry: MemoryEntry) -> float:
        parsed = datetime.fromisoformat(entry.created_at)
        parsed = parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)
        age_days = max(0, (current - parsed).total_seconds() / 86400)
        importance = max(0, min(1, float(entry.metadata.get("importance", 0.5))))
        return 0.7 * (entry.score or 0) + 0.2 * importance + 0.1 / (1 + age_days)

    return sorted(entries, key=key, reverse=True)
