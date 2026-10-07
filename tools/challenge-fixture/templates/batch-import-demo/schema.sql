-- batch-import-demo schema
-- 旧记录表：7 条 seed 数据，ID 和 hash 固定，挑战期间不得删除/改写
CREATE TABLE IF NOT EXISTS records (
    id TEXT PRIMARY KEY,
    payload_hash TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 导入审计表：每次导入请求的幂等键与状态
-- request_id 是客户端幂等键；相同 ID + 相同内容 → 返回既有结果；
-- 相同 ID + 不同内容 → 409 冲突
CREATE TABLE IF NOT EXISTS import_audit (
    request_id TEXT PRIMARY KEY,
    content_hash TEXT NOT NULL,
    status TEXT NOT NULL,  -- pending | processing | completed | failed
    total_rows INTEGER NOT NULL DEFAULT 0,
    written_rows INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 导入写入的新记录（与旧 records 表是同一张表，用 id 前缀区分）
-- R-042/R-043 写入的记录 id 形如 imp_<request_id>_<rownum>
