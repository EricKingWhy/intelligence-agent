"""#1353 §4：把 `memory_v2_records` 的 active 行一次性迁入 markdown 权威面。

一次性、幂等、无丢失、无重复。分拣规则落在 `markdown_store.target_for`（与
`MemoryV2Service.markdown_target` 同一句话）：`kind='user'` 或 `tier='profile'` →
USER.md，其余 → MEMORY.md。两条路分拣不一致，迁移完第一条自动写入就会把事实挪到
另一个文件里。

幂等靠 store 的 `add`（同内容条目不重复追加），所以重跑安全：不需要迁移水位表，
也不需要"已迁移"标记——那种标记一旦与实际文件不同步，就是又一份需要维护的真相。

容量预算与运行时同源（#1441 §1）：调用方传 `memory_char_limit` / `user_char_limit`
（CLI 传 `Settings` 值），否则迁移按构造默认值判定，改过配置的部署会两条写入路径分叉。
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

#: 合法的 kind 取值（三种记忆形态）。`user` 不在这里：它由 `target_for` 分到 USER.md，
#: 根本到不了 MEMORY.md，放进已知集会是一条永远走不到的判据。
_KNOWN_KINDS = frozenset(kind.value for kind in MemoryKind)


def _kind_defaulted(kind: str | None, tier: str) -> bool:
    """这一行是否**因为 kind 不合法**才落进 MEMORY.md（`kind_defaulted` 的判据）。

    判据与分拣**同源**：`target_for` 对 kind 做精确比较，所以 `"USER"` / `" semantic "`
    这类变体走的也是兜底分支——这里就必须同样按精确值判定，不能自己先 `strip().lower()`
    （那样会把兜底落进 MEMORY.md 的行算成"kind 已知"，报告与落点对不上）。

    `tier='profile'` 的行由 tier 分拣（→ USER.md），与 kind 无关，不算"默认"——
    否则计数会把一条画像行说成"kind 缺失兜底到 MEMORY.md"，报告就成了假话。
    """
    return target_for(kind, tier) == "memory" and kind not in _KNOWN_KINDS


async def migrate_active_records(
    database_path: Path | str, workspace_dir: Path | str,
    *, memory_char_limit: int | None = None, user_char_limit: int | None = None,
) -> dict[str, Any]:
    """导出全部 active 行到 `<workspace_dir>/memories/{MEMORY,USER}.md`。

    返回 `{"active": 行数, "exported": n, "deduplicated": n, "memory": n, "user": n,
    "kind_defaulted": n, "skipped": [(memory_id, error), ...]}`。

    `kind_defaulted` 是 `memory` 里**因 kind 不合法（NULL / 空串 / 未知值，含大小写与
    空白变体）才落 MEMORY.md** 的条数（#1441 §2 追认的简化：不做内容启发式，落点与
    现行默认相同）。它不参与分账等式，只是报告可见性——`kind` 缺失的行以前会静默
    混进 `memory` 计数。

    两个 `*_char_limit` 与 `Settings` 同义（#1441 §1）：CLI 传入部署配置值，迁移与
    运行时才用同一份容量预算——否则调高上限后迁移仍会跳过本可迁入的行，调低上限后
    迁移会写出运行时读不进来的超预算文件。不传则退回 `MarkdownMemoryStore` 的构造
    默认值（既有调用方与测试的路径不变）。

    `exported + deduplicated == active` 且 `skipped == []` 才是成功。**`exported < active`
    本身不是去重**：threat 扫描命中与字符预算超限都是真实丢失，落在 `skipped` 里，
    调用方必须处理（CLI 据此非零退出）。
    """
    database = Path(database_path)
    # 只把调用方真的给了的上限传下去；缺省仍由 store 的构造默认值决定（不在这里抄第二份）。
    limits = {
        name: value
        for name, value in (
            ("memory_char_limit", memory_char_limit), ("user_char_limit", user_char_limit),
        )
        if value is not None
    }
    store = MarkdownMemoryStore(Path(workspace_dir) / MEMORIES_DIRECTORY_NAME, **limits)
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
            if _kind_defaulted(kind, tier):
                counts["kind_defaulted"] += 1
        else:
            error = str(response.get("error") or "unknown error")
            skipped.append((memory_id, error))
            logger.warning("迁移跳过一条（%s）：%s", target, error)
    return counts


__all__ = ["migrate_active_records"]
