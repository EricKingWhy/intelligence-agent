"""W-20: 判定器测试 —— 故意给坏修复时判定器必须失败.

覆盖：重复写、误报成功、旧表删除三种坏修复。
"""
import hashlib
import os
import sqlite3
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../tools/challenge-fixture"))

import judge as judge_module
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


# ---------------------------------------------------------------------------
# #849：判定器必须**顺序无关** —— 验收夹具由判定器自己创建，不沿用被测 Agent 在
# 修复前留下的旧行。
#
# 现场（Run A 第 2 次，真实模型 glm-5.3-flash）：Agent 在修复完成前就用 R-042/R-043
# 复现过缺陷，旧代码把它们写成 `R-042 = completed / written=19`、`R-043 = 41 条`；
# 修复后的 app 按 B1 幂等语义**原样返回**这两条陈旧行 ⇒ 判定器读到的是修复前的旧
# 状态，判 fail（missing 恰为 r042_zero_writes / r043_exactly_once / ui_truthful），
# 而修复本身是对的。操作者只清掉这两个 id 的产物、app 一个字节不改，重跑即 6/6 过。
# ---------------------------------------------------------------------------

# 验收载荷，与操作者现场重发的那两条逐字节一致（独立来源：issue #849 正文引的
# `D:/w21-work/diagnose-judge-order.py`）。
_R042_LINES = [f"id{i:03d},name{i},v{i}" for i in range(1, 21)]
_R042_LINES[16] = ",emptyid,row17"  # 第 17 行 id 为空 ⇒ 必须 failed 且写入 0
R042_ACCEPTANCE_CSV = "\n".join(_R042_LINES)
R043_ACCEPTANCE_CSV = "\n".join(
    f"imp_R-043_row{i:03d},name{i},v{i}" for i in range(1, 41)
)


def _polluted_db(tmp_path) -> str:
    """模拟「Agent 在修复前就用验收 id 探测过」：陈旧的成功行 + 部分写入。"""
    d = generate(str(tmp_path / "demo"))
    db_path = os.path.join(d, "demo.db")
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO import_audit (request_id, content_hash, status,"
            " total_rows, written_rows, error)"
            " VALUES ('R-042', 'stale-pre-fix', 'completed', 20, 19, NULL)"
        )
        conn.execute(
            "INSERT INTO import_audit (request_id, content_hash, status,"
            " total_rows, written_rows) VALUES ('R-043', 'stale-pre-fix', 'completed', 40, 41)"
        )
        for i in range(1, 20):
            conn.execute(
                "INSERT INTO records (id, payload_hash, content) VALUES (?, 'h', 'c')",
                (f"imp_R-042_{i}",),
            )
        for i in range(1, 42):
            conn.execute(
                "INSERT INTO records (id, payload_hash, content) VALUES (?, 'h', 'c')",
                (f"imp_R-043_{i}",),
            )
        conn.commit()
    finally:
        conn.close()
    return db_path


class _AppStub:
    """修复后 app 的最小 HTTP 替身，语义取自现场跑通的那一份 `app.py`。

    - 新 request_id → 202 + pending（替身里同步跑完，省掉 worker 轮询）；
    - 既有 + 同 hash → **200 + 既有结果、不重跑**（B1 修好）；`idempotent=False`
      时改为重新入队（202 requeued，B1 未修），用来证明判定器没丢掉这条判据；
    - 既有 + 不同 hash → 409（B3 修好）；
    - R-042 第 17 行 id 为空 ⇒ 全量校验失败 ⇒ failed / written_rows=0 / 一条不写；
    - R-043 40 行合法 ⇒ completed / 40 条各写一次。
    """

    def __init__(self, db_path: str, *, idempotent: bool = True):
        self.db_path = db_path
        self.idempotent = idempotent
        self.posts: list[tuple[str, str]] = []

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        return conn

    def post(self, path: str, data: dict) -> dict:
        rid = data["request_id"]
        csv_text = data["csv"]
        self.posts.append((rid, csv_text))
        digest = hashlib.sha256(csv_text.encode()).hexdigest()
        conn = self._conn()
        try:
            existing = conn.execute(
                "SELECT content_hash, status FROM import_audit WHERE request_id=?",
                (rid,),
            ).fetchone()
            if existing is not None and existing["content_hash"] != digest:
                return {"code": 409, "body": {}}
            if existing is not None:
                if self.idempotent:
                    return {"code": 200, "body": {"status": existing["status"]}}
                return {"code": 202, "body": {"status": "requeued"}}
            conn.execute(
                "INSERT INTO import_audit (request_id, content_hash, status)"
                " VALUES (?, ?, 'pending')",
                (rid, digest),
            )
            conn.commit()
            self._write(conn, rid, csv_text)
            conn.commit()
        finally:
            conn.close()
        return {"code": 202, "body": {"status": "pending"}}

    def _write(self, conn: sqlite3.Connection, rid: str, csv_text: str) -> None:
        lines = [ln for ln in csv_text.strip().split("\n") if ln.strip()]
        rows = [ln.strip().split(",") for ln in lines]
        bad = next((i for i, p in enumerate(rows, start=1) if not p[0].strip()), None)
        if bad is not None:
            # 全量校验失败 ⇒ 一条不写、终态 failed（B2 修好的语义）
            conn.execute(
                "UPDATE import_audit SET status='failed', total_rows=?, written_rows=0,"
                " error=?, updated_at=datetime('now') WHERE request_id=?",
                (len(rows), f"row {bad}: empty id", rid),
            )
            return
        for i, row in enumerate(rows, start=1):
            content = ",".join(row)
            conn.execute(
                "INSERT OR IGNORE INTO records (id, payload_hash, content) VALUES (?, ?, ?)",
                (f"imp_{rid}_{i}", hashlib.sha256(content.encode()).hexdigest(), content),
            )
        conn.execute(
            "UPDATE import_audit SET status='completed', total_rows=?, written_rows=?,"
            " error=NULL, updated_at=datetime('now') WHERE request_id=?",
            (len(rows), len(rows), rid),
        )

    def get(self, path: str) -> dict:
        rid = path.rsplit("/", 1)[-1]
        conn = self._conn()
        try:
            row = conn.execute(
                "SELECT status FROM import_audit WHERE request_id=?", (rid,)
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            return {"code": 404, "body": {}}
        return {"code": 200, "body": {"status": row["status"]}}


def _with_app(monkeypatch, app: _AppStub) -> None:
    monkeypatch.setattr(judge_module, "_post", lambda base, path, data: app.post(path, data))
    monkeypatch.setattr(judge_module, "_get", lambda base, path: app.get(path))


def test_judge_is_order_independent_of_pre_fix_probing(tmp_path, monkeypatch):
    """修复前用验收 id 探测过（陈旧成功行）不得污染判定：修复正确 ⇒ 必须 pass。"""
    db_path = _polluted_db(tmp_path)
    app = _AppStub(db_path)
    _with_app(monkeypatch, app)

    report = Judge(db_path, "http://127.0.0.1:9").run_all()

    assert report["missing"] == [], report
    assert report["overall"] == "pass", report
    # 夹具是判定器自己驱动的，而不是沿用旧行
    assert [rid for rid, _ in app.posts][:2] == ["R-042", "R-043"], app.posts
    assert app.posts[0][1] == R042_ACCEPTANCE_CSV
    assert app.posts[1][1] == R043_ACCEPTANCE_CSV


def test_judge_still_catches_b1_after_owning_its_fixtures(tmp_path, monkeypatch):
    """夹具自持之后 B1 仍要被抓到：同内容重 POST 重新入队 ⇒ 必须 fail。"""
    db_path = _polluted_db(tmp_path)
    app = _AppStub(db_path, idempotent=False)
    _with_app(monkeypatch, app)

    report = Judge(db_path, "http://127.0.0.1:9").run_all()

    assert report["overall"] == "fail", report
    assert "retry_same_returns_existing" in report["missing"], report


def test_judge_acceptance_payloads_match_the_operator_brief():
    """判定器内置的验收载荷必须与操作者现场重发的那两条逐字节一致。"""
    assert judge_module.ACCEPTANCE_R042_CSV == R042_ACCEPTANCE_CSV
    assert judge_module.ACCEPTANCE_R043_CSV == R043_ACCEPTANCE_CSV
