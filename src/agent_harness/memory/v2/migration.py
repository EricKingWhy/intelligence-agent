"""#1353 §4：把 `memory_v2_records` 的 active 行一次性迁入 markdown 权威面。

一次性、幂等、无丢失、无重复。分拣规则落在 `markdown_store.target_for`（与
`MemoryV2Service.markdown_target` 同一句话）：`kind='user'` 或 `tier='profile'` →
USER.md，其余 → MEMORY.md。两条路分拣不一致，迁移完第一条自动写入就会把事实挪到
另一个文件里。

幂等靠 store 的 `add`（同内容条目不重复追加），所以重跑安全：不需要迁移水位表，
也不需要"已迁移"标记——那种标记一旦与实际文件不同步，就是又一份需要维护的真相。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from agent_harness.memory.v2._sqlite import connect
from agent_harness.memory.v2.assembly import MEMORIES_DIRECTORY_NAME
from agent_harness.memory.v2.markdown_store import MarkdownMemoryStore, target_for
from agent_harness.memory.v2.types import MemoryKind

logger = logging.getLogger(__name__)

#: 能按规则分拣的 kind 取值：`user` 走 USER.md，三种记忆形态走 MEMORY.md。
#: 之外的取值（NULL / 空串 / 未知串）都走 `target_for` 的兜底分支——#1441 §2 追认的
#: 「kind 缺失一律默认 MEMORY.md」，`kind_defaulted` 就是这些行的可见计数。
_KNOWN_KINDS = frozenset({"user", *(kind.value for kind in MemoryKind)})


def _kind_defaulted(kind: str | None, target: str) -> bool:
    """这一行是否**因为 kind 缺失**才落进 MEMORY.md（`kind_defaulted` 的判据）。

    `tier='profile'` 的行由 tier 分拣（→ USER.md），与 kind 无关，不算"默认"——
    否则计数会把一条画像行说成"kind 缺失兜底到 MEMORY.md"，报告就成了假话。
    """
    return target == "memory" and (kind or "").strip().lower() not in _KNOWN_KINDS


async def migrate_active_records(
    database_path: Path | str, workspace_dir: Path | str,
) -> dict[str, Any]:
    """导出全部 active 行到 `<workspace_dir>/memories/{MEMORY,USER}.md`。

    返回 `{"active": 行数, "exported": n, "deduplicated": n, "memory": n, "user": n,
    "kind_defaulted": n, "skipped": [(memory_id, error), ...]}`。

    `kind_defaulted` 是 `memory` 里**因 kind 缺失（NULL / 空串 / 未知值）才落 MEMORY.md**
    的条数（#1441 §2 追认的简化：不做内容启发式，落点与现行默认相同）。它不参与
    分账等式，只是报告可见性——`kind` 缺失的行以前会静默混进 `memory` 计数。

    `exported + deduplicated == active` 且 `skipped == []` 才是成功。**`exported < active`
    本身不是去重**：threat 扫描命中与字符预算超限都是真实丢失，落在 `skipped` 里，
    调用方必须处理（CLI 据此非零退出）。
    """
    database = Path(database_path)
    store = MarkdownMemoryStore(Path(workspace_dir) / MEMORIES_DIRECTORY_NAME)
    store.load_from_disk()

    rows: list[tuple[str, str, str, str]] = []
    if database.exists():
        async with connect(database) as connection:
            cursor = await connection.execute(
                "SELECT memory_id, kind, tier, content FROM memory_v2_records "
                "WHERE status='active' ORDER BY created_at, memory_id"
            )
            rows = [tuple(row) for row in await cursor.fetchall()]

    counts: dict[str, Any] = {
        "active": len(rows), "exported": 0, "deduplicated": 0,
        "memory": 0, "user": 0, "kind_defaulted": 0, "skipped": [],
    }
    skipped: list[tuple[str, str]] = counts["skipped"]
    for memory_id, kind, tier, content in rows:
        target = target_for(kind, tier)
        response = await store.add(target, content)
        if response.get("success"):
            counts["deduplicated" if response.get("deduplicated") else "exported"] += 1
            counts[target] += 1
            if _kind_defaulted(kind, target):
                counts["kind_defaulted"] += 1
        else:
            error = str(response.get("error") or "unknown error")
            skipped.append((memory_id, error))
            logger.warning("迁移跳过一条（%s）：%s", target, error)
    return counts


__all__ = ["migrate_active_records"]
