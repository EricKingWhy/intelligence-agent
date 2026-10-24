"""#1441 §2（F-10）：`kind_defaulted` 必须出现在迁移计数与 CLI 输出里。

票面判据：以 legacy 形状 fixture（`kind` 为 NULL 与空串的行各一）迁移后，
两行落点均为 MEMORY.md、计数含 `kind_defaulted`，且 CLI 输出可见；既有 AC9 3 例全绿。

#1441 审查后补强：计数判据与分拣规则（`target_for` 的**精确** kind 比较）同源，所以
`"USER"` / `" semantic "` 这类变体也走兜底、必须计入；同时 fixture 补一行
`kind="semantic", tier="collection"`——它按规则落 MEMORY.md，不是兜底，用来钉住判据的
另一半（判据退化成 `target == "memory"` 时这条会红）。

现行 schema 是 `kind TEXT NOT NULL`，插不进 NULL——所以这里**用直接 SQL 建旧形状表**，
而不是绕开校验造数据（票面明确允许这条路）。legacy 表只有迁移真正读的列：
`memory_id / kind / tier / content / status / created_at`。
"""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

import pytest

from agent_harness.config import Settings
from agent_harness.memory.v2.assembly import MEMORY_V2_DATABASE_NAME
from agent_harness.memory.v2.markdown_store import ENTRY_DELIMITER
from agent_harness.memory.v2.migration import migrate_active_records

pytestmark = pytest.mark.asyncio

_NOW = "2026-10-19T00:00:00+00:00"

#: `kind` 取 NULL / 空串 / 大小写变体 / 已知值 / 画像 tier 各一。
#:
#: 前四行都是 `kind_defaulted` 的对象：`target_for` 对 kind 做**精确**比较，所以
#: `"USER"` / `" semantic "` 与 NULL / 空串一样走兜底分支落进 MEMORY.md，必须计入。
#: `m-known`（`kind="semantic"`, `tier=collection`）是判据另一半的对照行：它**按规则**
#: 落 MEMORY.md，不是兜底——把判据退化成 `target == "memory"` 就会把它算进来。
#: `m-user` 走 USER.md，`m-profile` 由 tier 分拣，都与"kind 缺失"无关。
_LEGACY_ROWS = (
    ("m-null", None, "collection", "kind 为 NULL 的旧事实", "active", _NOW),
    ("m-empty", "", "collection", "kind 为空串的旧事实", "active", _NOW),
    ("m-upper", "USER", "collection", "kind 大写的旧事实", "active", _NOW),
    ("m-padded", " semantic ", "collection", "kind 带空格的旧事实", "active", _NOW),
    ("m-known", "semantic", "collection", "kind 合法的旧事实", "active", _NOW),
    ("m-user", "user", "collection", "用户叫林舟", "active", _NOW),
    ("m-profile", "semantic", "profile", "偏好用 pnpm", "active", _NOW),
    ("m-dead", None, "collection", "已失效的旧事实", "superseded", _NOW),
)


def _seed_legacy_table(db_path: Path) -> None:
    """建一张 legacy 形状的 `memory_v2_records`（kind 可空）并插入原始行。"""
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            CREATE TABLE memory_v2_records (
                memory_id TEXT PRIMARY KEY,
                kind TEXT,
                tier TEXT NOT NULL,
                content TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        connection.executemany(
            "INSERT INTO memory_v2_records "
            "(memory_id, kind, tier, content, status, created_at) VALUES (?,?,?,?,?,?)",
            _LEGACY_ROWS,
        )


def _entries(path: Path) -> list[str]:
    raw = path.read_text(encoding="utf-8-sig")
    return [entry.strip() for entry in raw.split(ENTRY_DELIMITER) if entry.strip()]


async def test_kind_missing_rows_are_counted_and_land_in_memory_md(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    db_path = workspace / MEMORY_V2_DATABASE_NAME
    _seed_legacy_table(db_path)

    counts = await migrate_active_records(db_path, workspace)

    assert counts["kind_defaulted"] == 4, f"NULL/空串/大小写变体各一，实际 {counts!r}"
    assert counts["active"] == 7, "superseded 行不参与迁移"
    assert counts["exported"] + counts["deduplicated"] == counts["active"]
    assert counts["skipped"] == []

    memory_entries = _entries(workspace / "memories" / "MEMORY.md")
    user_entries = _entries(workspace / "memories" / "USER.md")
    assert "kind 为 NULL 的旧事实" in memory_entries
    assert "kind 为空串的旧事实" in memory_entries
    assert "kind 大写的旧事实" in memory_entries
    assert "kind 带空格的旧事实" in memory_entries
    # 合法 kind 也落 MEMORY.md，但**不是**兜底——判据退化成 target=="memory" 时这条会红。
    assert "kind 合法的旧事实" in memory_entries
    assert "已失效的旧事实" not in memory_entries + user_entries
    # tier=profile 由 tier 分拣，不算 kind 兜底——否则计数会把画像行说成兜底。
    assert set(user_entries) == {"用户叫林舟", "偏好用 pnpm"}


async def test_cli_migrate_output_shows_kind_defaulted(tmp_path) -> None:
    """CLI 是用户唯一能看见这个计数的面：必须出现在输出里，且数字对得上。"""
    from agent_harness.cli import _memory_command

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _seed_legacy_table(workspace / MEMORY_V2_DATABASE_NAME)
    settings = Settings(_env_file=None, workspace_dir=str(workspace))

    output, failed = await _memory_command(settings, argparse.Namespace(command="migrate"))

    assert failed is False
    assert "kind 缺失默认归入 MEMORY.md：4 条" in output, output
    assert "active=7" in output, output
