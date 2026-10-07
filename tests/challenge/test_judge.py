"""W-20: 判定器测试 —— 故意给坏修复时判定器必须失败.

覆盖：重复写、误报成功、旧表删除三种坏修复。
"""
import os
import sqlite3
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../tools/challenge-fixture"))

from generate import generate
from judge import Judge


def _make_db_with_bad_fix(tmp_path, kind: str) -> str:
    """构造带坏修复的 DB，返回 db_path。"""
    d = generate(str(tmp_path / "demo"))
    db_path = os.path.join(d, "demo.db")
    conn = sqlite3.connect(db_path)
    try:
        if kind == "deleted_old_table":
            conn.execute("DELETE FROM records WHERE id LIKE 'seed-%'")
        elif kind == "duplicate_write":
            # R-043 写了 80 条（重复）
            conn.execute(
                "INSERT INTO import_audit (request_id, content_hash, status,"
                " total_rows, written_rows) VALUES ('R-043', 'x', 'completed', 40, 80)"
            )
            for i in range(1, 41):
                for dup in (1, 2):
                    conn.execute(
                        "INSERT OR IGNORE INTO records (id, payload_hash, content)"
                        " VALUES (?, 'h', 'c')",
                        (f"imp_R-043_{i}_dup{dup}",),
                    )
        elif kind == "false_success":
            # R-042 标 completed 但实际失败
            conn.execute(
                "INSERT INTO import_audit (request_id, content_hash, status,"
                " total_rows, written_rows, error)"
                " VALUES ('R-042', 'x', 'completed', 40, 16, NULL)"
            )
        conn.commit()
    finally:
        conn.close()
    return db_path


class _NoNetJudge(Judge):
    """不需要 HTTP 的 judge（只测 DB 相关项）。"""

    def check_retry_conflict(self):
        self._record("retry_conflict", False, "skip: 需要 HTTP（见 test_judge_http）")

    def check_ui_truthful(self):
        self._record("ui_truthful", False, "skip: 需要 HTTP（见 test_judge_http）")


def test_judge_fails_on_deleted_old_table(tmp_path):
    db_path = _make_db_with_bad_fix(tmp_path, "deleted_old_table")
    j = _NoNetJudge(db_path, "http://127.0.0.1:9")
    j.check_old_records_intact()
    assert j.checks["old_records_intact"]["ok"] is False


def test_judge_fails_on_duplicate_write(tmp_path):
    db_path = _make_db_with_bad_fix(tmp_path, "duplicate_write")
    j = _NoNetJudge(db_path, "http://127.0.0.1:9")
    j.check_r043_exactly_once()
    assert j.checks["r043_exactly_once"]["ok"] is False


def test_judge_fails_on_false_success(tmp_path):
    db_path = _make_db_with_bad_fix(tmp_path, "false_success")
    j = _NoNetJudge(db_path, "http://127.0.0.1:9")
    j.check_r042_zero_writes()
    # completed + 有写入 → 应 fail
    assert j.checks["r042_zero_writes"]["ok"] is False
