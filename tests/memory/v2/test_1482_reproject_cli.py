"""#1482 Finding 二：`agent-harness memory reproject` 的端到端验收（AC4 / AC5 / AC6）。

票面 §2.2 甲案：给 `MemoryV2Service.reproject` 接真实 CLI 入口，warning 指向它，且入口
不许假成功。三条 AC 分别覆盖：降级库全流程走通、文案与入口防漂移、四种失败情形的退出码。
"""

from __future__ import annotations

import asyncio
import os
import re
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from agent_harness import cli
from agent_harness.config import Settings
from agent_harness.memory.v2 import reprojection
from agent_harness.memory.v2 import store as store_module
from agent_harness.memory.v2.capability import MemoryV2Service
from agent_harness.memory.v2.markdown_store import MarkdownMemoryStore
from agent_harness.memory.v2.store import SqliteMemoryV2Store
from agent_harness.memory.v2.types import TrustedMemoryIdentity

TRUSTED = TrustedMemoryIdentity("local", "local")
SRC_ROOT = Path(__file__).resolve().parents[3] / "src"


def _settings(tmp_path: Path) -> Settings:
    return Settings(_env_file=None, workspace_dir=str(tmp_path), model_api_key="sk-test")


def _main(monkeypatch, settings: Settings) -> None:
    """把入口层的外部依赖钉到 tmp 工作区：Settings 与日志落盘。"""
    monkeypatch.setattr(cli, "Settings", lambda: settings)
    monkeypatch.setattr(cli, "setup_logging", lambda *args, **kwargs: None)


def _index_unique(db_path: Path) -> bool:
    with sqlite3.connect(db_path) as connection:
        return {
            name: bool(unique)
            for name, unique in connection.execute(
                "SELECT name, \"unique\" FROM pragma_index_list('memory_v2_records')")
        }["memory_v2_one_tool_projection"]


def _pending_count(db_path: Path) -> int:
    with sqlite3.connect(db_path) as connection:
        return connection.execute(
            "SELECT COUNT(*) FROM memory_v2_projection_pending").fetchone()[0]


async def _degraded_workspace(settings: Settings) -> Path:
    """造法同 `test_legacy_duplicate_tool_rows_degrade_to_a_plain_index`（:626）。"""
    db_path = Path(settings.workspace_dir) / "memory-v2.db"
    store = SqliteMemoryV2Store(db_path)
    await store.initialize()
    with sqlite3.connect(db_path) as connection:
        connection.execute("DROP INDEX memory_v2_one_tool_projection")
    draft = reprojection.draft_for("memory", "历史重复行")
    for _ in range(2):
        await store.create(
            draft, TRUSTED, projection_source="tool", projection_target="memory")
    await SqliteMemoryV2Store(db_path).initialize()
    assert _index_unique(db_path) is False
    return db_path


# --------------------------------------------------------------------- AC4


def test_reproject_entry_drains_duplicates_and_restores_the_unique_index(
    tmp_path, monkeypatch, capsys,
) -> None:
    """AC4：降级库 + 两个 markdown 文件都没有重复文本 → 每跑一次少一行 → 删空后重启恢复唯一索引。"""
    settings = _settings(tmp_path)
    db_path = asyncio.run(_degraded_workspace(settings))
    (tmp_path / "memories").mkdir()
    _main(monkeypatch, settings)

    cli._main_memory(["reproject", "--target", "memory"])

    printed = capsys.readouterr().out
    assert "memory: created=0 deleted=1" in printed, printed
    assert len(asyncio.run(_active_contents(db_path))) == 1

    cli._main_memory(["reproject", "--target", "memory"])

    assert asyncio.run(_active_contents(db_path)) == []
    # 唯一索引只在下一次启动时恢复（`store._ensure_tool_projection_index` 的既定路径）。
    asyncio.run(SqliteMemoryV2Store(db_path).initialize())
    assert _index_unique(db_path) is True


async def _active_contents(db_path: Path) -> list[str]:
    return [
        record.content
        for record in await SqliteMemoryV2Store(db_path).list_active(TRUSTED, limit=10)
    ]


# --------------------------------------------------------------------- AC5


def _warning_command(source: str) -> str:
    """从 store.py 的降级 warning 里取出点名的子命令。"""
    match = re.search(r"run `agent-harness memory (\w+)`", source)
    assert match is not None, source
    return match.group(1)


def test_the_subcommand_the_warning_names_is_really_registered() -> None:
    """AC5：子进程跑真实入口的 `--help`（不是只断言文案字符串存在），再与文案字面比对。"""
    command = _warning_command(Path(store_module.__file__).read_text(encoding="utf-8"))

    proc = subprocess.run(
        [sys.executable, "-m", "agent_harness.cli", "memory", command, "--help"],
        capture_output=True, text=True, encoding="utf-8", timeout=180, check=False,
        env={**os.environ, "PYTHONPATH": str(SRC_ROOT)},
    )

    assert proc.returncode == 0, proc.stderr
    assert f"usage: agent-harness memory {command}" in proc.stdout
    assert "--target" in proc.stdout


# --------------------------------------------------------------------- AC6


def test_entry_exits_non_zero_when_the_ledger_still_has_rows(
    tmp_path, monkeypatch, capsys,
) -> None:
    """AC6(i)：`remaining > 0`（撤回持续失败）⇒ 非零退出，且逐 target 摘要与原因都在。"""
    settings = _settings(tmp_path)
    db_path = Path(settings.workspace_dir) / "memory-v2.db"
    (tmp_path / "memories").mkdir()
    async def _seed():
        store = SqliteMemoryV2Store(db_path)
        await store.initialize()
        await store.create(
            reprojection.draft_for("memory", "账本积压句"), TRUSTED,
            projection_source="tool", projection_target="memory")
        await store.enqueue_projection(TRUSTED, "memory", "账本积压句")

    asyncio.run(_seed())

    async def _failing_delete(*args, **kwargs):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(SqliteMemoryV2Store, "delete", _failing_delete)
    _main(monkeypatch, settings)

    with pytest.raises(SystemExit) as excinfo:
        cli._main_memory(["reproject"])

    printed = capsys.readouterr().out
    assert excinfo.value.code == 2
    assert "memory: created=0 deleted=0" in printed
    assert "user: created=0 deleted=0" in printed
    assert "账本仍有 1 行待收敛" in printed
    assert "未收敛：" in printed
    assert _pending_count(db_path) == 2  # 行没被清掉（两个 target 各记一次）


def test_a_run_that_converges_in_reproject_target_does_not_report_a_backlog(
    tmp_path, monkeypatch, capsys,
) -> None:
    """#1482 F3：drain 阶段删失败一次、对账阶段删成功 ⇒ 本轮 `remaining == 0`、CLI 退 0。

    `remaining` 若取 drain 的旧快照，一次已经收敛的运行会仍报「账本积压」并退 2——对脚本
    化调用方是假红。这里复现审查员探针 `probe_stale_remaining.py` 的场景。
    """
    settings = _settings(tmp_path)
    db_path = Path(settings.workspace_dir) / "memory-v2.db"
    (tmp_path / "memories").mkdir()
    async def _seed():
        store = SqliteMemoryV2Store(db_path)
        await store.initialize()
        await store.create(
            reprojection.draft_for("memory", "瞬时积压句"), TRUSTED,
            projection_source="tool", projection_target="memory")
        await store.enqueue_projection(TRUSTED, "memory", "瞬时积压句")

    asyncio.run(_seed())

    real_delete = SqliteMemoryV2Store.delete
    state = {"calls": 0}

    async def _flaky_delete(self, *args, **kwargs):
        state["calls"] += 1
        if state["calls"] == 1:
            raise sqlite3.OperationalError("disk I/O error")
        return await real_delete(self, *args, **kwargs)

    monkeypatch.setattr(SqliteMemoryV2Store, "delete", _flaky_delete)
    _main(monkeypatch, settings)

    cli._main_memory(["reproject", "--target", "memory"])  # 不抛 SystemExit

    printed = capsys.readouterr().out
    assert state["calls"] == 2, state  # drain 失败一次 + 对账成功一次
    assert "deleted=1" in printed, printed
    assert "账本仍有" not in printed, printed
    assert "未收敛" not in printed, printed
    assert _pending_count(db_path) == 0
    assert asyncio.run(_active_contents(db_path)) == []


def test_backlog_in_the_other_target_does_not_fail_a_single_target_run(
    tmp_path, monkeypatch, capsys,
) -> None:
    """#1482 B1：`remaining` 必须**只数本 target**——user 槽有积压时跑 `--target memory` 不许假报。

    复审员探针（跨 target）：user 槽一行账本积压（另一文件仍含该句 ⇒ 撤回判 `kept`，账本行
    保留），跑 `--target memory` 时若拿全局计数就会打印「账本仍有 1 行待收敛」并退 2；
    按 target 过滤后本轮无积压、退 0。随后跑全量 `reproject`，user 槽的积压必须如实报出并退 2
    （不把这半条修成"少报"）。
    """
    settings = _settings(tmp_path)
    db_path = Path(settings.workspace_dir) / "memory-v2.db"
    memories = tmp_path / "memories"
    memories.mkdir()
    # 共享行由 MEMORY.md 兜着 ⇒ user 槽那行的撤回判 `kept`，账本行不会被清掉（真积压）；
    # USER.md 里没有它，所以这不是「已经一致」的假积压。
    (memories / "MEMORY.md").write_text("共享积压句", encoding="utf-8")
    (memories / "USER.md").write_text("另一句", encoding="utf-8")

    async def _seed():
        store = SqliteMemoryV2Store(db_path)
        await store.initialize()
        await store.create(
            reprojection.draft_for("user", "共享积压句"), TRUSTED,
            projection_source="tool", projection_target="user")
        await store.enqueue_projection(TRUSTED, "user", "共享积压句")

    asyncio.run(_seed())
    _main(monkeypatch, settings)

    cli._main_memory(["reproject", "--target", "memory"])  # 不抛 SystemExit

    printed = capsys.readouterr().out
    assert "memory: created=0 deleted=0" in printed, printed
    assert "账本仍有" not in printed, printed
    assert _pending_count(db_path) == 1  # user 槽那一行原样保留

    with pytest.raises(SystemExit) as excinfo:
        cli._main_memory(["reproject"])

    printed = capsys.readouterr().out
    assert excinfo.value.code == 2
    assert "账本仍有 1 行待收敛" in printed, printed
    assert "user 账本积压 1 行" in printed, printed  # 积压归到 user，不记到 memory 头上


def test_entry_exits_non_zero_when_a_target_still_has_pending_projections(
    tmp_path, monkeypatch, capsys,
) -> None:
    """AC6(ii)：任一 target `pending`（投影写入失败）⇒ 非零退出。"""
    settings = _settings(tmp_path)
    memories = tmp_path / "memories"
    memories.mkdir()
    (memories / "MEMORY.md").write_text("投影会失败的一句", encoding="utf-8")

    async def _failing_create(*args, **kwargs):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(SqliteMemoryV2Store, "create", _failing_create)
    _main(monkeypatch, settings)

    with pytest.raises(SystemExit) as excinfo:
        cli._main_memory(["reproject", "--target", "memory"])

    printed = capsys.readouterr().out
    assert excinfo.value.code == 2
    assert "pending=True" in printed
    assert "仍有待投影项" in printed


def test_entry_exits_non_zero_when_a_target_is_unreadable(
    tmp_path, monkeypatch, capsys,
) -> None:
    """AC6(iii)：文件读不出来（`unreadable` 非空）⇒ 非零退出，不许报全零成功。"""
    settings = _settings(tmp_path)
    memories = tmp_path / "memories"
    memories.mkdir()
    (memories / "MEMORY.md").write_text("有内容", encoding="utf-8")
    user_path = memories / "USER.md"
    real = MarkdownMemoryStore.read_raw_checked

    def flaky(path):
        return ("", False) if Path(path) == user_path else real(path)

    monkeypatch.setattr(MarkdownMemoryStore, "read_raw_checked", staticmethod(flaky))
    _main(monkeypatch, settings)

    with pytest.raises(SystemExit) as excinfo:
        cli._main_memory(["reproject"])

    printed = capsys.readouterr().out
    assert excinfo.value.code == 2
    assert "user: 文件读不出来，本轮未对账" in printed
    assert "user 不可读" in printed


def test_markdown_readable_with_the_other_file_unreadable_does_not_blame_the_wrong_file(
    tmp_path, monkeypatch, capsys,
) -> None:
    """#1482 F2：USER.md 读不出来时，跑 `--target memory` 不得声称 MEMORY.md/memory 不可读。

    `unreadable` 必须指向真正读不出的文件；把可读的那个说成不可读会把运维引去修错文件。
    造法同审查员探针 `probe_cli_misattrib.py`：MEMORY.md 可读、USER.md 钉成不可读，且
    派生层有一条 markdown 里已没有的工具行——memory 侧的撤回因此判 `unknown`。
    退码语义不变（有不可读 ⇒ 2），轮到 user 时如实报。
    """
    settings = _settings(tmp_path)
    db_path = Path(settings.workspace_dir) / "memory-v2.db"
    memories = tmp_path / "memories"
    memories.mkdir()
    (memories / "MEMORY.md").write_text("可读的一句", encoding="utf-8")
    (memories / "USER.md").write_text("", encoding="utf-8")
    async def _seed():
        store = SqliteMemoryV2Store(db_path)
        await store.initialize()
        await store.create(
            reprojection.draft_for("memory", "被删掉的句子"), TRUSTED,
            projection_source="tool", projection_target="memory")

    asyncio.run(_seed())
    user_path = memories / "USER.md"
    real = MarkdownMemoryStore.read_raw_checked
    monkeypatch.setattr(
        MarkdownMemoryStore, "read_raw_checked",
        staticmethod(lambda path: ("", False) if Path(path) == user_path else real(path)),
    )
    _main(monkeypatch, settings)

    with pytest.raises(SystemExit) as excinfo:
        cli._main_memory(["reproject"])

    printed = capsys.readouterr().out
    assert excinfo.value.code == 2
    # 可读的 memory 不得被说成不可读（旧实现会打印「memory: 文件读不出来」+「memory 不可读」）。
    assert "memory: 文件读不出来" not in printed, printed
    assert "memory 不可读" not in printed, printed
    # 被阻断的 memory 要如实说是「谁」读不出来（中性文案），不能只说「memory 有问题」。
    assert "user 读不出来，本轮无法判定 memory" in printed, printed
    # 真正不可读的 user 如实报，退码语义不变。
    assert "user: 文件读不出来，本轮未对账" in printed, printed
    assert "user 不可读" in printed, printed
    # 派生行没被误删、账本行仍在（未知不等于已一致）。
    assert "被删掉的句子" in asyncio.run(_active_contents(db_path))
    assert _pending_count(db_path) == 1


def test_entry_exits_non_zero_when_markdown_is_not_assembled(
    tmp_path, monkeypatch, capsys,
) -> None:
    """AC6(iv)：markdown 未装配 ⇒ 直接报错退出，不许打印全零摘要装作成功。"""
    settings = _settings(tmp_path)
    (tmp_path / "memories").mkdir()
    monkeypatch.setattr(MemoryV2Service, "markdown", property(lambda self: None))
    _main(monkeypatch, settings)

    with pytest.raises(SystemExit) as excinfo:
        cli._main_memory(["reproject"])

    printed = capsys.readouterr().out
    assert excinfo.value.code == 2
    assert "未装配 markdown 权威面" in printed
    # 票面 §2.2：不许打印全零摘要装作成功（#1482 F4）。
    assert "created=" not in printed, printed
