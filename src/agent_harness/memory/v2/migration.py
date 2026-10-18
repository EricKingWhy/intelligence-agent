"""#1353 §4：把 `memory_v2_records` 的 active 行一次性迁入 markdown 权威面。

一次性、幂等、无丢失、无重复。分拣规则与 `MemoryV2Service.markdown_target` **同一句话**：
`kind='user'` 或 `tier='profile'` → USER.md，其余 → MEMORY.md。两条路分拣不一致，
迁移完第一条自动写入就会把事实挪到另一个文件里。

幂等靠 store 的 `add`（同内容条目不重复追加），所以重跑安全：不需要迁移水位表，
也不需要"已迁移"标记——那种标记一旦与实际文件不同步，就是又一份需要维护的真相。
"""

from __future__ import annotations

import logging
from pathlib import Path

from agent_harness.memory.v2._sqlite import connect
from agent_harness.memory.v2.assembly import MEMORIES_DIRECTORY_NAME
from agent_harness.memory.v2.markdown_store import MarkdownMemoryStore

logger = logging.getLogger(__name__)


async def migrate_active_records(
    database_path: Path | str, workspace_dir: Path | str,
) -> dict[str, int]:
    """导出全部 active 行到 `<workspace_dir>/memories/{MEMORY,USER}.md`。

    返回 `{"active": 行数, "exported": 实际写入条数, "memory": n, "user": n}`——
    `exported` 小于 `active` 只可能是同内容重复（去重，不是丢失）。
    """
    database = Path(database_path)
    store = MarkdownMemoryStore(Path(workspace_dir) / MEMORIES_DIRECTORY_NAME)
    store.load_from_disk()

    rows: list[tuple[str, str, str]] = []
    if database.exists():
        async with connect(database) as connection:
            cursor = await connection.execute(
                "SELECT kind, tier, content FROM memory_v2_records "
                "WHERE status='active' ORDER BY created_at, memory_id"
            )
            rows = [tuple(row) for row in await cursor.fetchall()]

    counts = {"active": len(rows), "exported": 0, "memory": 0, "user": 0}
    for kind, tier, content in rows:
        target = "user" if tier == "profile" or kind == "user" else "memory"
        response = await store.add(target, content)
        if response.get("success"):
            counts["exported"] += 1
            counts[target] += 1
        else:
            logger.warning("迁移跳过一条（%s）：%s", target, response.get("error"))
    return counts


__all__ = ["migrate_active_records"]
