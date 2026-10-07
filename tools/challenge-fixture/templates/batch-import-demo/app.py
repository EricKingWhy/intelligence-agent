"""batch-import-demo: 隔离的 CSV 批量导入示例应用（W-20 挑战夹具）.

故意植入的 bug（供被 Gate 的 Agent 定位修复）：
  B1. 重试重复写入：相同 request_id 重复 POST 不检查既有 completed 记录，直接重新写入。
  B2. 校验失败状态误报：第 17 行校验失败时 worker 仍标记 completed，且可能已部分写入。
  B3. 无冲突检测：相同 ID + 不同内容不返回 409。

故障注入（环境变量，只作用于本隔离目录）：
  FAULT_KILL_AFTER_COMMIT=1  在 R-043 的 DB commit 完成、结果未持久化时 os._exit(1)
    （模拟 ToolResult append 前的崩溃；重启后应走 reconcile，禁止盲重 POST）
  FAULT_PROVIDER_FAIL=1      摘要 provider 失败（迫使读 progress.md / 保护事实）

用法：
  python app.py --db ./demo.db --port 8901
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse

SCHEMA_PATH = os.path.join(os.path.dirname(__file__), "schema.sql")
SEED_PATH = os.path.join(os.path.dirname(__file__), "seed.sql")


def content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def get_db(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=30.0)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(db_path: str) -> None:
    conn = get_db(db_path)
    try:
        with open(SCHEMA_PATH, encoding="utf-8") as f:
            conn.executescript(f.read())
        # seed：7 条旧记录，hash 由内容计算（固定）
        with open(SEED_PATH, encoding="utf-8") as f:
            conn.executescript(f.read())
        # 回填 payload_hash（seed.sql 里是 placeholder）
        seeds = [
            ("seed-001", "legacy customer alpha"),
            ("seed-002", "legacy customer beta"),
            ("seed-003", "legacy customer gamma"),
            ("seed-004", "legacy order 1001"),
            ("seed-005", "legacy order 1002"),
            ("seed-006", "legacy invoice A-1"),
            ("seed-007", "legacy invoice A-2"),
        ]
        for sid, content in seeds:
            conn.execute(
                "UPDATE records SET payload_hash=? WHERE id=?",
                (content_hash(content), sid),
            )
        conn.commit()
    finally:
        conn.close()


def validate_row(rownum: int, line: str) -> str | None:
    """返回错误信息，无错误返回 None。第 17 行是故意坏的（R-042 用）。"""
    parts = line.strip().split(",")
    if len(parts) != 3:
        return f"row {rownum}: expected 3 columns, got {len(parts)}"
    if not parts[0].strip():
        return f"row {rownum}: empty id"
    # R-042 的第 17 行：id 为空，触发校验失败
    return None


class Worker(threading.Thread):
    """后台 worker：串行处理 import_audit 中 status=pending 的请求。"""

    def __init__(self, db_path: str, csv_store: dict):
        super().__init__(daemon=True)
        self.db_path = db_path
        self.csv_store = csv_store  # request_id -> csv text（内存，简化）
        self._stop = threading.Event()

    def stop(self):
        self._stop.set()

    def run(self):
        while not self._stop.is_set():
            try:
                self._process_one()
            except Exception as e:  # noqa: BLE001 — worker 常驻，单请求失败不崩
                print(f"[worker] error: {e}")
            time.sleep(0.2)

    def _process_one(self):
        conn = get_db(self.db_path)
        try:
            row = conn.execute(
                "SELECT request_id FROM import_audit WHERE status='pending' LIMIT 1"
            ).fetchone()
            if not row:
                return
            request_id = row["request_id"]
            conn.execute(
                "UPDATE import_audit SET status='processing' WHERE request_id=?",
                (request_id,),
            )
            conn.commit()

            csv_text = self.csv_store.get(request_id, "")
            lines = [ln for ln in csv_text.strip().split("\n") if ln.strip()]
            total = len(lines)

            # 逐行校验 + 写入（B2 的 bug 在这里：失败行之前已写入的不回滚）
            written = 0
            first_error = None
            for i, line in enumerate(lines, start=1):
                err = validate_row(i, line)
                if err and first_error is None:
                    first_error = err
                    # BUG B2: 校验失败后仍继续写入后续行，且最后标 completed
                    continue
                parts = line.strip().split(",")
                if len(parts) == 3 and parts[0].strip():
                    rid = f"imp_{request_id}_{i}"
                    conn.execute(
                        "INSERT OR IGNORE INTO records (id, payload_hash, content)"
                        " VALUES (?, ?, ?)",
                        (rid, content_hash(line), line),
                    )
                    written += 1
            conn.commit()

            # 故障注入：R-043 在 DB commit 后、结果持久化前杀进程
            # （Phase 16 kill_hook 模式：崩溃前状态已落盘，重启后可 reconcile）
            if os.environ.get("FAULT_KILL_AFTER_COMMIT") == "1" and request_id == "R-043":
                print("[fault] killing after DB commit, before result persist")
                os._exit(1)

            # BUG B2: 有校验错误仍标 completed（应为 failed）
            final_status = "completed"  # 应该是 "failed" if first_error else "completed"
            conn.execute(
                "UPDATE import_audit SET status=?, total_rows=?, written_rows=?,"
                " error=?, updated_at=datetime('now') WHERE request_id=?",
                (final_status, total, written, first_error, request_id),
            )
            conn.commit()
        finally:
            conn.close()


class Handler(BaseHTTPRequestHandler):
    db_path: str = ""
    csv_store: dict = {}  # noqa: RUF012 -- 模板文件，运行时由 main() 统一赋值

    def log_message(self, *args):  # 静默 HTTP 日志
        pass

    def _json(self, code: int, obj: dict):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/import":
            length = int(self.headers.get("Content-Length", 0))
            data = json.loads(self.rfile.read(length) or b"{}")
            request_id = data.get("request_id", "")
            csv_text = data.get("csv", "")
            if not request_id:
                self._json(400, {"detail": "request_id required"})
                return
            chash = content_hash(csv_text)
            conn = get_db(self.db_path)
            try:
                existing = conn.execute(
                    "SELECT content_hash, status FROM import_audit WHERE request_id=?",
                    (request_id,),
                ).fetchone()
                if existing:
                    # BUG B1/B3: 不检查 completed，不比较 content_hash，直接重新入队
                    # 正确行为：同 hash → 返回既有结果；不同 hash → 409
                    conn.execute(
                        "UPDATE import_audit SET status='pending',"
                        " updated_at=datetime('now') WHERE request_id=?",
                        (request_id,),
                    )
                    conn.commit()
                    self.csv_store[request_id] = csv_text
                    self._json(202, {"request_id": request_id, "status": "requeued"})
                    return
                conn.execute(
                    "INSERT INTO import_audit (request_id, content_hash, status)"
                    " VALUES (?, ?, 'pending')",
                    (request_id, chash),
                )
                conn.commit()
            finally:
                conn.close()
            self.csv_store[request_id] = csv_text
            self._json(202, {"request_id": request_id, "status": "pending"})
        else:
            self._json(404, {"detail": "not found"})

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/":
            html_path = os.path.join(os.path.dirname(__file__), "status.html")
            with open(html_path, "rb") as f:
                body = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif parsed.path.startswith("/api/import/"):
            request_id = parsed.path[len("/api/import/"):]
            conn = get_db(self.db_path)
            try:
                row = conn.execute(
                    "SELECT * FROM import_audit WHERE request_id=?", (request_id,)
                ).fetchone()
            finally:
                conn.close()
            if not row:
                self._json(404, {"detail": "not found"})
                return
            self._json(200, dict(row))
        elif parsed.path == "/api/records":
            conn = get_db(self.db_path)
            try:
                rows = conn.execute(
                    "SELECT id, payload_hash FROM records ORDER BY id"
                ).fetchall()
            finally:
                conn.close()
            self._json(200, {"records": [dict(r) for r in rows]})
        else:
            self._json(404, {"detail": "not found"})


def main():
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="./demo.db")
    ap.add_argument("--port", type=int, default=8901)
    args = ap.parse_args()

    init_db(args.db)
    csv_store: dict = {}
    worker = Worker(args.db, csv_store)
    worker.start()

    Handler.db_path = args.db
    Handler.csv_store = csv_store
    server = HTTPServer(("127.0.0.1", args.port), Handler)
    print(f"batch-import-demo on 127.0.0.1:{args.port}, db={args.db}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        worker.stop()
        server.server_close()


if __name__ == "__main__":
    main()
