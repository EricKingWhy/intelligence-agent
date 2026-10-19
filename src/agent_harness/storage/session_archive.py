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
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from agent_harness.session import Session

logger = logging.getLogger(__name__)

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

_RELATIVE_BOUND_RE = re.compile(r"^(\d+)\s*(h|d|w)$", re.IGNORECASE)
_RELATIVE_UNIT_SECONDS = {"h": 3_600, "d": 86_400, "w": 604_800}

_CJK_RANGES = (
    (0x4E00, 0x9FFF), (0x3400, 0x4DBF), (0x20000, 0x2A6DF), (0x3000, 0x303F),
    (0x3040, 0x309F), (0x30A0, 0x30FF), (0xAC00, 0xD7AF),
)
_FTS5_SPECIAL_RE = re.compile(r"""[+{}():"^@/#&|~\[\]<>,;!?$=\\']""")
_QUOTED_PHRASE_RE = re.compile(r'"[^"]*"')
_QUOTED_PHRASE = re.compile(r'"[^"]*"')
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
    from datetime import UTC, datetime

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
        self._cjk_available = False

    # -- connection --------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=_BUSY_TIMEOUT_MS / 1000)
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
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = self._connect()
            self._local.conn = conn
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
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    # -- schema ------------------------------------------------------------

    def initialize(self) -> None:
        """建表 + 三套 FTS + 触发器（幂等）。"""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._cursor() as conn:
            conn.executescript(_SCHEMA_SQL)
            conn.executescript(_BASE_FTS_SQL)
            conn.executescript(_TRIGRAM_FTS_SQL)
        self._initialize_cjk()

    def _initialize_cjk(self) -> None:
        """CJK 表与**它的**触发器分开建：索引不完整时触发器必须保持缺席。"""
        available = self._load_cjk_extension()
        with self._cursor() as conn:
            if not available:
                # fail-open：摘触发器，写路径继续可用；检索降级 trigram/LIKE。
                for name in _CJK_TRIGGERS:
                    conn.execute(f"DROP TRIGGER IF EXISTS {name}")
                conn.execute(
                    "INSERT INTO state_meta(key, value) VALUES(?, '1') "
                    "ON CONFLICT(key) DO UPDATE SET value='1'",
                    (_FTS_CJK_STALE_KEY,),
                )
                self._cjk_available = False
                logger.warning(
                    "fts5_cjk tokenizer 不可用（%s）；CJK 索引降级为 trigram/LIKE",
                    self.cjk_so_path,
                )
                return
            stale = conn.execute(
                "SELECT value FROM state_meta WHERE key = ?", (_FTS_CJK_STALE_KEY,)
            ).fetchone()
            conn.executescript(_CJK_TABLE_SQL)
            if stale is None:
                conn.executescript(_CJK_TRIGGER_SQL)
            self._cjk_available = stale is None

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
            logger.warning("fts5_cjk 扩展不可用: %s", self.cjk_so_path, exc_info=True)
            return False

    @property
    def cjk_available(self) -> bool:
        return self._cjk_available

    # -- writes ------------------------------------------------------------

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
        **extra: Any,
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
            **{k: v for k, v in extra.items() if v is not None},
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

    def get_messages(self, session_id: str) -> list[dict[str, Any]]:
        """按 `messages.id` 自增序返回全文（READ 形态的真相读取）。"""
        rows = self._conn.execute(
            "SELECT * FROM messages WHERE session_id = ? ORDER BY id", (session_id,)
        ).fetchall()
        return [dict(row) for row in rows]

    def get_session(self, session_id: str) -> dict[str, Any] | None:
        row = self._conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
        return dict(row) if row is not None else None

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
        order = "ORDER BY m.timestamp DESC, rank"
        if sort == "oldest":
            order = "ORDER BY m.timestamp ASC, rank"
        elif sort == "newest":
            order = "ORDER BY m.timestamp DESC, rank"

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
                )
                if rows:
                    return rows
        return rows

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
            )
            if rows:
                return rows
        tokens = _non_operator_tokens(raw)
        if tokens and all(len(t) >= 3 for t in tokens):
            rows = self._match_rows(
                "messages_fts_trigram", match, order=order, limit=limit, offset=offset,
                role_filter=role_filter, source_filter=source_filter,
                exclude_sources=exclude_sources, after_ts=after_ts, before_ts=before_ts,
            )
            if rows:
                return rows
        # 基础索引对 CJK 按单字切词（假阳性多，但能覆盖 tool 行前缀）。
        rows = self._match_rows(
            "messages_fts", match, order=order, limit=limit, offset=offset,
            role_filter=role_filter, source_filter=source_filter,
            exclude_sources=exclude_sources, after_ts=after_ts, before_ts=before_ts,
        )
        if rows:
            return rows
        return self._like_rows(
            raw, limit=limit, offset=offset, role_filter=role_filter,
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

    def _match_rows(
        self, table: str, match: str, *, order: str, limit: int, offset: int, **filters: Any,
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
        return [dict(row) for row in rows]

    def _like_rows(self, raw: str, *, limit: int, offset: int, **filters: Any) -> list[dict[str, Any]]:
        where, params = self._where(**filters)
        terms = [t for t in _non_operator_tokens(raw)] or [raw]
        # 与 FTS 投影保持一致：tool 行只匹配前 N 字符（存储仍是全文）。
        # 否则 LIKE 子串扫描会绕过 8192 上限，把索引外的内容「搜」出来。
        projected = (
            "CASE WHEN m.role = 'tool' "
            f"THEN substr(COALESCE(m.content, ''), 1, {FTS_TOOL_CONTENT_PREFIX_CHARS}) "
            "ELSE m.content END"
        )
        like_clause = " AND ".join(f"{projected} LIKE ? ESCAPE '\\'" for _ in terms)
        sql = f"""
            SELECT m.id, m.session_id, m.role,
                   substr(COALESCE(m.content, ''), 1, 160) AS snippet,
                   m.timestamp, m.tool_name, s.source, s.model,
                   s.started_at AS session_started
            FROM messages AS m
            JOIN sessions AS s ON s.id = m.session_id
            WHERE {where} AND {like_clause}
            ORDER BY m.timestamp DESC
            LIMIT ? OFFSET ?
        """
        escaped = [
            "%" + t.replace("%", "\\%").replace("_", "\\_") + "%" for t in terms
        ]
        try:
            rows = self._conn.execute(sql, (*params, *escaped, limit, offset)).fetchall()
        except sqlite3.OperationalError:
            return []
        return [dict(row) for row in rows]

    # -- session_search（四形态） ------------------------------------------

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

    def _scroll(
        self, session_id: str, around_message_id: int, window: int, current_session_id: str | None,
    ) -> dict[str, Any]:
        try:
            anchor = int(around_message_id)
        except (TypeError, ValueError):
            return {"mode": "scroll", "success": False, "error": "scroll requires integer around_message_id"}
        window = max(1, min(int(window), 20))
        meta = self.get_session(session_id)
        if meta is None:
            return {"mode": "scroll", "success": False, "error": f"session_id not found: {session_id}"}
        anchor_row = self._conn.execute(
            "SELECT session_id FROM messages WHERE id = ?", (anchor,)
        ).fetchone()
        owning = anchor_row["session_id"] if anchor_row is not None else None
        if owning is None:
            return {
                "mode": "scroll", "success": False,
                "error": f"around_message_id {anchor} not found",
            }
        rows = self._conn.execute(
            "SELECT * FROM messages WHERE session_id = ? AND id BETWEEN ? AND ? ORDER BY id",
            (owning, anchor - window, anchor + window),
        ).fetchall()
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
        out: dict[str, Any] = {
            "mode": "scroll",
            "session_id": owning,
            "around_message_id": anchor,
            "session_meta": self._meta_block(meta),
            "window": window,
            "messages": messages,
            "messages_before": before,
            "messages_after": after,
        }
        if current_session_id and self.lineage_root(current_session_id) == self.lineage_root(owning):
            out["note"] = "anchor lives in the current session lineage (already in active context)"
        return out

    def _discover(
        self, query: str, *, limit: int, sort: str | None, after_ts: float | None,
        before_ts: float | None, current_session_id: str | None,
    ) -> dict[str, Any]:
        raw = self.search_messages(
            query, limit=_DISCOVER_SCAN_LIMIT, role_filter=("user", "assistant"),
            exclude_sources=HIDDEN_SESSION_SOURCES, after_ts=after_ts, before_ts=before_ts,
            sort=sort,
        )
        # cron 降权：稳定排序让交互会话永远排在前面（Hermes #19434）。
        raw = sorted(raw, key=lambda r: (r.get("source") or "") in DEMOTED_SESSION_SOURCES)
        current_root = self.lineage_root(current_session_id) if current_session_id else None
        seen: dict[str, dict[str, Any]] = {}
        for row in raw:
            if len(seen) >= limit:
                break
            root = self.lineage_root(row["session_id"])
            if current_root and root == current_root:
                continue
            seen.setdefault(root, {**row, "_lineage_root": root})
        if not raw:
            return {
                "mode": "discover", "query": query, "results": [], "count": 0,
                "message": (
                    "No matching sessions found. FTS5 ANDs all terms by default — "
                    "broaden with OR, exact-match with quoted phrases, or prefix-match with `deploy*`."
                ),
            }
        results = []
        first = True
        for root, match in seen.items():
            entry = self._hydrate(root, match, full=first)
            first = False
            if entry is not None:
                results.append(entry)
        return {"mode": "discover", "query": query, "results": results,
                "count": len(results), "sessions_searched": len(seen)}

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
            entry["parent_session_id"] = lineage_root
        return entry

    @staticmethod
    def _meta_block(meta: dict[str, Any]) -> dict[str, Any]:
        return {
            "when": meta.get("started_at"),
            "source": meta.get("source"),
            "model": meta.get("model"),
            "title": meta.get("title"),
        }


# ----------------------------------------------------------------- writer ---


class SessionArchiveWriter:
    """把 Session 落盘后的**终态消息**写进归档库（独立于事件流）。

    挂在 `Session.add_listener`（事件落盘成功后回调），只认：

    * `user/message` → role=user
    * `model/completed` → role=assistant（`model/delta` 流式 chunk 一律不归档）
    * `tool/result` → role=tool（content 是 ToolResult 的 JSON 全文）

    失败只记 warning，绝不破坏 append 契约。
    """

    _ARCHIVED_EVENTS = frozenset({"user/message", "model/completed", "tool/result"})

    def __init__(self, archive: SessionArchive, session_id: str) -> None:
        self.archive = archive
        self.session_id = session_id
        self._attached = False

    def attach(self, session: Session) -> None:
        if self._attached:
            return
        session.add_listener(self._on_event)
        self._attached = True

    def _on_event(self, event: Any) -> None:
        if event.type not in self._ARCHIVED_EVENTS:
            return
        try:
            self._archive_event(event)
        except Exception:
            logger.warning(
                "会话归档写入失败（session=%s, event=%s）",
                self.session_id, event.type, exc_info=True,
            )

    def _archive_event(self, event: Any) -> None:
        data = event.data or {}
        if event.type == "user/message":
            self.archive.append_message(
                self.session_id, "user", data.get("content"), timestamp=time.time(),
            )
        elif event.type == "model/completed":
            tool_calls = data.get("tool_calls")
            self.archive.append_message(
                self.session_id, "assistant", data.get("content"),
                tool_calls=json.dumps(tool_calls, ensure_ascii=False) if tool_calls else None,
                timestamp=time.time(),
            )
        elif event.type == "tool/result":
            self.archive.append_message(
                self.session_id, "tool", data.get("content"),
                tool_call_id=data.get("tool_call_id"), timestamp=time.time(),
            )


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
    ) -> None:
        # 传路径时**惰性建库**：web 的 `AppState` 构造早于任何会话，若在那里
        # 就落盘 state.db，会破坏「请求被拒时不留痕迹」的既有不变量
        # （tests/session/test_launch_false.py 实测撞红）。只有在**观察到第一条
        # 归档事件**时才建库——那时会话早已真实存在。
        self._archive = archive if isinstance(archive, SessionArchive) else None
        self._archive_path = None if self._archive is not None else archive
        self._cjk_so_path = cjk_so_path
        # 进程内已登记过的会话（省掉每条事件的 upsert）。**只是缓存**：
        # 多进程 / 重启后由 `ensure_session` 的 INSERT..ON CONFLICT 兜底。
        self._known: set[str] = set()
        self._lock = threading.Lock()

    @property
    def archive(self) -> SessionArchive:
        if self._archive is None:
            arc = SessionArchive(self._archive_path, cjk_so_path=self._cjk_so_path)
            arc.initialize()
            self._archive = arc
        return self._archive

    # -- 接线 --------------------------------------------------------------

    def attach(self, store: Any) -> None:
        store.set_event_observer(self)

    # -- 观察者入口 --------------------------------------------------------

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
) -> SessionArchiveObserver:
    """把归档观察者挂到 store 上（#1389 的**唯一**生产接线点）。

    不碰磁盘：建库推迟到第一条归档事件（见 `SessionArchiveObserver.__init__`）。
    建库/写入失败只记 warning——存档是派生检索索引，起不来不能让会话写路径
    跟着不可用。
    """
    observer = SessionArchiveObserver(db_path, cjk_so_path=cjk_so_path)
    observer.attach(store)
    return observer
