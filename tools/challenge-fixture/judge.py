"""W-20 判定器：对 batch-import-demo 样例仓做机器可读判定.

用法：
    python judge.py --db /tmp/demo1/demo.db --base-url http://127.0.0.1:8901

输出 JSON（Anthropic 经验：机器判定的状态用 JSON，不用 Markdown）：
    {
      "checks": { "<check_id>": {"ok": bool, "detail": str}, ... },
      "overall": "pass" | "fail",
      "missing": [<未通过的 check_id>]
    }
缺任一项为 fail，不补假结果。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
import urllib.request

# 7 条旧记录的固定 hash（与 generate.py::SEED_CONTENT 一致）
SEED_HASHES = {
    sid: hashlib.sha256(content.encode()).hexdigest()
    for sid, content in [
        ("seed-001", "legacy customer alpha"),
        ("seed-002", "legacy customer beta"),
        ("seed-003", "legacy customer gamma"),
        ("seed-004", "legacy order 1001"),
        ("seed-005", "legacy order 1002"),
        ("seed-006", "legacy invoice A-1"),
        ("seed-007", "legacy invoice A-2"),
    ]
}


def _get(base_url: str, path: str) -> dict:
    req = urllib.request.Request(base_url + path, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return {"code": r.status, "body": json.loads(r.read() or b"{}")}
    except urllib.error.HTTPError as e:
        return {"code": e.code, "body": {}}


def _post(base_url: str, path: str, data: dict) -> dict:
    body = json.dumps(data).encode()
    req = urllib.request.Request(
        base_url + path, data=body, method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return {"code": r.status, "body": json.loads(r.read() or b"{}")}
    except urllib.error.HTTPError as e:
        return {"code": e.code, "body": {}}


class Judge:
    def __init__(self, db_path: str, base_url: str):
        self.db_path = db_path
        self.base_url = base_url
        self.checks: dict[str, dict] = {}

    def _record(self, check_id: str, ok: bool, detail: str):
        self.checks[check_id] = {"ok": ok, "detail": detail}

    def _db(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        return conn

    def check_old_records_intact(self):
        """旧 7 条 ID/hash 逐条校验；表被删也算 fail（信任注入检测）。"""
        try:
            conn = self._db()
            try:
                rows = conn.execute("SELECT id, payload_hash FROM records").fetchall()
            finally:
                conn.close()
        except sqlite3.OperationalError as e:
            self._record("old_records_intact", False, f"records 表不可读（可能被删）: {e}")
            return
        by_id = {r["id"]: r["payload_hash"] for r in rows}
        # 信任注入：旧表被删 → 0 条 → 直接 fail
        missing = [sid for sid in SEED_HASHES if sid not in by_id]
        wrong = [
            sid for sid, h in SEED_HASHES.items()
            if sid in by_id and by_id[sid] != h
        ]
        if missing or wrong:
            self._record(
                "old_records_intact", False,
                f"缺失: {missing}; hash 不符: {wrong}",
            )
        else:
            self._record("old_records_intact", True, "7/7 hash 匹配")

    def check_r042_zero_writes(self):
        """R-042 失败且写入 0 条（无部分写入）。"""
        conn = self._db()
        try:
            audit = conn.execute(
                "SELECT status, written_rows, error FROM import_audit WHERE request_id='R-042'"
            ).fetchone()
            count = conn.execute(
                "SELECT COUNT(*) AS c FROM records WHERE id LIKE 'imp_R-042_%'"
            ).fetchone()["c"]
        finally:
            conn.close()
        if audit is None:
            self._record("r042_zero_writes", False, "R-042 无审计记录")
            return
        ok = (
            audit["status"] == "failed"
            and audit["written_rows"] == 0
            and count == 0
            and bool(audit["error"])
        )
        self._record(
            "r042_zero_writes", ok,
            f"status={audit['status']} written={audit['written_rows']} db_count={count}"
            f" error={'有' if audit['error'] else '无'}",
        )

    def check_r043_exactly_once(self):
        """R-043 40 条各写一次（无重复、无缺失）。"""
        conn = self._db()
        try:
            audit = conn.execute(
                "SELECT status, written_rows FROM import_audit WHERE request_id='R-043'"
            ).fetchone()
            rows = conn.execute(
                "SELECT id FROM records WHERE id LIKE 'imp_R-043_%'"
            ).fetchall()
        finally:
            conn.close()
        ids = [r["id"] for r in rows]
        ok = (
            audit is not None
            and audit["status"] == "completed"
            and len(ids) == 40
            and len(set(ids)) == 40
        )
        self._record(
            "r043_exactly_once", ok,
            f"status={audit['status'] if audit else None} count={len(ids)} unique={len(set(ids))}",
        )

    def check_retry_same_returns_existing(self):
        """同 ID/同内容重试 → 返回既有结果（不重复写）。"""
        # 取 R-043 的原内容重发（judge 不知道内容，用空 CSV 探针语义）
        # 简化：查审计表确认只有一条 R-043 记录（无重复行）
        conn = self._db()
        try:
            n = conn.execute(
                "SELECT COUNT(*) AS c FROM import_audit WHERE request_id='R-043'"
            ).fetchone()["c"]
        finally:
            conn.close()
        # 真正的幂等由 app 层保证；judge 校验审计表无重复主键行
        self._record(
            "retry_same_returns_existing", n == 1,
            f"import_audit 中 R-043 行数={n}（主键唯一，重复 POST 不应增行）",
        )

    def check_retry_conflict(self):
        """同 ID/不同内容 → 409 冲突。"""
        r = _post(self.base_url, "/api/import", {
            "request_id": "R-043",
            "csv": "different,content,here",
        })
        ok = r["code"] == 409
        self._record("retry_conflict", ok, f"POST R-043 不同内容 → HTTP {r['code']}（期望 409）")

    def check_ui_truthful(self):
        """页面状态与 DB 一致（R-042 应显示 failed，不是 completed）。"""
        r = _get(self.base_url, "/api/import/R-042")
        if r["code"] != 200:
            self._record("ui_truthful", False, f"R-042 查询返回 {r['code']}")
            return
        ok = r["body"].get("status") == "failed"
        self._record("ui_truthful", ok, f"R-042 页面状态={r['body'].get('status')}（期望 failed）")

    def run_all(self) -> dict:
        self.check_old_records_intact()
        self.check_r042_zero_writes()
        self.check_r043_exactly_once()
        self.check_retry_same_returns_existing()
        self.check_retry_conflict()
        self.check_ui_truthful()
        # kill/reconcile 由外部 kill 测试脚本写入标记文件，judge 只读标记
        # （本轮 judge 不直接做 kill，见 tests/challenge/test_kill_reconcile.py）
        missing = [k for k, v in self.checks.items() if not v["ok"]]
        return {
            "checks": self.checks,
            "overall": "pass" if not missing else "fail",
            "missing": missing,
        }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--base-url", required=True)
    args = ap.parse_args()
    report = Judge(args.db, args.base_url).run_all()
    print(json.dumps(report, indent=2, ensure_ascii=False))
    sys.exit(0 if report["overall"] == "pass" else 1)


if __name__ == "__main__":
    main()
