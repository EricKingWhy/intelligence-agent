"""#1353 AC9：`memory_v2_records` active 行一次性迁入 markdown（无丢失、无重复）。

判定（票面 §4）：
- 导出条目数 == active 行数；
- 去重后条目数 == 导出条目数；
- kind=user / tier=profile → USER.md；其余 → MEMORY.md；
- 非 active 行不得导出；重复执行不得产生重复条目。
"""

from __future__ import annotations

import importlib
import inspect
import sqlite3
from pathlib import Path

import pytest

from tests.memory.v2._markdown_memory_1353 import DELIMITER

pytestmark = pytest.mark.asyncio

_MIGRATION_MODULES = (
    "agent_harness.memory.v2.migration",
    "agent_harness.memory.v2.migrate",
    "agent_harness.memory.v2.markdown_store",
)
_MIGRATION_FUNCS = (
    "migrate_active_records",
    "migrate_memory_v2_to_markdown",
    "export_active_records",
    "migrate_from_sqlite",
    "migrate",
)

_NOW = "2026-10-19T00:00:00+00:00"


def _migration_callable():
    for module_name in _MIGRATION_MODULES:
        try:
            module = importlib.import_module(module_name)
        except ModuleNotFoundError:
            continue
        for name in _MIGRATION_FUNCS:
            candidate = getattr(module, name, None)
            if callable(candidate):
                return candidate
    pytest.fail(
        "#1353 AC9：未找到迁移入口；尝试过 "
        f"{_MIGRATION_MODULES} × {_MIGRATION_FUNCS}",
        pytrace=False,
    )


async def _run_migration(db_path: Path, workspace_dir: Path):
    func = _migration_callable()
    attempts = (
        lambda: func(db_path, workspace_dir),
        lambda: func(workspace_dir, db_path),
        lambda: func(database=db_path, workspace_dir=workspace_dir),
        lambda: func(db_path=db_path, workspace_dir=workspace_dir),
        lambda: func(database_path=db_path, workspace_dir=workspace_dir),
    )
    last: TypeError | None = None
    for factory in attempts:
        try:
            result = factory()
        except TypeError as exc:
            last = exc
            continue
        if inspect.isawaitable(result):
            result = await result
        return result
    pytest.fail(f"#1353 AC9：迁移入口构造形态不匹配：{last}", pytrace=False)


async def _seed(db_path: Path) -> int:
    from agent_harness.memory.v2.store import SqliteMemoryV2Store

    store = SqliteMemoryV2Store(db_path)
    await store.initialize()
    closer = getattr(store, "aclose", None) or getattr(store, "close", None)
    if closer is not None:
        result = closer()
        if inspect.isawaitable(result):
            await result

    columns = (
        "memory_id, schema_version, root_id, version, kind, tier, scope, tenant_id, user_id, "
        "project_id, content, payload, status, importance, strength, source_type, "
        "source_session_id, source_event_ids, evidence, valid_at, invalidated_at, superseded_by, "
        "created_at, updated_at"
    )
    rows = [
        ("m1", 2, "r1", 1, "user", "profile", "user_global", "local", "local", None,
         "用户叫林舟", "{}", "active", 0.9, 0.9, "user_message", None, "[]", "[]",
         None, None, None, _NOW, _NOW),
        ("m2", 2, "r2", 1, "semantic", "profile", "user_global", "local", "local", None,
         "偏好用 pnpm 而不是 npm", "{}", "active", 0.8, 0.8, "user_message", None, "[]", "[]",
         None, None, None, _NOW, _NOW),
        ("m3", 2, "r3", 1, "semantic", "collection", "user_global", "local", "local", None,
         "部署环境是 Ubuntu 24.04", "{}", "active", 0.7, 0.7, "model", None, "[]", "[]",
         None, None, None, _NOW, _NOW),
        ("m4", 2, "r4", 1, "episodic", "collection", "user_global", "local", "local", None,
         "项目使用 Python 3.12", "{}", "active", 0.6, 0.6, "tool", None, "[]", "[]",
         None, None, None, _NOW, _NOW),
        # 非 active：绝不能导出。
        ("m5", 2, "r5", 1, "semantic", "collection", "user_global", "local", "local", None,
         "已失效的旧事实", "{}", "superseded", 0.5, 0.5, "model", None, "[]", "[]",
         None, None, None, _NOW, _NOW),
    ]
    with sqlite3.connect(db_path) as connection:
        connection.executemany(
            f"INSERT INTO memory_v2_records ({columns}) VALUES ({','.join('?' * 24)})",
            rows,
        )
        connection.commit()
    return 4  # active 行数


def _entries(path: Path) -> list[str]:
    raw = path.read_text(encoding="utf-8-sig")
    return [entry.strip() for entry in raw.split(DELIMITER) if entry.strip()]


def _migrated_files(workspace: Path) -> tuple[Path, Path]:
    memory = list(workspace.rglob("MEMORY.md"))
    user = list(workspace.rglob("USER.md"))
    assert len(memory) == 1, f"迁移后应恰有一份 MEMORY.md，实际 {memory}"
    assert len(user) == 1, f"迁移后应恰有一份 USER.md，实际 {user}"
    return memory[0], user[0]


async def test_active_rows_export_without_loss_or_duplicates(tmp_path) -> None:
    db_path = tmp_path / "memory-v2.db"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    active_count = await _seed(db_path)
    await _run_migration(db_path, workspace)
    memory_path, user_path = _migrated_files(workspace)

    memory_entries = _entries(memory_path)
    user_entries = _entries(user_path)

    assert len(memory_entries) + len(user_entries) == active_count, "导出总数必须等于 active 行数"
    assert len(set(memory_entries)) == len(memory_entries), "MEMORY.md 出现重复"
    assert len(set(user_entries)) == len(user_entries), "USER.md 出现重复"
    assert set(user_entries) == {"用户叫林舟", "偏好用 pnpm 而不是 npm"}
    assert set(memory_entries) == {"部署环境是 Ubuntu 24.04", "项目使用 Python 3.12"}
    assert "已失效的旧事实" not in memory_entries + user_entries


async def test_migration_is_idempotent(tmp_path) -> None:
    db_path = tmp_path / "memory-v2.db"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    await _seed(db_path)

    await _run_migration(db_path, workspace)
    memory_path, user_path = _migrated_files(workspace)
    first = (_entries(memory_path), _entries(user_path))
    await _run_migration(db_path, workspace)
    second = (_entries(memory_path), _entries(user_path))

    assert second == first, "重复执行迁移不得产生重复条目"
