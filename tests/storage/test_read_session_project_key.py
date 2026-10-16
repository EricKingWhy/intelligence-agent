"""`read_session_project_key` 的同步只读读取（#1191 §三.3，重审 finding F1）。

供给器（`assembly._build_scoped_fact_provider`）是 sync 闭包，拿不到异步
`SessionMetaStore`，故需要一条同步只读通道。本文件钉住它的三个契约：
① 正常路径按 session_id 取到 project_key；② **路径含 `?`/`%`/`#` 时仍取到**
（裸拼进 `file:` URI 会让 `mode=ro` 失效并静默回落——F1 回归点）；
③ 库 / 表 / 行缺失一律 `None`（读取侧按 cwd 兜底，绝不抛）。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from agent_harness.storage.sqlite import read_session_project_key

_DDL = """
CREATE TABLE session_meta (
    session_id  TEXT PRIMARY KEY,
    project_key TEXT
)
"""


def _make_db(path: Path, *, session_id: str = "s1", project_key: str = "/repo/x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.execute(_DDL)
    connection.execute(
        "INSERT INTO session_meta VALUES (?, ?)", (session_id, project_key)
    )
    connection.commit()
    connection.close()
    return path


def test_reads_project_key_for_normal_path(tmp_path: Path) -> None:
    db = _make_db(tmp_path / "harness.db")
    assert read_session_project_key(db, "s1") == "/repo/x"


def test_reads_project_key_when_path_contains_uri_specials(tmp_path: Path) -> None:
    # `?` / `%` / `#` 裸拼进 file: URI 会被解析吃掉 → mode=ro 失效 → OperationalError
    # → 静默 None → 供给器回落到 live cwd，重新制造面板/注入分组漂移（F1）。
    db = _make_db(tmp_path / "a?b%c#d" / "harness.db")
    assert read_session_project_key(db, "s1") == "/repo/x"


def test_missing_row_returns_none(tmp_path: Path) -> None:
    db = _make_db(tmp_path / "harness.db")
    assert read_session_project_key(db, "absent") is None


def test_missing_table_returns_none(tmp_path: Path) -> None:
    db = tmp_path / "harness.db"
    sqlite3.connect(db).close()  # 空库：无 session_meta 表
    assert read_session_project_key(db, "s1") is None


def test_missing_file_returns_none(tmp_path: Path) -> None:
    assert read_session_project_key(tmp_path / "gone.db", "s1") is None
