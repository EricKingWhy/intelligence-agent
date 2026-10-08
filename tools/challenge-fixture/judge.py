"""W-20 判定器：对 batch-import-demo 样例仓做机器可读判定.

用法：
    python judge.py --db /tmp/demo1/demo.db --base-url http://127.0.0.1:8901

判定器**自己创建验收夹具**（#849）：先清掉 R-042/R-043 的非 seed 产物，再自己
POST 两条验收载荷并轮询到终态，然后才断言 —— 判定结果只取决于「当前代码是否修对」，
与被测 Agent 在修复前是否用这两个 id 复现过无关（seed 7 条一律不碰）。

输出 JSON（Anthropic 经验：机器判定的状态用 JSON，不用 Markdown）：
    {
      "checks": { "<check_id>": {"ok": bool, "detail": str}, ... },
      "acceptance": {"deleted_records": int, "deleted_audit_rows": int,
                     "terminal": {"R-042": str, "R-043": str}},
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
import time
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

# 验收载荷（判定器自带，不依赖被测 Agent 先跑过；#849）。
# 与操作者现场重发的那两条逐字节一致：R-042 = 20 行、第 17 行 id 为空；
# R-043 = 40 行全合法（记录 id 形如 imp_R-043_rowNNN）。
_ACCEPTANCE_R042_LINES = [f"id{i:03d},name{i},v{i}" for i in range(1, 21)]
_ACCEPTANCE_R042_LINES[16] = ",emptyid,row17"
ACCEPTANCE_R042_CSV = "\n".join(_ACCEPTANCE_R042_LINES)
ACCEPTANCE_R043_CSV = "\n".join(
    f"imp_R-043_row{i:03d},name{i},v{i}" for i in range(1, 41)
)
ACCEPTANCE_REQUEST_IDS = ("R-042", "R-043")


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

    # ---- 验收夹具自持（#849）：判定结果不得取决于被测 Agent 的探索顺序 ----

    def reset_acceptance_artifacts(self) -> dict:
        """清掉 R-042/R-043 的**非 seed 产物**（可能是修复前旧代码留下的陈旧行）。

        只删 `imp_R-042_%` / `imp_R-043_%` 记录与这两条审计行；7 条 seed 一律不碰
        （旧表是否被删仍由 check_old_records_intact 负责）。

        不做这一步的话：Agent 在修复完成前用这两个 id 复现过缺陷（很自然的做法），
        旧代码写下的 `R-042 = completed / written=19`、`R-043 = 41 条` 会被修复后
        app 的 B1 幂等分支**原样返回**，判定器读到的就是修复前的旧状态 ⇒ 一份正确
        的修复被判 fail。判定器测的必须是「修复是否正确」，不是「有没有先碰过它」。
        """
        conn = self._db()
        try:
            deleted_records = conn.execute(
                "DELETE FROM records WHERE id LIKE 'imp_R-042_%' OR id LIKE 'imp_R-043_%'"
            ).rowcount
            deleted_audit = conn.execute(
                "DELETE FROM import_audit WHERE request_id IN ('R-042', 'R-043')"
            ).rowcount
            conn.commit()
        finally:
            conn.close()
        return {"deleted_records": deleted_records, "deleted_audit_rows": deleted_audit}

    def drive_acceptance(self, timeout_s: float = 30.0) -> dict:
        """判定器**自己** POST 两条验收载荷并轮询到终态，返回 {request_id: 终态}。

        这样断言读到的必然是「当前代码」产出的状态，与 Agent 何时复现过无关。
        轮询超时留 "timeout"（后续断言据此 fail，不补假结果）。
        """
        terminal: dict[str, str] = {}
        for request_id, csv_text in (
            ("R-042", ACCEPTANCE_R042_CSV),
            ("R-043", ACCEPTANCE_R043_CSV),
        ):
            _post(self.base_url, "/api/import",
                  {"request_id": request_id, "csv": csv_text})
            deadline = time.monotonic() + timeout_s
            status = "timeout"
            while time.monotonic() < deadline:
                r = _get(self.base_url, f"/api/import/{request_id}")
                if r["code"] == 200 and r["body"].get("status") in ("completed", "failed"):
                    status = str(r["body"]["status"])
                    break
                time.sleep(0.2)
            terminal[request_id] = status
        return terminal

    def _r043_row_count(self) -> int:
        conn = self._db()
        try:
            return conn.execute(
                "SELECT COUNT(*) AS c FROM records WHERE id LIKE 'imp_R-043_%'"
            ).fetchone()["c"]
        finally:
            conn.close()

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
        """同 ID/同内容重试 → **返回既有结果**、不重新入队（B1 的判据）。

        #849：旧实现只数 `import_audit` 里 R-043 的行数，而 request_id 是主键，
        该数在「判定器自带夹具」之后恒为 1 ⇒ 等于放掉 B1。改成真判据：用同一份
        验收载荷重 POST，要求走「既有结果」分支（200）而不是重新入队（未修时是
        202 requeued），且记录条数不变。参照现场跑通的那份 app.py：既有 + 同 hash
        → 200 + 既有 status，不重跑。
        """
        before = self._r043_row_count()
        r = _post(self.base_url, "/api/import",
                  {"request_id": "R-043", "csv": ACCEPTANCE_R043_CSV})
        after = self._r043_row_count()
        ok = r["code"] == 200 and after == before == 40
        self._record(
            "retry_same_returns_existing", ok,
            f"同内容重 POST → HTTP {r['code']}（期望 200 既有结果；未修时为 202 requeued）；"
            f"记录条数 {before}→{after}",
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
        # 先清陈旧产物、再由判定器自己驱动两条验收载荷（#849）——顺序无关
        acceptance = self.reset_acceptance_artifacts()
        acceptance["terminal"] = self.drive_acceptance()
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
            "acceptance": acceptance,
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
