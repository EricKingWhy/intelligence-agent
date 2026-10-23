"""#1389 会话全文存档 + FTS5（移植 Hermes `b324b1f7` 的 state.db）。

两条存储职责分明（不变量 #22）：

* SessionEvent JSONL 仍是 **Session 真相**（可观测 / 恢复 / 回放）；
* 本模块的 SQLite 库是**派生检索索引**——全文消息 + 三套 FTS5，供 `session_search`
  回忆历史对话。它不写回事件流，也不改变事件 schema。

Hermes 行为语义逐项保留（来源标注见各处）：

* 三套 FTS 表：`messages_fts`（unicode61，tool 行取前 8192 字符投影）、
  `messages_fts_trigram`（trigram，排除 tool 行与 cron/subagent 会话）、
  `messages_fts_cjk`（`cjk_unicode61` 可加载 tokenizer，中文按二元组索引）。
  来源: Hermes b324b1f7 `hermes_state_common.py:750+`、`hermes_state_fts.py:46`。
* 每套 insert / delete / update 三个触发器；UPDATE 触发器写成
  `AFTER UPDATE OF content, tool_name, tool_calls, role`——compaction 归档翻转
  `active`/`compacted` 列**故意不触发**（I/O 优化 `#68858/#73639`）。
* `state_meta` 的 `fts_rebuild_high_water` / `_progress` 门控：新库无回填时两条
  marker 都缺席，`COALESCE(..., -1)` 让门控恒真。CJK 表用自己的一对 marker
  （`fts_cjk_rebuild_*`），与 Hermes 同。
* tool 索引投影 8192 字符（`FTS_TOOL_CONTENT_PREFIX_CHARS`），存储仍是全文；
  查询输入上限 2048 字符（`MAX_FTS5_QUERY_CHARS`）。
* `session_search` 四形态（DISCOVERY / SCROLL / READ / BROWSE）、hidden sources
  （Owlet 映射 subagent/fork）、demoted sources（cron 可搜但降权）、content caps
  （单消息 2000 / bookends 1200 / scroll 4000）、相对时间（7d/24h/2w）、压缩摘要
  前缀排除、lineage 去重。来源: Hermes `tools/session_search_tool.py`。

fail-open（与 Hermes 一致）：CJK tokenizer 加载失败时**摘掉 cjk 触发器**并标记
`fts_cjk_stale`，写路径继续可用，检索降级到 trigram / LIKE。
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import threading
import time
from collections.abc import Iterable
from contextlib import contextmanager
from datetime import UTC, datetime
from functools import wraps
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def _archive_operation(method):
    @wraps(method)
    def wrapped(self, *args, **kwargs):
        with self._operation():
            return method(self, *args, **kwargs)

    return wrapped

#: tool 结果常是数 MB 的机器载荷：基础 FTS 只索引每行的前 N 字符，
#: 存储（`messages.content`）仍是全文。来源: Hermes `hermes_state_common.py:315`。
FTS_TOOL_CONTENT_PREFIX_CHARS = 8_192

#: 用户输入进 MATCH 前的上限。来源: Hermes `hermes_state_common.py:328`。
MAX_FTS5_QUERY_CHARS = 2_048

#: 不进搜索的会话来源（Owlet 词汇表：委派子会话 / fork 分支）。
#: 来源: Hermes `tools/session_search_tool.py` 的 `_HIDDEN_SESSION_SOURCES`
#: （其 kanban/tool 在 Owlet 无对应物）。
HIDDEN_SESSION_SOURCES: tuple[str, ...] = ("subagent", "fork")

#: 可搜但降权的来源：定时任务的重复词汇会淹没用户会话（Hermes `#19434`）。
DEMOTED_SESSION_SOURCES: tuple[str, ...] = ("cron",)

#: 自动维护只 close 不删的 stale open 来源：这些会话由进程自己拥有生命周期，
#: 进程死掉时 `ended_at` 永远不写。Hermes 的原集合还含 cli/kanban/acp/api_server/
#: tool/recovered，本仓没有对应 producer；词汇表见 `SOURCE_*`。
#: 来源: Hermes `hermes_state.py:463`（`_AUTO_PRUNE_STALE_OPEN_SOURCES`）。
AUTO_PRUNE_STALE_OPEN_SOURCES: tuple[str, ...] = ("cron", "subagent", "fork")

#: 自动维护 VACUUM 的可回收页占比门槛（`freelist_count / page_count`）。
#: 低于它时整库重写比回收的 I/O 更贵。来源: Hermes `hermes_state_common.py:289`。
AUTO_VACUUM_MIN_FREELIST_RATIO = 0.25

#: 批量删除的 id 分块：cron 密集的库一次能删上万个会话（Hermes `_id_chunks`）。
_PRUNE_CHUNK = 500

#: READ 形态单条消息的字符上限（`#69334`：曾有单次 read 回 74K 字符）。
_READ_MAX_CONTENT = 2_000
#: discovery 的 bookends 上限。
_BOOKEND_MAX_CONTENT = 1_200
#: scroll 窗口单条上限。
_SCROLL_MAX_CONTENT = 4_000
#: FTS 行扫描上限（lineage 去重前）。
_DISCOVER_SCAN_LIMIT = 300

#: 压缩摘要前缀：不进 bookends（Hermes `session_search_tool.py:52`）。
_COMPACTION_PREFIXES = ("[CONTEXT COMPACTION", "[CONTEXT SUMMARY]:")

#: 明确结束上一段对话、且**不**把 transcript 带进后继的 end_reason 词表。
#: 来源: Hermes `hermes_state_common.py:181-182`（`_BOUNDARY_END_REASONS`）与
#: `tools/session_search_tool.py:206-212`（fresh-reset 集合由其派生）。
_FRESH_RESET_END_REASONS = frozenset({
    "session_reset", "session_switch", "idle", "daily", "suspended",
    "resume_pending_expired", "new_session",
})

#: LIKE 分词的 token 切法：引号短语整体作为一个单元（Hermes `hermes_state_search.py:42`）。
_LIKE_TOKEN_RE = re.compile(r'"[^"]+"|\S+')

_RELATIVE_BOUND_RE = re.compile(r"^(\d+)\s*(h|d|w)$", re.IGNORECASE)
_RELATIVE_UNIT_SECONDS = {"h": 3_600, "d": 86_400, "w": 604_800}

_CJK_RANGES = (
    (0x4E00, 0x9FFF), (0x3400, 0x4DBF), (0x20000, 0x2A6DF), (0x3000, 0x303F),
    (0x3040, 0x309F), (0x30A0, 0x30FF), (0xAC00, 0xD7AF),
)
_FTS5_SPECIAL_RE = re.compile(r"""[+{}():"^@/#&|~\[\]<>,;!?$=\\']""")
_QUOTED_PHRASE_RE = re.compile(r'"[^"]*"')
_FTS_OPERATORS = frozenset({"AND", "OR", "NOT"})

_BUSY_TIMEOUT_MS = 10_000


# --------------------------------------------------------------------- sql --


def _fts_indexed_content_sql(alias: str) -> str:
    """索引投影：tool 行只取前 N 字符，其它行取全文（存储不动）。"""
    return (
        f"CASE WHEN {alias}.role = 'tool' "
        f"THEN substr(COALESCE({alias}.content, ''), 1, {FTS_TOOL_CONTENT_PREFIX_CHARS}) "
        f"ELSE {alias}.content END"
    )


_NEW_INDEXED = _fts_indexed_content_sql("new")
_OLD_INDEXED = _fts_indexed_content_sql("old")

_TRIGRAM_SOURCE_PREDICATE = (
    "s.source NOT IN ('cron', 'subagent') "
    "AND json_extract(s.model_config, '$._delegate_from') IS NULL"
)

_HIGH_WATER = (
    "COALESCE((SELECT CAST(value AS INTEGER) FROM state_meta "
    "WHERE key = 'fts_rebuild_high_water'), -1)"
)
_PROGRESS = (
    "COALESCE((SELECT CAST(value AS INTEGER) FROM state_meta "
    "WHERE key = 'fts_rebuild_progress'), -1)"
)

_SCHEMA_SQL = """
-- 待裁决列（#1389 票面「全量建表、零裁剪」）：裁决前不许删，详见 issue #1389。
--   sessions：billing_* / estimated_cost_usd / actual_cost_usd / cost_status /
--             cost_source / pricing_version（计费与成本）、handoff_*（交接）、
--             compression_*（压缩恢复）、chat_id / chat_type / thread_id /
--             session_key / user_id / created_source / profile_name /
--             transport_profile / origin_json / expiry_finalized / display_name /
--             archived / auto_archived / pinned / hidden（平台与归档语义）。
--   messages：codex_*（模型侧 reasoning 载荷）、display_* / message_uid /
--             absorbed_message_uids / tool_call_uids / tool_call_uid /
--             platform_message_id（展示与去重）、effect_disposition / observed
--             （副作用对账）、api_content / _compressed_summary / active /
--             compacted（压缩与检索可见性）。
--   session_model_usage：billing_* / *_cost_usd / cost_status / cost_source（计费）。
CREATE TABLE IF NOT EXISTS system_prompts (
    hash TEXT PRIMARY KEY,
    prompt TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    created_source TEXT,
    user_id TEXT,
    session_key TEXT,
    chat_id TEXT,
    chat_type TEXT,
    thread_id TEXT,
    display_name TEXT,
    origin_json TEXT,
    expiry_finalized INTEGER DEFAULT 0,
    model TEXT,
    model_config TEXT,
    system_prompt TEXT,
    system_prompt_hash TEXT,
    parent_session_id TEXT,
    started_at REAL NOT NULL,
    ended_at REAL,
    end_reason TEXT,
    message_count INTEGER DEFAULT 0,
    tool_call_count INTEGER DEFAULT 0,
    input_tokens INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    cache_read_tokens INTEGER DEFAULT 0,
    cache_write_tokens INTEGER DEFAULT 0,
    reasoning_tokens INTEGER DEFAULT 0,
    cwd TEXT,
    git_branch TEXT,
    git_repo_root TEXT,
    git_metadata_generation INTEGER NOT NULL DEFAULT 0,
    billing_provider TEXT,
    billing_base_url TEXT,
    billing_mode TEXT,
    estimated_cost_usd REAL,
    actual_cost_usd REAL,
    cost_status TEXT,
    cost_source TEXT,
    pricing_version TEXT,
    title TEXT,
    title_source TEXT,
    last_activity_at REAL,
    last_activity_description TEXT,
    last_activity_provenance TEXT,
    api_call_count INTEGER DEFAULT 0,
    handoff_state TEXT,
    handoff_platform TEXT,
    handoff_error TEXT,
    compression_failure_cooldown_until REAL,
    compression_failure_error TEXT,
    compression_fallback_streak INTEGER NOT NULL DEFAULT 0,
    compression_ineffective_count INTEGER NOT NULL DEFAULT 0,
    compression_recovery_deadline REAL,
    compression_overload_streak INTEGER NOT NULL DEFAULT 0,
    profile_name TEXT,
    transport_profile TEXT,
    rewind_count INTEGER NOT NULL DEFAULT 0,
    archived INTEGER NOT NULL DEFAULT 0,
    auto_archived INTEGER NOT NULL DEFAULT 0,
    pinned INTEGER NOT NULL DEFAULT 0,
    hidden INTEGER NOT NULL DEFAULT 0,
    last_read_at REAL,
    tool_names TEXT,
    FOREIGN KEY (parent_session_id) REFERENCES sessions(id),
    FOREIGN KEY (system_prompt_hash) REFERENCES system_prompts(hash)
);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    role TEXT NOT NULL,
    content TEXT,
    tool_call_id TEXT,
    tool_calls TEXT,
    tool_name TEXT,
    effect_disposition TEXT,
    timestamp REAL NOT NULL,
    token_count INTEGER,
    finish_reason TEXT,
    reasoning TEXT,
    reasoning_content TEXT,
    reasoning_details TEXT,
    codex_reasoning_items TEXT,
    codex_message_items TEXT,
    platform_message_id TEXT,
    observed INTEGER DEFAULT 0,
    _compressed_summary INTEGER NOT NULL DEFAULT 0,
    active INTEGER NOT NULL DEFAULT 1,
    compacted INTEGER NOT NULL DEFAULT 0,
    api_content TEXT,
    display_kind TEXT,
    display_metadata TEXT,
    display_identity BLOB,
    display_order INTEGER,
    message_uid TEXT,
    absorbed_message_uids TEXT,
    tool_call_uids TEXT,
    tool_call_uid TEXT
);

CREATE TABLE IF NOT EXISTS session_model_usage (
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    model TEXT NOT NULL,
    billing_provider TEXT NOT NULL DEFAULT '',
    billing_base_url TEXT NOT NULL DEFAULT '',
    billing_mode TEXT NOT NULL DEFAULT '',
    task TEXT NOT NULL DEFAULT '',
    api_call_count INTEGER NOT NULL DEFAULT 0,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens INTEGER NOT NULL DEFAULT 0,
    cache_write_tokens INTEGER NOT NULL DEFAULT 0,
    reasoning_tokens INTEGER NOT NULL DEFAULT 0,
    estimated_cost_usd REAL NOT NULL DEFAULT 0,
    actual_cost_usd REAL NOT NULL DEFAULT 0,
    cost_status TEXT,
    cost_source TEXT,
    first_seen REAL,
    last_seen REAL,
    PRIMARY KEY (session_id, model, billing_provider, billing_base_url, billing_mode, task)
);

CREATE TABLE IF NOT EXISTS state_meta (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, id);
CREATE INDEX IF NOT EXISTS idx_sessions_started ON sessions(started_at DESC);
"""

_BASE_FTS_SQL = f"""
CREATE VIEW IF NOT EXISTS messages_fts_src AS
    SELECT id,
           CASE WHEN role = 'tool'
                THEN substr(COALESCE(content, ''), 1, {FTS_TOOL_CONTENT_PREFIX_CHARS})
                ELSE content END AS content,
           tool_name, tool_calls
    FROM messages;

CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
    content, tool_name, tool_calls,
    content='messages_fts_src', content_rowid='id'
);

CREATE TRIGGER IF NOT EXISTS messages_fts_insert AFTER INSERT ON messages
WHEN (new.id > {_HIGH_WATER} OR new.id <= {_PROGRESS})
BEGIN
    INSERT INTO messages_fts(rowid, content, tool_name, tool_calls)
    VALUES (new.id, {_NEW_INDEXED}, new.tool_name, new.tool_calls);
END;

CREATE TRIGGER IF NOT EXISTS messages_fts_delete AFTER DELETE ON messages
WHEN (old.id > {_HIGH_WATER} OR old.id <= {_PROGRESS})
BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, content, tool_name, tool_calls)
    VALUES ('delete', old.id, {_OLD_INDEXED}, old.tool_name, old.tool_calls);
END;

CREATE TRIGGER IF NOT EXISTS messages_fts_update
AFTER UPDATE OF content, tool_name, tool_calls, role ON messages
WHEN (old.content IS NOT new.content
    OR old.tool_name IS NOT new.tool_name
    OR old.tool_calls IS NOT new.tool_calls
    OR old.role IS NOT new.role)
   AND (old.id > {_HIGH_WATER} OR old.id <= {_PROGRESS})
BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, content, tool_name, tool_calls)
    VALUES ('delete', old.id, {_OLD_INDEXED}, old.tool_name, old.tool_calls);
    INSERT INTO messages_fts(rowid, content, tool_name, tool_calls)
    VALUES (new.id, {_NEW_INDEXED}, new.tool_name, new.tool_calls);
END;
"""

_TRIGRAM_FTS_SQL = f"""
CREATE VIEW IF NOT EXISTS messages_fts_trigram_src AS
    SELECT m.id, m.role, m.content, m.tool_name
    FROM messages AS m
    JOIN sessions AS s ON s.id = m.session_id
    WHERE m.role <> 'tool' AND {_TRIGRAM_SOURCE_PREDICATE};

CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts_trigram USING fts5(
    content, tool_name,
    content='messages_fts_trigram_src', content_rowid='id',
    tokenize='trigram'
);

CREATE TRIGGER IF NOT EXISTS messages_fts_trigram_insert AFTER INSERT ON messages
WHEN new.role <> 'tool'
   AND EXISTS (SELECT 1 FROM sessions AS s WHERE s.id = new.session_id AND {_TRIGRAM_SOURCE_PREDICATE})
   AND (new.id > {_HIGH_WATER} OR new.id <= {_PROGRESS})
BEGIN
    INSERT INTO messages_fts_trigram(rowid, content, tool_name)
    VALUES (new.id, new.content, new.tool_name);
END;

CREATE TRIGGER IF NOT EXISTS messages_fts_trigram_delete AFTER DELETE ON messages
WHEN old.role <> 'tool'
   AND EXISTS (SELECT 1 FROM sessions AS s WHERE s.id = old.session_id AND {_TRIGRAM_SOURCE_PREDICATE})
   AND (old.id > {_HIGH_WATER} OR old.id <= {_PROGRESS})
BEGIN
    INSERT INTO messages_fts_trigram(messages_fts_trigram, rowid, content, tool_name)
    VALUES ('delete', old.id, old.content, old.tool_name);
END;

CREATE TRIGGER IF NOT EXISTS messages_fts_trigram_update
AFTER UPDATE OF content, tool_name, role ON messages
WHEN (old.content IS NOT new.content
    OR old.tool_name IS NOT new.tool_name
    OR old.role IS NOT new.role)
   AND (old.id > {_HIGH_WATER} OR old.id <= {_PROGRESS})
BEGIN
    INSERT INTO messages_fts_trigram(messages_fts_trigram, rowid, content, tool_name)
    SELECT 'delete', old.id, old.content, old.tool_name
    WHERE old.role <> 'tool'
      AND EXISTS (SELECT 1 FROM sessions AS s WHERE s.id = old.session_id AND {_TRIGRAM_SOURCE_PREDICATE});
    INSERT INTO messages_fts_trigram(rowid, content, tool_name)
    SELECT new.id, new.content, new.tool_name
    WHERE new.role <> 'tool'
      AND EXISTS (SELECT 1 FROM sessions AS s WHERE s.id = new.session_id AND {_TRIGRAM_SOURCE_PREDICATE});
END;
"""

_CJK_TABLE_SQL = """
CREATE VIEW IF NOT EXISTS messages_fts_cjk_src AS
    SELECT id, role, content, tool_name, tool_calls
    FROM messages
    WHERE role <> 'tool';

CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts_cjk USING fts5(
    content, tool_name, tool_calls,
    content='messages_fts_cjk_src', content_rowid='id',
    tokenize='cjk_unicode61'
);
"""

_CJK_TRIGGER_SQL = """
CREATE TRIGGER IF NOT EXISTS messages_fts_cjk_insert AFTER INSERT ON messages
WHEN new.role <> 'tool'
   AND (new.id > COALESCE((SELECT CAST(value AS INTEGER) FROM state_meta
                           WHERE key = 'fts_cjk_rebuild_high_water'), -1)
     OR new.id <= COALESCE((SELECT CAST(value AS INTEGER) FROM state_meta
                            WHERE key = 'fts_cjk_rebuild_progress'), -1))
BEGIN
    INSERT INTO messages_fts_cjk(rowid, content, tool_name, tool_calls)
    VALUES (new.id, new.content, new.tool_name, new.tool_calls);
END;

CREATE TRIGGER IF NOT EXISTS messages_fts_cjk_delete AFTER DELETE ON messages
WHEN old.role <> 'tool'
   AND (old.id > COALESCE((SELECT CAST(value AS INTEGER) FROM state_meta
                           WHERE key = 'fts_cjk_rebuild_high_water'), -1)
     OR old.id <= COALESCE((SELECT CAST(value AS INTEGER) FROM state_meta
                            WHERE key = 'fts_cjk_rebuild_progress'), -1))
BEGIN
    INSERT INTO messages_fts_cjk(messages_fts_cjk, rowid, content, tool_name, tool_calls)
    VALUES ('delete', old.id, old.content, old.tool_name, old.tool_calls);
END;

CREATE TRIGGER IF NOT EXISTS messages_fts_cjk_update
AFTER UPDATE OF content, tool_name, tool_calls, role ON messages
WHEN (old.content IS NOT new.content
    OR old.tool_name IS NOT new.tool_name
    OR old.tool_calls IS NOT new.tool_calls
    OR old.role IS NOT new.role)
   AND (old.id > COALESCE((SELECT CAST(value AS INTEGER) FROM state_meta
                           WHERE key = 'fts_cjk_rebuild_high_water'), -1)
     OR old.id <= COALESCE((SELECT CAST(value AS INTEGER) FROM state_meta
                            WHERE key = 'fts_cjk_rebuild_progress'), -1))
BEGIN
    INSERT INTO messages_fts_cjk(messages_fts_cjk, rowid, content, tool_name, tool_calls)
    SELECT 'delete', old.id, old.content, old.tool_name, old.tool_calls
    WHERE old.role <> 'tool';
    INSERT INTO messages_fts_cjk(rowid, content, tool_name, tool_calls)
    SELECT new.id, new.content, new.tool_name, new.tool_calls
    WHERE new.role <> 'tool';
END;
"""

_CJK_TRIGGERS = ("messages_fts_cjk_insert", "messages_fts_cjk_delete", "messages_fts_cjk_update")
_FTS_CJK_STALE_KEY = "fts_cjk_stale"

#: ANSI 转义（终端输出里很常见）。Hermes 在 `tools/ansi_strip.py`，本票收敛在
#: 本模块内（协调员 ACK gap3，不新增独立模块）。
_ANSI_RE = re.compile(
    r"""
    \x1B
    (?:
        \[[0-?]*[ -/]*[@-~]        # CSI
      | \][^\x07\x1B]*(?:\x07|\x1B\\)  # OSC
      | [PX^_][^\x1B]*\x1B\\       # DCS/SOS/PM/APC strings
      | [@-Z\\-_]                  # 2-char escapes
    )
    """,
    re.VERBOSE,
)


def strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def _is_cjk(cp: int) -> bool:
    return any(lo <= cp <= hi for lo, hi in _CJK_RANGES)


def _contains_cjk(text: str) -> bool:
    return any(_is_cjk(ord(ch)) for ch in text)


def _has_lone_cjk_run(text: str) -> bool:
    run = 0
    for ch in text:
        if _is_cjk(ord(ch)):
            run += 1
        else:
            if run == 1:
                return True
            run = 0
    return run == 1


def _non_operator_tokens(raw: str) -> list[str]:
    return [t for t in raw.split() if t.upper() not in _FTS_OPERATORS]


def _quote_fts_tokens(raw: str) -> str:
    return " ".join(
        tok if tok.upper() in _FTS_OPERATORS else '"' + tok.replace('"', '""') + '"'
        for tok in raw.split()
    )


def _sanitize_fts5_query(query: str) -> str:
    """清理 FTS5 MATCH 输入（特殊字符会直接抛语法错）。

    来源: Hermes `hermes_state_search.py:_sanitize_fts5_query`（截断上限、保留成对
    引号短语、剥离未配对特殊字符、点/横线/下划线词整体加引号）。
    """
    query = query[:MAX_FTS5_QUERY_CHARS]
    held: list[str] = []

    def _hold(m: re.Match[str]) -> str:
        held.append(m.group(0))
        return f"\x00Q{len(held) - 1}\x00"

    sanitized = _QUOTED_PHRASE_RE.sub(_hold, query).replace('"', " ")
    sanitized = _FTS5_SPECIAL_RE.sub(" ", sanitized)
    if "%" in sanitized and not _contains_cjk(sanitized):
        sanitized = sanitized.replace("%", " ")
    sanitized = re.sub(r"\*+", "*", sanitized)
    sanitized = re.sub(r"(^|\s)\*", r"\1", sanitized)
    sanitized = re.sub(r"(?i)^(AND|OR|NOT)\b\s*", "", sanitized.strip())
    sanitized = re.sub(r"(?i)\s+(AND|OR|NOT)\s*$", "", sanitized.strip())
    sanitized = re.sub(r"\b(\w+(?:[._-]\w+)+)\b", r'"\1"', sanitized)
    for i, quoted in enumerate(held):
        sanitized = sanitized.replace(f"\x00Q{i}\x00", quoted)
    return sanitized.strip()


def _escape_like(term: str) -> str:
    """转义 LIKE 通配符（配合 `ESCAPE '\\'`）。"""
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _or_relaxed_query(query: str) -> str | None:
    """把隐式 AND 查询改写成 any-term OR；不适用时返回 None。

    少于两个可搜单元（单词无法放宽）或出现显式 `OR`/`NOT`（调用方表达了精确语义）
    时返回 None。引号短语保持整体：`"docker networking" tls` → `"docker networking" OR tls`。
    来源: Hermes `hermes_state_search.py:876-888`。
    """
    units: list[str] = []
    for raw_token in _LIKE_TOKEN_RE.findall(query):
        upper = raw_token.upper()
        if upper in {"OR", "NOT"}:
            return None
        if upper != "AND":
            units.append(raw_token)
    return " OR ".join(units) if len(units) >= 2 else None


def _parse_time_bound(value: str | None) -> float | None:
    """ISO 日期/时间或相对时长（`7d`/`24h`/`2w`）→ UTC 时间戳。"""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    match = _RELATIVE_BOUND_RE.match(text)
    if match:
        return time.time() - int(match.group(1)) * _RELATIVE_UNIT_SECONDS[match.group(2).lower()]

    normalized = text[:-1] + "+00:00" if text.endswith("Z") else text
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.timestamp()


def _shape_message(row: dict[str, Any], *, anchor_id: int | None = None,
                   max_content_len: int | None = None) -> dict[str, Any]:
    """裁剪成对外形态；`content` 即使为空也保留（tool-call-only 轮）。"""
    content = row.get("content")
    if isinstance(content, str) and "\x1b" in content:
        content = strip_ansi(content)
    entry: dict[str, Any] = {
        "id": row.get("id"),
        "role": row.get("role"),
        "content": content,
        "timestamp": row.get("timestamp"),
    }
    for key in ("tool_name", "tool_calls", "tool_call_id"):
        if row.get(key):
            entry[key] = row[key]
    if anchor_id is not None and row.get("id") == anchor_id:
        entry["anchor"] = True
    if max_content_len and content and len(content) > max_content_len:
        entry.update(
            content=content[:max_content_len] + "…",
            content_truncated=True,
            original_content_chars=len(content),
        )
    return entry


def _is_compaction_summary(content: str | None) -> bool:
    return bool(content) and str(content).lstrip().startswith(_COMPACTION_PREFIXES)


def _session_activity_sql(alias: str = "s") -> str:
    """会话 recency = freshest of `last_activity_at` / 最新消息 timestamp / `started_at`。

    来源: Hermes `hermes_state_common.py:256-271`（`_sql_session_last_active`）。
    差异（本仓无该数据来源）：不抄 Hermes 的 coerce-window 垃圾值防御——那边的
    `messages.timestamp` 可能从损坏页里捞回 TEXT / 异常 double，本仓两列只由本模块
    以 REAL 写入，不存在窗口外取值。
    """
    return (
        "COALESCE((SELECT MAX(_act.v) FROM ("
        f"SELECT {alias}.last_activity_at AS v UNION ALL "
        f"SELECT (SELECT MAX(_act_m.timestamp) FROM messages AS _act_m "
        f"WHERE _act_m.session_id = {alias}.id)"
        f") AS _act), {alias}.started_at)"
    )


def _seconds_since(now: float, raw: str | None) -> float | None:
    """`state_meta` 时间戳的年龄；未记录或损坏 = 没有上次运行。
    来源: Hermes `hermes_state_maintenance.py:38`（`_seconds_since`）。"""
    try:
        return now - float(raw) if raw else None
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------- archive ---


class SessionArchive:
    """`<workspace_dir>/sessions/state.db` 的同步读写门面。

    连接按线程缓存（监听器可能在任意线程回调），WAL + busy_timeout 沿用
    `storage/sqlite.py` 的惯例。
    """

    def __init__(self, db_path: str | Path, *, cjk_so_path: str | Path | None = None) -> None:
        self.db_path = Path(db_path)
        self.cjk_so_path = (
            Path(cjk_so_path) if cjk_so_path is not None
            else Path(os.environ.get("OWLET_FTS5_CJK_SO", str(self.db_path.parent / "libfts5_cjk.so")))
        )
        self._local = threading.local()
        self._lifecycle = threading.Condition()
        self._connections: dict[threading.Thread, sqlite3.Connection] = {}
        self._active_by_thread: dict[threading.Thread, int] = {}
        self._active_operations = 0
        self._closing = False
        self._closed = False
        self._cjk_available = False

    @contextmanager
    def _operation(self):
        """Hold an operation lease while a method may use its thread connection."""
        owner = threading.current_thread()
        with self._lifecycle:
            if self._closed or (self._closing and owner not in self._active_by_thread):
                raise RuntimeError("SessionArchive is closed")
            self._active_operations += 1
            self._active_by_thread[owner] = self._active_by_thread.get(owner, 0) + 1
        try:
            yield
        finally:
            with self._lifecycle:
                self._active_operations -= 1
                active = self._active_by_thread[owner] - 1
                if active:
                    self._active_by_thread[owner] = active
                else:
                    del self._active_by_thread[owner]
                if self._closing and self._active_operations == 0:
                    self._finish_close_locked()
                self._lifecycle.notify_all()

    def _finish_close_locked(self) -> None:
        """Close every owned connection; caller holds ``_lifecycle``."""
        connections = tuple(self._connections.values())
        self._connections.clear()
        self._closed = True
        self._closing = False
        for conn in connections:
            try:
                conn.close()
            except sqlite3.Error:
                logger.debug("SessionArchive connection close failed", exc_info=True)
        self._local.conn = None

    # -- connection --------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        # The archive owns a connection registry and closes all registered
        # connections from its lifecycle owner.  sqlite3's default
        # ``check_same_thread`` would deadlock a foreign-thread close even when
        # no operation is in flight; operation leases still ensure a connection
        # is never used concurrently by two threads.
        conn = sqlite3.connect(
            self.db_path, timeout=_BUSY_TIMEOUT_MS / 1000, check_same_thread=False,
        )
        conn.row_factory = sqlite3.Row
        conn.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        # 每线程一条连接 ⇒ 扩展必须每条连接各加载一次（SQLite 按连接登记
        # tokenizer，不是按库）。失败留给 initialize() 走 fail-open。
        if self.cjk_so_path.exists():
            try:
                conn.enable_load_extension(True)
                try:
                    conn.load_extension(str(self.cjk_so_path))
                finally:
                    conn.enable_load_extension(False)
            except Exception:
                logger.debug("fts5_cjk 扩展加载失败: %s", self.cjk_so_path, exc_info=True)
        return conn

    @property
    def _conn(self) -> sqlite3.Connection:
        owner = threading.current_thread()
        with self._lifecycle:
            if self._closed or (self._closing and owner not in self._active_by_thread):
                raise RuntimeError("SessionArchive is closed")
            conn = getattr(self._local, "conn", None)
            if conn is None:
                conn = self._connect()
                self._local.conn = conn
                self._connections[owner] = conn
            return conn

    @contextmanager
    def _cursor(self):
        conn = self._conn
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    def close(self) -> None:
        owner = threading.current_thread()
        with self._lifecycle:
            if self._closed or self._closing:
                return
            self._closing = True
            if self._active_operations:
                if owner in self._active_by_thread:
                    return
                while self._active_operations:
                    self._lifecycle.wait()
            self._finish_close_locked()

    # -- schema ------------------------------------------------------------

    @_archive_operation
    def initialize(self) -> None:
        """建表 + 三套 FTS + 触发器（幂等）。"""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._cursor() as conn:
            conn.executescript(_SCHEMA_SQL)
            conn.executescript(_BASE_FTS_SQL)
            conn.executescript(_TRIGRAM_FTS_SQL)
        self._initialize_cjk()

    @_archive_operation
    def _initialize_cjk(self) -> None:
        """CJK 表与**它的**触发器分开建：索引不完整时触发器必须保持缺席。

        三分支语义照抄 Hermes `hermes_state_fts.py:243-300`；本仓无 `optimize-storage`
        命令，所以 Hermes 在那边做的「stale 索引整体重建 + 回填」收敛到本方法
        （`_reset_cjk_if_stale` / `_backfill_cjk`）。
        """
        available = self._load_cjk_extension()
        with self._cursor() as conn:
            if not available:
                self._degrade_cjk(conn)
                return
            # 缺口范围未知的 stale 索引只能从零重建（Hermes `_fts_cjk_reset_if_stale`）。
            self._reset_cjk_if_stale(conn)
            self._ensure_cjk_schema(conn)
            self._backfill_cjk(conn)

    def _degrade_cjk(self, conn: sqlite3.Connection) -> None:
        """fail-open：摘触发器，写路径继续可用；检索降级 trigram/LIKE。"""
        cjk_present = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'messages_fts_cjk'"
        ).fetchone()
        if cjk_present:
            live = [
                row[0] for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'trigger' "
                    f"AND name IN ({','.join('?' for _ in _CJK_TRIGGERS)})",
                    _CJK_TRIGGERS,
                ).fetchall()
            ]
            if live:
                # 先落 stale 标记再摘触发器：两步之间崩溃只会多留一个保守标记。
                conn.execute(
                    "INSERT INTO state_meta(key, value) VALUES(?, '1') "
                    "ON CONFLICT(key) DO UPDATE SET value='1'",
                    (_FTS_CJK_STALE_KEY,),
                )
                for name in live:
                    conn.execute(f"DROP TRIGGER IF EXISTS {name}")
                # debug 而非 warning/info：缺扩展是**预期内**的降级路径（没编
                # .so 的机器与全部测试环境都走这条）。任何 ≥INFO 的记录都会以
                # `system_log` 进 agent.jsonl，把「事件流里只有那五类事件」的
                # 既有断言打红（tests/test_structured_logging.py 全量实测）。
                # 降级状态仍可从 `cjk_available` 读到，不是隐形。
                # 例外：.so 存在但探测失败由 `_load_cjk_extension` 单独
                # warning（异常信号，见那里的注释），本句只管「压根没编 .so」
                # 的预期内降级。
                logger.debug(
                    "fts5_cjk tokenizer 不可用（%s）；CJK 索引降级为 trigram/LIKE",
                    self.cjk_so_path,
                )
        self._cjk_available = False

    def _reset_cjk_if_stale(self, conn: sqlite3.Connection) -> None:
        """stale 索引（触发器曾被摘掉，缺口范围未知）→ 表/视图/触发器全弃，从零重建。"""
        if conn.execute(
            "SELECT 1 FROM state_meta WHERE key = ?", (_FTS_CJK_STALE_KEY,)
        ).fetchone() is None:
            return
        for name in _CJK_TRIGGERS:
            conn.execute(f"DROP TRIGGER IF EXISTS {name}")
        conn.execute("DROP TABLE IF EXISTS messages_fts_cjk")
        conn.execute("DROP VIEW IF EXISTS messages_fts_cjk_src")
        conn.execute(
            "DELETE FROM state_meta WHERE key IN (?, 'fts_cjk_rebuild_high_water', "
            "'fts_cjk_rebuild_progress')",
            (_FTS_CJK_STALE_KEY,),
        )

    def _ensure_cjk_schema(self, conn: sqlite3.Connection) -> None:
        """Hermes `_ensure_fts_cjk_schema` 三分支（loaded+absent / loaded+present / stale）。

        loaded+absent：建表、删 stale、有数据则设回填标记对；
        loaded+present：stale 在则**不**重装触发器（缺口未知），否则建触发器；
        `cjk_available` = 无回填标记 pending。
        """
        cjk_present = bool(conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'messages_fts_cjk'"
        ).fetchone())
        conn.executescript(_CJK_TABLE_SQL)
        if not cjk_present:
            # 老 stale 标记指向的表已经不存在了。
            conn.execute("DELETE FROM state_meta WHERE key = ?", (_FTS_CJK_STALE_KEY,))
            # 空库天生完整；有数据的库靠 id 门控的标记对撑到回填完成。
            if conn.execute(
                "SELECT COUNT(*) FROM messages WHERE role <> 'tool'"
            ).fetchone()[0] > 0:
                high_water = conn.execute("SELECT COALESCE(MAX(id), 0) FROM messages").fetchone()[0]
                for key, value in (
                    ("fts_cjk_rebuild_high_water", str(high_water)),
                    ("fts_cjk_rebuild_progress", "0"),
                ):
                    conn.execute(
                        "INSERT INTO state_meta(key, value) VALUES(?, ?) "
                        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                        (key, value),
                    )
        if conn.execute(
            "SELECT 1 FROM state_meta WHERE key = ?", (_FTS_CJK_STALE_KEY,)
        ).fetchone():
            # 缺口范围未知：不重装触发器（Hermes 原语义）。
            self._cjk_available = False
            return
        conn.executescript(_CJK_TRIGGER_SQL)
        backfill_pending = conn.execute(
            "SELECT 1 FROM state_meta WHERE key = 'fts_cjk_rebuild_high_water' LIMIT 1"
        ).fetchone()
        self._cjk_available = backfill_pending is None

    def _backfill_cjk(self, conn: sqlite3.Connection) -> None:
        """把标记对覆盖的行补进 CJK 索引，完成后清标记、置可用。

        Hermes 把这一步放在 `optimize-storage` 里分块跑（让出写锁给在线网关）；
        本仓的回填只发生在启动时的一次 `initialize()`，整批一个事务更简单，也不会有
        半截索引——崩溃即整体回滚，下次打开从标记对重来。
        ponytail: 大库首次装扩展时这一次写入会占着写锁，需要在线让锁时再改分块提交。
        """
        row = conn.execute(
            "SELECT value FROM state_meta WHERE key = 'fts_cjk_rebuild_high_water'"
        ).fetchone()
        if row is None:
            return
        high_water = int(row[0])
        conn.execute(
            "INSERT INTO messages_fts_cjk(rowid, content, tool_name, tool_calls) "
            "SELECT id, content, tool_name, tool_calls FROM messages "
            "WHERE id <= ? AND role <> 'tool'",
            (high_water,),
        )
        # 边界清扫：高水位附近可能漏掉的行（Hermes `_BOUNDARY_SWEEP_SQL`）。
        conn.execute(
            "INSERT INTO messages_fts_cjk(rowid, content, tool_name, tool_calls) "
            "SELECT m.id, m.content, m.tool_name, m.tool_calls FROM messages AS m "
            "WHERE m.id > ? AND m.id <= ? AND m.role <> 'tool' "
            "AND NOT EXISTS (SELECT 1 FROM messages_fts_cjk_docsize AS d WHERE d.id = m.id)",
            (high_water - 1_000, high_water + 1_000),
        )
        conn.execute(
            "DELETE FROM state_meta WHERE key IN "
            "('fts_cjk_rebuild_high_water', 'fts_cjk_rebuild_progress')"
        )
        self._cjk_available = True

    @_archive_operation
    def _load_cjk_extension(self) -> bool:
        """探测本连接上 `cjk_unicode61` 是否可用（扩展在 `_connect` 已加载）。"""
        if not self.cjk_so_path.exists():
            return False
        try:
            self._conn.execute(
                "CREATE VIRTUAL TABLE temp.__cjk_probe USING fts5(x, tokenize='cjk_unicode61')"
            )
            self._conn.execute("DROP TABLE temp.__cjk_probe")
            return True
        except Exception:
            # warning 是 `_degrade_cjk` 的 debug 口径的刻意豁免：.so 存在却
            # 装不进来 = 文件损坏/构建不对，是需要人看的异常信号，不是「没编
            # .so」的预期内降级（那条走 `_degrade_cjk` 的 debug）。
            logger.warning("fts5_cjk 扩展不可用: %s", self.cjk_so_path, exc_info=True)
            return False

    @property
    def cjk_available(self) -> bool:
        return self._cjk_available

    # -- writes ------------------------------------------------------------

    @_archive_operation
    def ensure_session(
        self,
        session_id: str,
        *,
        source: str,
        started_at: float | None = None,
        parent_session_id: str | None = None,
        model: str | None = None,
        title: str | None = None,
    ) -> None:
        with self._cursor() as conn:
            conn.execute(
                """
                INSERT INTO sessions(id, source, started_at, parent_session_id, model, title)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    source = excluded.source,
                    parent_session_id = COALESCE(excluded.parent_session_id, sessions.parent_session_id),
                    model = COALESCE(excluded.model, sessions.model),
                    title = COALESCE(excluded.title, sessions.title)
                """,
                (session_id, source, started_at if started_at is not None else time.time(),
                 parent_session_id, model, title),
            )

    @_archive_operation
    def end_session(self, session_id: str, end_reason: str) -> None:
        """关闭会话行（**首写获胜**：已有 `ended_at` 的行不再改写）。

        来源: Hermes `hermes_state_sessions.py:483-490` @ `b324b1f7`
        （`UPDATE ... WHERE id = ? AND ended_at IS NULL`）；与本类
        `_sweep_orphaned_sessions` 的关闭语义一致。行不存在 / 已关闭
        ⇒ 命中 0 行，静默跳过（调用方的 fail-open 依赖它不抛）。

        Hermes 同函数还经 `_end_and_bump` bump conversation generation；
        本仓无该机制，故省略。
        """
        with self._cursor() as conn:
            cur = conn.execute(
                "UPDATE sessions SET ended_at = ?, end_reason = ? "
                "WHERE id = ? AND ended_at IS NULL",
                (time.time(), end_reason, session_id),
            )
            if cur.rowcount <= 0:
                logger.debug("end_session 未命中（不存在或已关闭）: %s", session_id)

    @_archive_operation
    def append_message(
        self,
        session_id: str,
        role: str,
        content: str | None,
        *,
        tool_name: str | None = None,
        tool_calls: str | None = None,
        tool_call_id: str | None = None,
        timestamp: float | None = None,
        active: int = 1,
        compacted: int = 0,
    ) -> int:
        """一条 INSERT（append-only 写路径）。FTS 由触发器同步索引。"""
        columns = {
            "session_id": session_id,
            "role": role,
            "content": content,
            "tool_name": tool_name,
            "tool_calls": tool_calls,
            "tool_call_id": tool_call_id,
            "timestamp": timestamp if timestamp is not None else time.time(),
            "active": active,
            "compacted": compacted,
        }
        names = ", ".join(columns)
        placeholders = ", ".join("?" for _ in columns)
        with self._cursor() as conn:
            cur = conn.execute(
                f"INSERT INTO messages({names}) VALUES ({placeholders})",
                tuple(columns.values()),
            )
            conn.execute(
                "UPDATE sessions SET message_count = message_count + 1, "
                "last_activity_at = ? WHERE id = ?",
                (columns["timestamp"], session_id),
            )
            return int(cur.lastrowid)

    # -- reads -------------------------------------------------------------

    @_archive_operation
    def get_messages(self, session_id: str) -> list[dict[str, Any]]:
        """按 `messages.id` 自增序返回全文（READ 形态的真相读取）。"""
        rows = self._conn.execute(
            "SELECT * FROM messages WHERE session_id = ? ORDER BY id", (session_id,)
        ).fetchall()
        return [dict(row) for row in rows]

    @_archive_operation
    def get_session(self, session_id: str) -> dict[str, Any] | None:
        row = self._conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
        return dict(row) if row is not None else None

    @_archive_operation
    def lineage_root(self, session_id: str) -> str:
        """沿 `parent_session_id` 走到根（lineage 去重的键）。"""
        visited: set[str] = set()
        current = session_id
        while current and current not in visited:
            visited.add(current)
            meta = self.get_session(current)
            parent = meta.get("parent_session_id") if meta else None
            if not parent:
                return current
            current = parent
        return session_id

    def _same_lineage(self, a: str, b: str) -> bool:
        """来源: Hermes `tools/session_search_tool.py:201-203`。"""
        return bool(a and b and self.lineage_root(a) == self.lineage_root(b))

    @_archive_operation
    def _session_left_live_context(self, session_id: str) -> bool:
        """transcript 是否已离开所有人的 live context。

        `compression`（已摘要进子会话）或 fresh reset（子会话从空开始）为真；仍在
        委派中的子会话（end_reason 为空）与 branched 父会话仍是当前上下文。
        来源: Hermes `tools/session_search_tool.py:206-212`。
        """
        meta = self.get_session(session_id) if session_id else None
        end_reason = (meta or {}).get("end_reason")
        return end_reason == "compression" or end_reason in _FRESH_RESET_END_REASONS

    @_archive_operation
    def _is_compacted_message(self, message_id: int | None) -> bool:
        """compaction 归档行（`active=0, compacted=1`）已不在 live context。

        rewind/undo 行是 `active=0, compacted=0`，仍算上下文内。
        来源: Hermes `tools/session_search_tool.py:231-236`。
        """
        if not message_id:
            return False
        row = self._conn.execute(
            "SELECT active, compacted FROM messages WHERE id = ?", (message_id,)
        ).fetchone()
        return row is not None and row["active"] == 0 and row["compacted"] == 1

    @_archive_operation
    def resolve_session_by_title(self, title: str) -> str | None:
        """标题 → session id，优先最新的 `"<title> #N"` 续篇。

        来源: Hermes `hermes_state_titles.py:167-186`。
        """
        exact = self._conn.execute(
            "SELECT id FROM sessions WHERE title = ? ORDER BY started_at DESC LIMIT 1", (title,)
        ).fetchone()
        numbered = self._conn.execute(
            "SELECT id FROM sessions WHERE title LIKE ? ESCAPE '\\' "
            "ORDER BY started_at DESC LIMIT 1",
            (f"{_escape_like(title)} #%",),
        ).fetchone()
        if numbered is not None:
            return numbered["id"]
        return exact["id"] if exact is not None else None

    @_archive_operation
    def search_messages(
        self,
        query: str,
        *,
        limit: int = 10,
        offset: int = 0,
        role_filter: Iterable[str] | None = None,
        source_filter: Iterable[str] | None = None,
        exclude_sources: Iterable[str] | None = None,
        after_ts: float | None = None,
        before_ts: float | None = None,
        sort: str | None = None,
    ) -> list[dict[str, Any]]:
        """FTS5 检索；CJK 走 `messages_fts_cjk`，缺失时降级 trigram→LIKE。"""
        sanitized = _sanitize_fts5_query(query or "")
        if not sanitized:
            return []
        # 一次性物化：生成器在下面的 `in` 判断里会被消费掉。
        role_filter = list(role_filter) if role_filter else None
        order = "ORDER BY m.timestamp DESC, rank"
        if sort == "oldest":
            order = "ORDER BY m.timestamp ASC, rank"
        elif sort == "newest":
            order = "ORDER BY m.timestamp DESC, rank"

        # 显式 tool 角色是全文 opt-in 路径：tool 行索引只有前缀，canonical LIKE 才有全文。
        # 来源: Hermes `hermes_state_search.py:1105-1108`。
        if role_filter and "tool" in role_filter:
            return self._like_rows(
                sanitized, order=order, limit=limit, offset=offset,
                role_filter=role_filter, source_filter=source_filter,
                exclude_sources=exclude_sources, after_ts=after_ts, before_ts=before_ts,
            )

        if _contains_cjk(sanitized):
            return self._search_cjk(
                sanitized, limit=limit, offset=offset, role_filter=role_filter,
                source_filter=source_filter, exclude_sources=exclude_sources,
                after_ts=after_ts, before_ts=before_ts, order=order,
            )

        rows = self._match_rows(
            "messages_fts", sanitized, order=order, limit=limit, offset=offset,
            role_filter=role_filter, source_filter=source_filter,
            exclude_sources=exclude_sources, after_ts=after_ts, before_ts=before_ts,
            raw_query=sanitized,
        )
        if rows:
            return rows
        # Latin miss：`修改youer服务端` 这类中英混排 unicode61 切不开，退到可子串匹配的索引。
        tokens = _non_operator_tokens(sanitized.strip('"').strip())
        if tokens and all(len(t) >= 3 for t in tokens):
            for table in ("messages_fts_cjk", "messages_fts_trigram"):
                if table == "messages_fts_cjk" and not self._cjk_available:
                    continue
                rows = self._match_rows(
                    table, _quote_fts_tokens(sanitized), order=order, limit=limit,
                    offset=offset, role_filter=role_filter, source_filter=source_filter,
                    exclude_sources=exclude_sources, after_ts=after_ts, before_ts=before_ts,
                    raw_query=sanitized,
                )
                if rows:
                    return rows
        # OR-relaxed retry：隐式 AND 让改写的多词查询漏掉只缺一个词的存句。
        # 只在精确查询与子串回退全零命中后触发，命中仍保留原 sort（rank 让覆盖更多
        # 词的行靠前）。单 term / 显式 OR/NOT 不放宽。
        # 来源: Hermes `hermes_state_search.py:1181-1188`。
        relaxed = _or_relaxed_query(sanitized)
        if relaxed is not None:
            rows = self._match_rows(
                "messages_fts", relaxed, order=order, limit=limit, offset=offset,
                role_filter=role_filter, source_filter=source_filter,
                exclude_sources=exclude_sources, after_ts=after_ts, before_ts=before_ts,
                raw_query=sanitized,
            )
        return rows

    @_archive_operation
    def _search_cjk(
        self, sanitized: str, *, limit: int, offset: int, role_filter: Iterable[str] | None,
        source_filter: Iterable[str] | None, exclude_sources: Iterable[str] | None,
        after_ts: float | None, before_ts: float | None, order: str,
    ) -> list[dict[str, Any]]:
        raw = sanitized.strip('"').strip()
        wants_tool_rows = bool(role_filter) and "tool" in list(role_filter)
        match = _quote_fts_tokens(raw)
        if self._cjk_available and not wants_tool_rows and not _has_lone_cjk_run(raw):
            rows = self._match_rows(
                "messages_fts_cjk", match, order=order, limit=limit, offset=offset,
                role_filter=role_filter, source_filter=source_filter,
                exclude_sources=exclude_sources, after_ts=after_ts, before_ts=before_ts,
                raw_query=raw,
            )
            if rows:
                return rows
        tokens = _non_operator_tokens(raw)
        if tokens and all(len(t) >= 3 for t in tokens):
            rows = self._match_rows(
                "messages_fts_trigram", match, order=order, limit=limit, offset=offset,
                role_filter=role_filter, source_filter=source_filter,
                exclude_sources=exclude_sources, after_ts=after_ts, before_ts=before_ts,
                raw_query=raw,
            )
            if rows:
                return rows
        # 基础索引对 CJK 按单字切词（假阳性多，但能覆盖 tool 行前缀）。
        rows = self._match_rows(
            "messages_fts", match, order=order, limit=limit, offset=offset,
            role_filter=role_filter, source_filter=source_filter,
            exclude_sources=exclude_sources, after_ts=after_ts, before_ts=before_ts,
            raw_query=raw,
        )
        if rows:
            return rows
        return self._like_rows(
            raw, order=order, limit=limit, offset=offset, role_filter=role_filter,
            source_filter=source_filter, exclude_sources=exclude_sources,
            after_ts=after_ts, before_ts=before_ts,
        )

    def _where(
        self, *, role_filter: Iterable[str] | None, source_filter: Iterable[str] | None,
        exclude_sources: Iterable[str] | None, after_ts: float | None, before_ts: float | None,
    ) -> tuple[str, list[Any]]:
        clauses = ["(m.active = 1 OR m.compacted = 1)", "COALESCE(m.display_kind, '') <> 'hidden'"]
        params: list[Any] = []
        if role_filter:
            values = list(role_filter)
            clauses.append(f"m.role IN ({','.join('?' for _ in values)})")
            params.extend(values)
        if source_filter:
            values = list(source_filter)
            clauses.append(f"s.source IN ({','.join('?' for _ in values)})")
            params.extend(values)
        if exclude_sources:
            values = list(exclude_sources)
            clauses.append(f"s.source NOT IN ({','.join('?' for _ in values)})")
            params.extend(values)
        if after_ts is not None:
            clauses.append("s.started_at >= ?")
            params.append(after_ts)
        if before_ts is not None:
            clauses.append("s.started_at < ?")
            params.append(before_ts)
        return " AND ".join(clauses), params

    @_archive_operation
    def _match_rows(
        self, table: str, match: str, *, order: str, limit: int, offset: int,
        raw_query: str | None = None, **filters: Any,
    ) -> list[dict[str, Any]]:
        where, params = self._where(**filters)
        sql = f"""
            SELECT m.id, m.session_id, m.role,
                   snippet({table}, 0, '[', ']', '…', 12) AS snippet,
                   m.timestamp, m.tool_name, s.source, s.model,
                   s.started_at AS session_started
            FROM {table}
            JOIN messages AS m ON m.id = {table}.rowid
            JOIN sessions AS s ON s.id = m.session_id
            WHERE {table} MATCH ? AND {where}
            {order}
            LIMIT ? OFFSET ?
        """
        try:
            rows = self._conn.execute(sql, (match, *params, limit, offset)).fetchall()
        except sqlite3.OperationalError:
            return []
        except sqlite3.DatabaseError:
            # 损坏（非语法错）：派生索引不可用，但 canonical 行仍在——降级 LIKE。
            # 来源: Hermes `hermes_state_search.py:957` / `:1127-1141`。
            logger.warning(
                "%s FTS 检索命中损坏错误；降级 canonical LIKE", table, exc_info=True,
            )
            if raw_query is None:
                raise
            return self._like_rows(
                raw_query, order=order, limit=limit, offset=offset, **filters,
            )
        return [dict(row) for row in rows]

    @_archive_operation
    def _like_rows(
        self, raw: str, *, order: str, limit: int, offset: int, **filters: Any,
    ) -> list[dict[str, Any]]:
        """canonical 行 LIKE 扫描：每个 term 一个三列 OR 子句，term 之间 OR。

        `content` / `tool_name` / `tool_calls` 三列都要看（词可能只在 tool 名里）；
        扫的是 canonical 全文——索引 bound 不是检索 bound，显式 tool 搜索本来就该
        看到 8192 之后的内容。来源: Hermes `hermes_state_search.py:48-50`、`:1209-1220`。
        """
        where, params = self._where(**filters)
        # LIKE 扫描没有 `rank` 列：沿用调用方的时间方向，去掉 rank。
        like_order = "ORDER BY m.timestamp ASC, m.id ASC" if " ASC" in order else \
            "ORDER BY m.timestamp DESC, m.id DESC"
        terms = [t.strip('"').strip() for t in _non_operator_tokens(raw)] or [raw]
        terms = [t for t in terms if t] or [raw]
        clause = (
            "(COALESCE(m.content, '') LIKE ? ESCAPE '\\' "
            "OR COALESCE(m.tool_name, '') LIKE ? ESCAPE '\\' "
            "OR COALESCE(m.tool_calls, '') LIKE ? ESCAPE '\\')"
        )
        like_clause = " OR ".join(clause for _ in terms)
        sql = f"""
            SELECT m.id, m.session_id, m.role,
                   substr(COALESCE(m.content, ''), 1, 160) AS snippet,
                   m.timestamp, m.tool_name, s.source, s.model,
                   s.started_at AS session_started
            FROM messages AS m
            JOIN sessions AS s ON s.id = m.session_id
            WHERE {where} AND ({like_clause})
            {like_order}
            LIMIT ? OFFSET ?
        """
        escaped = [f"%{_escape_like(t)}%" for t in terms for _ in range(3)]
        try:
            rows = self._conn.execute(sql, (*params, *escaped, limit, offset)).fetchall()
        except sqlite3.OperationalError:
            return []
        return [dict(row) for row in rows]

    # -- session_search（四形态） ------------------------------------------

    @_archive_operation
    def session_search(
        self,
        *,
        query: str = "",
        session_id: str | None = None,
        around_message_id: int | None = None,
        window: int = 5,
        limit: int = 3,
        sort: str | None = None,
        after: str | None = None,
        before: str | None = None,
        current_session_id: str | None = None,
    ) -> dict[str, Any]:
        """内部 Python API（本票不注册为 agent 工具）：DISCOVERY/SCROLL/READ/BROWSE。"""
        if session_id:
            if around_message_id is not None:
                return self._scroll(session_id, around_message_id, window, current_session_id)
            return self._read(session_id)
        limit = max(1, min(int(limit), 10))
        if not query or not query.strip():
            return self._browse(limit, current_session_id)
        sort = sort if sort in ("newest", "oldest") else None
        return self._discover(
            query.strip(), limit=limit, sort=sort,
            after_ts=_parse_time_bound(after), before_ts=_parse_time_bound(before),
            current_session_id=current_session_id,
        )

    @_archive_operation
    def _browse(self, limit: int, current_session_id: str | None) -> dict[str, Any]:
        placeholders = ",".join("?" for _ in HIDDEN_SESSION_SOURCES)
        rows = self._conn.execute(
            f"""
            SELECT id, source, model, started_at, last_activity_at, message_count, title
            FROM sessions
            WHERE source NOT IN ({placeholders})
              AND id IS NOT ?
            ORDER BY COALESCE(last_activity_at, started_at) DESC, started_at DESC
            LIMIT ?
            """,
            (*HIDDEN_SESSION_SOURCES, current_session_id, limit),
        ).fetchall()
        results = [
            {
                "session_id": row["id"],
                "title": row["title"],
                "source": row["source"],
                "started_at": row["started_at"],
                "last_active": row["last_activity_at"],
                "message_count": row["message_count"],
            }
            for row in rows
        ]
        return {"mode": "browse", "results": results, "count": len(results)}

    @_archive_operation
    def _read(self, session_id: str, head: int = 20, tail: int = 10) -> dict[str, Any]:
        meta = self.get_session(session_id)
        if meta is None:
            return {"mode": "read", "success": False, "error": f"session_id not found: {session_id}"}
        rows = self.get_messages(session_id)
        shaped = [_shape_message(row, max_content_len=_READ_MAX_CONTENT) for row in rows]
        truncated = len(shaped) > head + tail
        return {
            "mode": "read",
            "session_id": session_id,
            "session_meta": self._meta_block(meta),
            "message_count": len(shaped),
            "truncated": truncated,
            "messages": shaped[:head] + shaped[-tail:] if truncated else shaped,
        }

    @_archive_operation
    def _scroll(
        self, session_id: str, around_message_id: int, window: int, current_session_id: str | None,
    ) -> dict[str, Any]:
        try:
            anchor = int(around_message_id)
        except (TypeError, ValueError):
            return {"mode": "scroll", "success": False, "error": "scroll requires integer around_message_id"}
        window = max(1, min(int(window), 20))
        anchor_row = self._conn.execute(
            "SELECT session_id, active, compacted FROM messages WHERE id = ?", (anchor,)
        ).fetchone()
        owning = anchor_row["session_id"] if anchor_row is not None else None
        if owning is None:
            return {
                "mode": "scroll", "success": False,
                "error": f"around_message_id {anchor} not found",
            }
        # 锚点仍在调用方 live context → 硬拒绝（不降级成 note）。
        # 判据照抄 Hermes `_anchor_in_live_context`（`session_search_tool.py:522-528`）。
        if current_session_id and self._anchor_in_live_context(anchor_row, owning, current_session_id):
            return {
                "mode": "scroll", "success": False,
                "error": "scroll rejected: anchor lives in the current session lineage "
                         "(already in your active context)",
            }
        meta = self.get_session(session_id)
        if meta is None:
            return {"mode": "scroll", "success": False, "error": f"session_id not found: {session_id}"}
        out: dict[str, Any] = {}
        if owning != session_id:
            # 请求方把父 session_id 配上了子会话里的 message id：仅同 lineage 透明
            # rebind，且 session_meta 一律取 owning 会话。来源: Hermes `:544-556`。
            if not self._same_lineage(session_id, owning):
                return {
                    "mode": "scroll", "success": False,
                    "error": f"around_message_id {anchor} not in session_id {session_id}",
                }
            out["warning"] = (
                f"around_message_id {anchor} lives in {owning} (child of {session_id}); "
                "rebound transparently"
            )
            session_id = owning
            meta = self.get_session(owning) or meta
        rows = self._conn.execute(
            "SELECT * FROM messages WHERE session_id = ? AND id BETWEEN ? AND ? ORDER BY id",
            (owning, anchor - window, anchor + window),
        ).fetchall()
        if not rows:
            return {
                "mode": "scroll", "success": False,
                "error": f"around_message_id {anchor} not in session_id {session_id}",
            }
        messages = [
            _shape_message(dict(row), anchor_id=anchor, max_content_len=_SCROLL_MAX_CONTENT)
            for row in rows
        ]
        before = self._conn.execute(
            "SELECT COUNT(*) FROM messages WHERE session_id = ? AND id < ?",
            (owning, anchor - window),
        ).fetchone()[0]
        after = self._conn.execute(
            "SELECT COUNT(*) FROM messages WHERE session_id = ? AND id > ?",
            (owning, anchor + window),
        ).fetchone()[0]
        out.update({
            "mode": "scroll",
            "session_id": owning,
            "around_message_id": anchor,
            "session_meta": self._meta_block(meta),
            "window": window,
            "messages": messages,
            "messages_before": before,
            "messages_after": after,
        })
        return out

    @_archive_operation
    def _anchor_in_live_context(
        self, anchor_row: sqlite3.Row, owning: str, current_session_id: str,
    ) -> bool:
        """锚点是否仍在调用方 live context（是则 SCROLL 拒绝）。

        同 lineage 且已离开 live context 的历史（compacted 行、compression 结束的
        父会话、/new 重置的前任）放行；rewind/undo 行（active=0, compacted≠1）
        从不算「已离开上下文」。来源: Hermes `tools/session_search_tool.py:522-528`。
        """
        if not self._same_lineage(owning, current_session_id):
            return False
        if anchor_row["active"] == 0 and anchor_row["compacted"] == 1:
            return False
        return anchor_row["active"] == 0 or not self._session_left_live_context(owning)

    @_archive_operation
    def _discover(
        self, query: str, *, limit: int, sort: str | None, after_ts: float | None,
        before_ts: float | None, current_session_id: str | None,
    ) -> dict[str, Any]:
        current_root = self.lineage_root(current_session_id) if current_session_id else None
        # 先试标题：模型常引用记得的会话名。来源: Hermes `:276-313`。
        title_entry = self._title_match(query, current_root, after_ts, before_ts)
        raw = self.search_messages(
            query, limit=_DISCOVER_SCAN_LIMIT, role_filter=("user", "assistant"),
            exclude_sources=HIDDEN_SESSION_SOURCES, after_ts=after_ts, before_ts=before_ts,
            sort=sort,
        )
        # cron 降权：稳定排序让交互会话永远排在前面（Hermes #19434）。
        raw = sorted(raw, key=lambda r: (r.get("source") or "") in DEMOTED_SESSION_SOURCES)
        if not raw and title_entry is None:
            return {
                "mode": "discover", "query": query, "results": [], "count": 0,
                "message": (
                    "No matching sessions found. FTS5 ANDs all terms by default — "
                    "broaden with OR, exact-match with quoted phrases, or prefix-match with `deploy*`."
                ),
            }
        seen: dict[str, dict[str, Any]] = {}
        results: list[dict[str, Any]] = []
        if title_entry is not None:
            results.append(title_entry)
            # 该 lineage 不再被 FTS 结果重复加入。
            seen[title_entry.pop("_lineage_root", None) or title_entry["session_id"]] = {}
        for row in raw:
            if len(seen) >= limit:
                break
            root = self.lineage_root(row["session_id"])
            if root in seen:
                continue
            # 当前 lineage 的命中跳过，除非 transcript 已离开 live context：
            # (a) owning 会话 compression / fresh-reset 结束；(b) 命中行是 compacted 行；
            # (c) 命中就在 current_session_id 本身且是 compacted 行。
            # 来源: Hermes `tools/session_search_tool.py:388-415`。
            is_compacted_hit = self._is_compacted_message(row.get("id"))
            if current_root and root == current_root and not (
                self._session_left_live_context(row["session_id"]) or is_compacted_hit
            ):
                continue
            if (
                current_session_id and row["session_id"] == current_session_id
                and not is_compacted_hit
            ):
                continue
            seen[root] = {**row, "_lineage_root": root}
        first = title_entry is not None
        for root, match in seen.items():
            if not match:
                continue
            entry = self._hydrate(root, match, full=first)
            first = False
            if entry is not None:
                results.append(entry)
        # 全部命中都被当前 lineage 跳过时不能静默空结果（F15）。
        out: dict[str, Any] = {
            "mode": "discover", "query": query, "results": results,
            "count": len(results), "sessions_searched": len(seen),
        }
        if not results:
            out["message"] = (
                "No matching sessions found. FTS5 ANDs all terms by default — "
                "broaden with OR, exact-match with quoted phrases, or prefix-match with `deploy*`."
            )
        return out

    @_archive_operation
    def _title_match(
        self, query: str, current_root: str | None,
        after_ts: float | None, before_ts: float | None,
    ) -> dict[str, Any] | None:
        """查询命中会话标题时的 DISCOVERY entry；否则 None。

        来源: Hermes `tools/session_search_tool.py:276-313`。
        """
        title_query = query.strip().strip("`'\"")
        if not title_query:
            return None
        session_id = self.resolve_session_by_title(title_query)
        if not session_id:
            return None
        root = self.lineage_root(session_id)
        # 同 lineage 的标题命中只在会话已离开 live context 时可召回。
        if current_root and root == current_root and not self._session_left_live_context(session_id):
            return None
        meta = self.get_session(root) or self.get_session(session_id) or {}
        if meta.get("source") in HIDDEN_SESSION_SOURCES:
            return None
        started = meta.get("started_at")
        if after_ts is not None and (started is None or started < after_ts):
            return None
        if before_ts is not None and (started is None or started >= before_ts):
            return None
        messages = self.get_messages(session_id)
        anchor_id = messages[0]["id"] if messages else None
        title = meta.get("title") or title_query
        entry = self._hydrate(
            root,
            {"id": anchor_id, "session_id": session_id, "role": None, "snippet": ""},
            full=True,
        ) or {}
        entry.update(
            matched_role="session_title",
            snippet=f"Session title matched: {title}",
            title=title,
            detail="full",
        )
        entry["_lineage_root"] = root
        return entry

    @_archive_operation
    def _hydrate(self, lineage_root: str, match: dict[str, Any], *, full: bool) -> dict[str, Any] | None:
        meta = self.get_session(lineage_root) or self.get_session(match["session_id"]) or {}
        message_id = match.get("id")
        window_rows = self._conn.execute(
            "SELECT * FROM messages WHERE session_id = ? AND id BETWEEN ? AND ? ORDER BY id",
            (match["session_id"], (message_id or 0) - 2, (message_id or 0) + 2),
        ).fetchall()
        entry: dict[str, Any] = {
            "session_id": match["session_id"],
            "when": meta.get("started_at"),
            "source": meta.get("source") or match.get("source", "unknown"),
            "model": meta.get("model") or match.get("model"),
            "title": meta.get("title"),
            "matched_role": match.get("role"),
            "match_message_id": message_id,
            "snippet": match.get("snippet") or "",
            "messages": [
                _shape_message(dict(row), anchor_id=message_id, max_content_len=_SCROLL_MAX_CONTENT)
                for row in window_rows
                if full or row["id"] == message_id
            ],
            "detail": "full" if full else "compact",
        }
        if full:
            start = self._conn.execute(
                "SELECT * FROM messages WHERE session_id = ? ORDER BY id LIMIT 3",
                (match["session_id"],),
            ).fetchall()
            end = self._conn.execute(
                "SELECT * FROM messages WHERE session_id = ? ORDER BY id DESC LIMIT 3",
                (match["session_id"],),
            ).fetchall()
            entry["bookend_start"] = [
                _shape_message(dict(row), max_content_len=_BOOKEND_MAX_CONTENT)
                for row in start if not _is_compaction_summary(row["content"])
            ]
            entry["bookend_end"] = [
                _shape_message(dict(row), max_content_len=_BOOKEND_MAX_CONTENT)
                for row in reversed(end) if not _is_compaction_summary(row["content"])
            ]
        else:
            entry["bookend_start"] = []
            entry["bookend_end"] = []
        if lineage_root and lineage_root != entry["session_id"]:
            entry["lineage_root"] = lineage_root
        return entry

    @staticmethod
    def _meta_block(meta: dict[str, Any]) -> dict[str, Any]:
        return {
            "when": meta.get("started_at"),
            "source": meta.get("source"),
            "model": meta.get("model"),
            "title": meta.get("title"),
        }

    # -- 自动维护（auto-prune / stale-open sweep / VACUUM） -----------------

    @_archive_operation
    def _get_meta(self, key: str) -> str | None:
        row = self._conn.execute("SELECT value FROM state_meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row is not None else None

    @_archive_operation
    def _set_meta(self, key: str, value: str) -> None:
        with self._cursor() as conn:
            conn.execute(
                "INSERT INTO state_meta(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )

    @_archive_operation
    def _prune_sessions(self, cutoff: float) -> int:
        """删除 `ended_at` 已写、未 pinned、activity < cutoff 的会话。

        子会话先孤儿化（`parent_session_id = NULL`）而不是级联删除：留在窗口外的
        子会话仍然可读。删 messages 再删 sessions，FTS 行由 delete 触发器连带删。
        被 sweep close 的孤儿（`end_reason='startup_orphan_reap'`）按 `ended_at`
        计龄而不是按旧 activity：否则它们会在下一轮维护立刻被删，拿不到完整
        retention 窗口（Hermes `_PRUNE_FILTERS` 的 `last_active_before` 条款）。
        来源: Hermes `hermes_state_maintenance.py:301-330`（`prune_sessions`）、
        `:52-56`（`_PRUNE_FILTERS`）。
        """
        activity = _session_activity_sql("s")
        rows = self._conn.execute(
            f"SELECT s.id FROM sessions AS s WHERE s.ended_at IS NOT NULL "
            f"AND COALESCE(s.pinned, 0) = 0 AND {activity} < ? "
            f"AND (COALESCE(s.end_reason, '') != 'startup_orphan_reap' OR s.ended_at < ?)",
            (cutoff, cutoff),
        ).fetchall()
        ids = [row["id"] for row in rows]
        if not ids:
            return 0
        with self._cursor() as conn:
            for start in range(0, len(ids), _PRUNE_CHUNK):
                chunk = ids[start:start + _PRUNE_CHUNK]
                placeholders = ",".join("?" for _ in chunk)
                conn.execute(
                    f"UPDATE sessions SET parent_session_id = NULL "
                    f"WHERE parent_session_id IN ({placeholders})",
                    chunk,
                )
                conn.execute(f"DELETE FROM messages WHERE session_id IN ({placeholders})", chunk)
                conn.execute(f"DELETE FROM sessions WHERE id IN ({placeholders})", chunk)
        return len(ids)

    @_archive_operation
    def _sweep_orphaned_sessions(self, cutoff: float, now: float) -> list[str]:
        """把进程死亡遗留的 open 自动化会话**关闭**（不删）。

        判据：`ended_at IS NULL` + source ∈ `AUTO_PRUNE_STALE_OPEN_SOURCES` +
        activity < cutoff + 未 pinned。只写 `ended_at` / `end_reason`：消息保留、
        会话可恢复，并从关闭时刻重新获得完整 retention 窗口。
        `started_at < cutoff` 与 Hermes 同：保护 activity 抄自旧会话的
        压缩 / 分支子会话。
        「mid-turn」在本仓没有租约机制，`ended_at IS NULL` 即视为 open 不删。
        来源: Hermes `hermes_state_maintenance.py:139-196`（`sweep_orphaned_sessions`）。
        """
        if not AUTO_PRUNE_STALE_OPEN_SOURCES:
            return []
        placeholders = ",".join("?" for _ in AUTO_PRUNE_STALE_OPEN_SOURCES)
        activity = _session_activity_sql("sessions")
        rows = self._conn.execute(
            f"SELECT id FROM sessions WHERE ended_at IS NULL "
            f"AND source IN ({placeholders}) AND COALESCE(pinned, 0) = 0 "
            f"AND started_at < ? AND {activity} < ?",
            (*AUTO_PRUNE_STALE_OPEN_SOURCES, cutoff, cutoff),
        ).fetchall()
        victims = [row["id"] for row in rows]
        if not victims:
            return []
        # 按 victims id 列表 UPDATE（而非重写一遍谓词）：关闭的行必然 ==
        # 返回的 victims，两处谓词无漂移空间；`AND ended_at IS NULL` 只防
        # SELECT→UPDATE 之间被别处关闭的竞态，不复刻完整谓词。
        with self._cursor() as conn:
            conn.execute(
                "UPDATE sessions SET ended_at = ?, end_reason = 'startup_orphan_reap' "
                f"WHERE id IN ({','.join('?' for _ in victims)}) AND ended_at IS NULL",
                (now, *victims),
            )
        return victims

    @_archive_operation
    def _freelist_ratio(self) -> float | None:
        """可回收页占比；None = 读不到（退回时间节流，照抄 Hermes 原语义）。
        来源: Hermes `hermes_state_maintenance.py:353`
        （"None = fall back to the time throttle"）。"""
        try:
            pages = int(self._conn.execute("PRAGMA page_count").fetchone()[0])
            free = int(self._conn.execute("PRAGMA freelist_count").fetchone()[0])
        except sqlite3.DatabaseError:
            return None
        return free / pages if pages > 0 else 0.0

    @_archive_operation
    def maybe_auto_prune_and_vacuum(
        self,
        *,
        auto_prune: bool = True,
        retention_days: float = 90,
        min_interval_hours: float = 24,
        vacuum: bool = True,
        min_vacuum_interval_days: float = 30,
        min_vacuum_freelist_ratio: float = AUTO_VACUUM_MIN_FREELIST_RATIO,
        now: float | None = None,
    ) -> dict[str, Any]:
        """幂等的启动期自动维护：prune → stale-open sweep → 可选 VACUUM；**永不抛**。

        最多每 `min_interval_hours` 跑一次（`state_meta['last_auto_prune']`，跨进程
        共享）。VACUUM 另有 `min_vacuum_interval_days` 节流，并要求
        `freelist_count / page_count` 超过阈值——小库删几行不值得整库重写。

        顺序是先 prune 后 sweep（Hermes 原语义）：被 close 的孤儿从关闭时刻起获得
        完整 retention 窗口，本轮不会被删。`last_auto_prune` 无论删没删到行都写，
        节流对空跑同样生效。

        返回 `{"skipped", "pruned", "closed", "vacuumed"}`，考虑过 VACUUM 时带
        `"freelist_ratio"`，失败时带 `"error"`。
        来源: Hermes `hermes_state_maintenance.py:400-510`
        （`maybe_auto_prune_and_vacuum`）。

        未照抄的部分（本仓无对应机制）：Hermes 的跨进程 maintenance lock、多进程
        WAL holder 扫描（`foreign_state_db_holders`）、启动看门狗租约、transcript
        文件删除、lineage 整体删除；本仓 archive 是派生索引，没有这些对象。
        """
        result: dict[str, Any] = {"skipped": False, "pruned": 0, "closed": 0, "vacuumed": False}
        if not auto_prune:
            result["skipped"] = True
            return result
        if retention_days is None or retention_days < 0:
            # 负 retention 会构造未来 cutoff，匹配所有已结束会话；
            # 禁用开关是 auto_prune=False，不是负数。
            logger.warning(
                "会话自动维护跳过：retention_days=%r 非法（需 >= 0）；"
                "关闭请设 auto_prune=False",
                retention_days,
            )
            result["skipped"] = True
            return result
        try:
            now = time.time() if now is None else now
            since_prune = _seconds_since(now, self._get_meta("last_auto_prune"))
            if since_prune is not None and since_prune < min_interval_hours * 3600:
                result["skipped"] = True
                return result
            cutoff = now - float(retention_days) * 86400.0
            # 先 prune：下面被 close 的孤儿因此拿到完整 retention 窗口。
            result["pruned"] = pruned = self._prune_sessions(cutoff)
            closed = self._sweep_orphaned_sessions(cutoff, now)
            result["closed"] = len(closed)
            # VACUUM 只在真的删了行、时间节流已过时跑；占比读不到（None）时
            # 退回时间节流照常 VACUUM（Hermes `hermes_state_maintenance.py:482`
            # `ratio is None or ratio > ...` 原语义）。
            since_vacuum = _seconds_since(now, self._get_meta("last_vacuum"))
            vacuum_due = since_vacuum is None or since_vacuum >= min_vacuum_interval_days * 86400
            if vacuum and pruned > 0 and vacuum_due:
                result["freelist_ratio"] = ratio = self._freelist_ratio()
                if ratio is None or ratio > min_vacuum_freelist_ratio:
                    self._conn.execute("VACUUM")
                    self._set_meta("last_vacuum", str(now))
                    result["vacuumed"] = True
            # pruned == 0 也要写：节流对空跑同样生效（Hermes 原语义）。
            self._set_meta("last_auto_prune", str(now))
            if closed or pruned > 0:
                logger.info(
                    "会话自动维护：关闭 %d 个 stale open 会话，清理 %d 个 %s 天无活动的会话%s",
                    len(closed), pruned, retention_days,
                    " + VACUUM" if result["vacuumed"] else "",
                )
        except Exception as exc:
            # 维护失败绝不能反噬会话写路径（本方法挂在写入触发的惰性路径上）。
            logger.warning("会话自动维护失败: %s", exc, exc_info=True)
            result["error"] = str(exc)
        return result


# --------------------------------------------------------------- 产品接线 --


#: `sessions.source` 的词汇表 → 搜索语义（hidden / demoted 见上方常量）。
#: 票面 §4.5：`interactive` 正常；`subagent`（`SessionMeta.origin='delegation'`）
#: hidden；`fork`（`origin='fork'`）hidden；`cron` demoted。
SOURCE_INTERACTIVE = "interactive"
SOURCE_SUBAGENT = "subagent"
SOURCE_FORK = "fork"
SOURCE_CRON = "cron"

#: 归档关心的三种「终态消息」事件（`model/delta` 流式 chunk 一律不归档）。
_ARCHIVED_EVENT_TYPES = frozenset({"user/message", "model/completed", "tool/result"})

#: provenance 事件：它们决定会话来源，出现时必须重跑一次 upsert（首条
#: `session/started` 可能还看不出是 fork / 委派）。
_PROVENANCE_EVENT_TYPES = frozenset({"session/started", "session/forked"})


class SessionArchiveObserver:
    """把落盘事件写进归档索引的 store 级观察者（#1389 写入钩子）。

    挂在 `JsonlSessionStore.set_event_observer` 上——所有生产写路径（web run /
    CLI `run` / 委派子代理 / fork）最终都经 `JsonlSessionStore.append_event`，
    一处接线即全覆盖；不新增事件类型，也不改事件载荷（不变量 #22）。

    会话行**延迟创建**：本库第一次见到某会话时按事件推断 `source` 并 upsert。
    判据全部取自事件自身（不查 `SessionMetaStore`——归档观察者跑在写锁之外的
    同步线程里，那边是异步接口）：

    * `session/forked` → `fork`
    * `session/started` 带委派键（`delegation_tree_id`）→ `subagent`
      （委派子会话由 `Session(...)` 直接构造，不走 `Session.start`，首条
      `session/started` 就带委派键）
    * 其余 → `interactive`

    `cron` 在本仓暂无 producer（票面 ACK gap1 的存量事实）：词汇与 demoted
    搜索语义已实现，未来的生产者调 `note_source(session_id, "cron")` 即可登记。

    失败只记 warning：归档是派生索引，绝不反噬事件流写入。
    """

    def __init__(
        self,
        archive: SessionArchive | str | Path,
        *,
        cjk_so_path: str | Path | None = None,
        auto_prune: bool = True,
        retention_days: float = 90,
        min_interval_hours: float = 24,
    ) -> None:
        # 传路径时**惰性建库**：web 的 `AppState` 构造早于任何会话，若在那里
        # 就落盘 state.db，会破坏「请求被拒时不留痕迹」的既有不变量
        # （tests/session/test_launch_false.py 实测撞红）。只有在**观察到第一条
        # 归档事件**时才建库——那时会话早已真实存在。
        self._archive = archive if isinstance(archive, SessionArchive) else None
        self._archive_path = None if self._archive is not None else archive
        self._cjk_so_path = cjk_so_path
        # 自动维护配置（Hermes `sessions.*` 默认值）。物化 archive 后跑一次；
        # 内部节流保证 24h 内 no-op，失败不抛。
        self._auto_prune = auto_prune
        self._retention_days = retention_days
        self._min_interval_hours = min_interval_hours
        self._maintained = False
        # 进程内已登记过的会话（省掉每条事件的 upsert）。**只是缓存**：
        # 多进程 / 重启后由 `ensure_session` 的 INSERT..ON CONFLICT 兜底。
        self._known: set[str] = set()
        self._lock = threading.Lock()
        self._store: Any | None = None
        self._owns_archive = self._archive is None
        self._closed = False

    @property
    def archive(self) -> SessionArchive:
        # 首建走双重检查锁定：并发首访（观察者事件线程 + web 请求线程）只建
        # 一个实例，避免重复 initialize/维护与孤儿连接。`initialize()` 与
        # `maybe_auto_prune_and_vacuum()` 都不回调用本对象的 `_lock`，持锁安全。
        with self._lock:
            if self._closed:
                raise RuntimeError("SessionArchiveObserver is closed")
            if self._archive is None:
                assert self._archive_path is not None
                arc = SessionArchive(
                    self._archive_path, cjk_so_path=self._cjk_so_path,
                )
                arc.initialize()
                self._archive = arc
            archive = self._archive
            assert archive is not None
            if not self._maintained:
                self._maintained = True
                # 惰性触发点：建库完成即跑一次自动维护（写入路径上，永不抛）。
                archive.maybe_auto_prune_and_vacuum(
                    auto_prune=self._auto_prune,
                    retention_days=self._retention_days,
                    min_interval_hours=self._min_interval_hours,
                )
            return archive

    def close(self) -> None:
        """Detach from the store and close only an archive owned by this observer."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            store = self._store
            self._store = None
            archive = self._archive if self._owns_archive else None
            if store is not None and getattr(store, "_event_observer", None) is self:
                store.set_event_observer(None)
        if archive is not None:
            archive.close()

    # -- 接线 --------------------------------------------------------------

    def attach(self, store: Any) -> None:
        with self._lock:
            if self._closed:
                raise RuntimeError("SessionArchiveObserver is closed")
            store.set_event_observer(self)
            self._store = store

    # -- 观察者入口 --------------------------------------------------------

    def end_session(self, session_id: str, end_reason: str) -> None:
        """结束会话行（`/new` 换会话的唯一调用口）。

        fail-open 边界与 `__call__` 同一条：归档是派生索引，写失败只记 warning。
        """
        try:
            self.archive.end_session(session_id, end_reason)
        except Exception:
            logger.warning(
                "会话归档结束行失败（session=%s, reason=%s）",
                session_id, end_reason, exc_info=True,
            )

    def __call__(self, session_id: str, event: Any) -> None:
        try:
            self.observe(session_id, event)
        except Exception:
            logger.warning(
                "会话归档写入失败（session=%s, event=%s）",
                session_id, getattr(event, "type", "?"), exc_info=True,
            )

    def observe(self, session_id: str, event: Any) -> None:
        event_type = event.type
        data = event.data or {}
        if event_type == "agent/delegation-started":
            # 这是**父**会话里的事实，指向子会话 id：子会话的行若已存在就补父链。
            child = data.get("child_session_id")
            if isinstance(child, str) and child:
                self._upsert_session(
                    child, "agent/delegation-started", data,
                    parent_session_id=session_id,
                )
            return
        if event_type in _PROVENANCE_EVENT_TYPES:
            self._upsert_session(session_id, event_type, data)
        elif event_type in _ARCHIVED_EVENT_TYPES:
            self._ensure_session(session_id)
        else:
            return
        if event_type == "user/message":
            self.archive.append_message(
                session_id, "user", data.get("content"), timestamp=time.time(),
            )
        elif event_type == "model/completed":
            tool_calls = data.get("tool_calls")
            self.archive.append_message(
                session_id, "assistant", data.get("content"),
                tool_calls=(
                    json.dumps(tool_calls, ensure_ascii=False) if tool_calls else None
                ),
                timestamp=time.time(),
            )
        elif event_type == "tool/result":
            self.archive.append_message(
                session_id, "tool", data.get("content"),
                tool_call_id=data.get("tool_call_id"), timestamp=time.time(),
            )

    # -- 会话行 ------------------------------------------------------------

    def note_source(self, session_id: str, source: str) -> None:
        """显式登记一个会话来源（供未来的 cron 生产者用）。"""
        self.archive.ensure_session(session_id, source=source, started_at=time.time())
        with self._lock:
            self._known.add(session_id)

    def _ensure_session(self, session_id: str) -> None:
        with self._lock:
            if session_id in self._known:
                return
        self._upsert_session(session_id, None, {})

    def _upsert_session(
        self, session_id: str, event_type: str | None, data: dict,
        *, parent_session_id: str | None = None,
    ) -> None:
        if event_type == "session/forked":
            parent = data.get("parent_session_id")
            parent_session_id = str(parent) if parent else None
        self.archive.ensure_session(
            session_id,
            source=_source_for(event_type, data),
            started_at=time.time(),
            parent_session_id=parent_session_id,
        )
        with self._lock:
            self._known.add(session_id)


def _source_for(event_type: str | None, data: dict) -> str:
    """按票面 §4.5 推断 `sessions.source`（判据见 `SessionArchiveObserver`）。"""
    if event_type == "session/forked":
        return SOURCE_FORK
    if event_type == "agent/delegation-started":
        return SOURCE_SUBAGENT
    if event_type == "session/started" and data.get("delegation_tree_id"):
        return SOURCE_SUBAGENT
    return SOURCE_INTERACTIVE


def install_session_archive(
    store: Any, db_path: str | Path, *, cjk_so_path: str | Path | None = None,
    auto_prune: bool = True, retention_days: float = 90, min_interval_hours: float = 24,
) -> SessionArchiveObserver:
    """把归档观察者挂到 store 上（#1389 的**唯一**生产接线点）。

    不碰磁盘：建库推迟到第一条归档事件（见 `SessionArchiveObserver.__init__`）。
    建库/写入失败只记 warning——存档是派生检索索引，起不来不能让会话写路径
    跟着不可用。

    三个自动维护参数（Hermes `sessions.*` 默认值）透传给观察者；它在**首次物化**
    archive 后跑一次 `maybe_auto_prune_and_vacuum`（内部节流，失败不抛）。
    """
    observer = SessionArchiveObserver(
        db_path, cjk_so_path=cjk_so_path,
        auto_prune=auto_prune, retention_days=retention_days,
        min_interval_hours=min_interval_hours,
    )
    observer.attach(store)
    return observer
