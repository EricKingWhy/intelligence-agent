"""#1441 §1（F-09）：容量预算从部署配置注入 markdown store。

票面判据：`memory_char_limit` / `user_char_limit` 落到记忆配置段（实际载体是
`config.Settings`——`MemorySettingsV2` 是按用户的库内冻结设置，不是部署配置），
缺省 2200 / 1375；注入点两处，同一份预算：`assembly.build_memory_v2_service`（运行时）
与 `migration.migrate_active_records`（CLI `memory migrate`）。

这里钉三件事：
1. 缺省值就是 2200 / 1375，且与 `MarkdownMemoryStore` 的模块常量**逐值相等**——装配层
   永远显式传 Settings 值，构造默认值不在任何断言路径上，所以漂移只能靠直接比对常量抓；
2. 改配置后 store 的上限**随之变化**，且真的参与写入判定（不是只挂在属性上）；
3. `memory migrate` 与运行时读同一份预算（#1441 审查 P2）：调高上限后原本被跳过的行能
   迁入，调低上限后超限行进 `skipped` 而不是写出运行时读不进来的文件。
"""

from __future__ import annotations

import argparse
import sqlite3

import pytest

from agent_harness.config import Settings
from agent_harness.memory.v2.assembly import build_memory_v2_service
from agent_harness.memory.v2.markdown_store import (
    DEFAULT_MEMORY_CHAR_LIMIT,
    DEFAULT_USER_CHAR_LIMIT,
)

pytestmark = pytest.mark.asyncio


def _settings(tmp_path, **overrides) -> Settings:
    return Settings(_env_file=None, **{"workspace_dir": str(tmp_path), **overrides})


async def test_store_constants_match_settings_defaults() -> None:
    """装配层总是显式传参，构造默认值不会被走到——漂移只能在这里直接比对抓。"""
    assert DEFAULT_MEMORY_CHAR_LIMIT == Settings.model_fields["memory_char_limit"].default
    assert DEFAULT_USER_CHAR_LIMIT == Settings.model_fields["user_char_limit"].default


async def test_defaults_are_2200_and_1375(tmp_path) -> None:
    settings = _settings(tmp_path)
    assert settings.memory_char_limit == 2200
    assert settings.user_char_limit == 1375

    service = await build_memory_v2_service(settings)
    markdown = service.markdown
    assert markdown is not None, "装配层必须注入 markdown 权威面"
    assert markdown.memory_char_limit == 2200
    assert markdown.user_char_limit == 1375


async def test_configured_limits_reach_the_store_and_gate_writes(tmp_path) -> None:
    service = await build_memory_v2_service(
        _settings(tmp_path, memory_char_limit=40, user_char_limit=20),
    )
    markdown = service.markdown
    assert markdown is not None
    assert (markdown.memory_char_limit, markdown.user_char_limit) == (40, 20)

    # 上限不是装饰：40 字符的 MEMORY.md 收得下 39，收不下 41。写入口是唯一 seam。
    commit = service.commit
    assert (await commit.add("memory", "m" * 39))["success"] is True
    assert (await commit.add("memory", "n" * 3))["success"] is False
    assert (await commit.add("user", "u" * 20))["success"] is True
    assert (await commit.add("user", "v"))["success"] is False


# ---------------------------------------------------------------- 迁移路径同源（审查 P2）

_NOW = "2026-10-19T00:00:00+00:00"


def _seed_records(db_path, rows) -> None:
    """建 legacy 形状的 records 表（迁移只读这六列）。"""
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            CREATE TABLE memory_v2_records (
                memory_id TEXT PRIMARY KEY, kind TEXT, tier TEXT NOT NULL,
                content TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL
            )
            """
        )
        connection.executemany(
            "INSERT INTO memory_v2_records "
            "(memory_id, kind, tier, content, status, created_at) VALUES (?,?,?,?,?,?)",
            rows,
        )


async def test_raised_limit_lets_migration_export_rows_defaults_would_skip(tmp_path) -> None:
    """调高上限后，迁移与运行时用同一份预算：两条 1200 字的行都该迁入。

    默认 2200 下第二条会被跳过（1200 + 3 + 1200 = 2403）——这正是审查探针 A。
    """
    from agent_harness.cli import _memory_command

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _seed_records(workspace / "memory-v2.db", [
        ("a1", "semantic", "collection", "甲" * 1200, "active", _NOW),
        ("a2", "semantic", "collection", "乙" * 1200, "active", _NOW),
    ])

    output, failed = await _memory_command(
        _settings(tmp_path, workspace_dir=str(workspace), memory_char_limit=5000),
        argparse.Namespace(command="migrate"),
    )

    assert failed is False, output
    assert "导出=2" in output, output
    assert (workspace / "memories" / "MEMORY.md").read_text(encoding="utf-8").count("甲") == 1200


async def test_lowered_limit_makes_oversized_rows_skipped_not_written(tmp_path) -> None:
    """调低上限后，超限行进 `skipped`，且**不**写出运行时读不进来的超预算文件。

    默认 2200 下 1500 字能写进去；上限 1000 时迁移必须与运行时一致地拒写——审查探针 B。
    """
    from agent_harness.cli import _memory_command

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _seed_records(workspace / "memory-v2.db", [
        ("b1", "semantic", "collection", "丙" * 1500, "active", _NOW),
    ])

    output, failed = await _memory_command(
        _settings(tmp_path, workspace_dir=str(workspace), memory_char_limit=1000),
        argparse.Namespace(command="migrate"),
    )

    assert failed is True, "有 skipped 行时 CLI 必须非零退出"
    assert "未导出 1 条" in output, output
    memory_md = workspace / "memories" / "MEMORY.md"
    assert not memory_md.exists() or "丙" not in memory_md.read_text(encoding="utf-8")
