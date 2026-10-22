"""MEM-V2-1 的 SQLite 权威存储：记录、版本生命周期与派生索引意图。

# 为什么是新表而不是扩 V1 的表

V1（`memory_records` / `memory_outbox`）由 #156/#158/#159 冻结，且 PRD §6.1 的
信封形状与 V1 的 `MemoryEntry`（generic content+metadata）不兼容：V1 没有
kind / payload / tier / scope / version / status / provenance。就地改造会让
"V2 写入"和"V1 读取"在同一列上语义分叉，而 #297 的 Must Not Do 明确要求
V1 继续可运行。新表让两条路径各自完整，cutover（MEM-V2-7）才是一次干净的替换。

# 权威性（PRD §6.6）

SQLite 是记录、版本、状态与 outbox 的唯一权威；派生索引（进程内）可重建。
写入一律"记录行 + outbox 意图"同事务提交，索引由 relay 异步收敛
（见 `memory/v2/index.py`）——所以 `create/update/invalidate` 返回**不代表**
检索已更新。

# 版本与 active 不变量（R3/R4）

- 每个逻辑记忆（`root_id`）有单调递增的 `version`；
- **至多一个 active 版本**，由 SQLite 部分唯一索引 `memory_v2_one_active` 强制，
  而不是靠应用层自觉——并发写时由数据库拒绝，不会出现两条 active；
- `update` 先把旧 active 置为 `superseded`（并记 `superseded_by`）再插入新版本；
- `invalidate` 把 active 置为 `invalidated`，**保留内容**（§5.4.2 要求历史仍可读）。
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum
from pathlib import Path
from uuid import uuid4

import aiosqlite

from agent_harness.memory.v2._sqlite import connect, write_connection
from agent_harness.memory.v2.markdown_store import entry_digest
from agent_harness.memory.v2.types import (
    EvidenceItem,
    MemoryDraftV2,
    MemoryKind,
    MemoryRecordV2,
    MemoryScope,
    MemoryStatus,
    MemoryTier,
    MemoryTombstoneV2,
    SourceType,
    TrustedMemoryIdentity,
    assert_trusted_identity,
)
from agent_harness.storage.sqlite import retry_on_busy

logger = logging.getLogger(__name__)

#: #1353 M6 审批态表。`identity` 是可信身份（tenant/user/project）的规范化 JSON——
#: 审批重放时必须按**同一个**身份取回记录。独立成常量是因为
#: `write_approval.StagedWriteStore.initialize()` 单独使用时也要建它（不必先建 store）。
STAGED_SCHEMA = """
CREATE TABLE IF NOT EXISTS memory_v2_staged (
    id TEXT PRIMARY KEY,
    identity TEXT NOT NULL,
    payload TEXT NOT NULL,
    pinned_entry TEXT,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    claimed_at TEXT
);
CREATE INDEX IF NOT EXISTS memory_v2_staged_identity
    ON memory_v2_staged(identity, status, created_at);
"""

_SCHEMA = """
CREATE TABLE IF NOT EXISTS memory_v2_records (
    memory_id TEXT PRIMARY KEY, schema_version INTEGER NOT NULL,
    root_id TEXT NOT NULL, version INTEGER NOT NULL,
    kind TEXT NOT NULL, tier TEXT NOT NULL, scope TEXT NOT NULL,
    tenant_id TEXT NOT NULL, user_id TEXT NOT NULL, project_id TEXT,
    content TEXT NOT NULL, payload TEXT NOT NULL, status TEXT NOT NULL,
    importance REAL NOT NULL, strength REAL NOT NULL,
    source_type TEXT NOT NULL, source_session_id TEXT,
    source_event_ids TEXT NOT NULL, evidence TEXT NOT NULL,
    valid_at TEXT, invalidated_at TEXT, superseded_by TEXT,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    projection_source TEXT NOT NULL DEFAULT 'service',
    projection_target TEXT,
    UNIQUE(root_id, version)
);
CREATE UNIQUE INDEX IF NOT EXISTS memory_v2_one_active
    ON memory_v2_records(root_id) WHERE status = 'active';
-- #1437 F-06b：投影对账键上的部分唯一索引——**不在这里建**。它引用
-- `projection_source`，而旧库要等 `initialize()` 补列之后才有这一列；写进 `_SCHEMA`
-- 会让 `executescript` 在 ALTER 之前就炸（审查 Corr N1）。建索引的正文与键形状
-- 论证在 `_TOOL_PROJECTION_INDEX`。
CREATE INDEX IF NOT EXISTS memory_v2_owner
    ON memory_v2_records(tenant_id, user_id, scope, project_id);
CREATE TABLE IF NOT EXISTS memory_v2_outbox (
    memory_id TEXT PRIMARY KEY, revision TEXT NOT NULL,
    operation TEXT NOT NULL CHECK (operation IN ('upsert', 'delete')),
    tenant_id TEXT NOT NULL, user_id TEXT NOT NULL,
    scope TEXT NOT NULL, project_id TEXT
);
CREATE TABLE IF NOT EXISTS memory_v2_tombstones (
    memory_id TEXT PRIMARY KEY, root_id TEXT NOT NULL,
    tenant_id TEXT NOT NULL, user_id TEXT NOT NULL,
    scope TEXT NOT NULL, project_id TEXT, version INTEGER,
    deleted_at TEXT NOT NULL, expires_at TEXT NOT NULL,
    deletion_reason TEXT NOT NULL,
    content_hashes TEXT NOT NULL, source_hashes TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS memory_v2_tombstones_owner
    ON memory_v2_tombstones(tenant_id, user_id, scope, project_id, expires_at);
CREATE TABLE IF NOT EXISTS memory_v2_settings (
    tenant_id TEXT NOT NULL, user_id TEXT NOT NULL,
    extraction_enabled INTEGER NOT NULL, recall_enabled INTEGER NOT NULL,
    fallback_enabled INTEGER NOT NULL DEFAULT 1,
    write_approval INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (tenant_id, user_id)
);
-- #1353 M6：审批态（staged）与 job 态（memory_v2_jobs）语义不同，故单列一张表。
-- 建在 store 里而不是 write_approval 里：`memory_v2_staged` 与 records 同库同生命周期，
-- 任何 `SqliteMemoryV2Store.initialize()` 都应得到完整 schema；反过来让 store 反向
-- import 审批模块会造成循环依赖。
""" + STAGED_SCHEMA + """
-- #1437 F-06：write-ahead 待投影账本。"markdown 已提交、记录还不存在"这个状态
-- `memory_v2_outbox` 表达不了（outbox 行恒与记录行同事务写），所以单列一张表。
-- 存 content 全文而不是只存摘要：drain 时 markdown 可能已被 replace/remove，
-- 账本必须记得"当初想投影什么"，再拿它去当前条目里核对。
CREATE TABLE IF NOT EXISTS memory_v2_projection_pending (
    tenant_id TEXT NOT NULL,
    user_id   TEXT NOT NULL,
    target    TEXT NOT NULL CHECK (target IN ('memory','user')),
    content_digest TEXT NOT NULL,
    content   TEXT NOT NULL,
    session_id TEXT,
    event_id  TEXT,
    attempts  INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (tenant_id, user_id, target, content_digest)
);

-- #1353 §5：词法检索腿的 FTS5 虚表（rowid = memory_v2_records.rowid）。
-- 只收 content：`keyword_search` 的语义就是"只搜规范内容"，不搜 payload/evidence。
-- 刻意**不是** `content='memory_v2_records'` 的外部内容表：CJK 需要逐字切分才能命中
-- （见 `_CJK_RUN`），而外部内容表会把原始 content 直接喂给分词器。代价是同步要显式做
-- ——写路径只有 `_insert` / `_delete_root_in` 两处，都在本模块内。
CREATE VIRTUAL TABLE IF NOT EXISTS memory_v2_fts USING fts5(content);
"""

#: #1437 F-06b：投影对账键上的部分唯一索引。键形状 = (tenant, user, content)——
#: **不带 target**：一条工具行可以同时服务 MEMORY.md 与 USER.md 里的同一段文本
#: （`project_entry` 的跨文件去重就是这个语义），带上 target 反而会放行两行。
#: 只约束工具来源的 active 行：服务来源行各有自己的 root/版本链，不该被这条键合并。
#:
#: 不放进 `_SCHEMA`：它引用 `projection_source`，旧库要等 `initialize()` 补完列才有
#: （审查 Corr N1——放 `_SCHEMA` 会让 `executescript` 在建列前抛 `no such column`，
#: 既有部署直接起不来）。同理必须容忍历史重复行（N1b）：P2-2 竞态存在期间建出的库
#: 可能已有两行同文本工具行，建索引会 `UNIQUE constraint failed`，升级即失败。
_TOOL_PROJECTION_INDEX = """
CREATE UNIQUE INDEX IF NOT EXISTS memory_v2_one_tool_projection
    ON memory_v2_records(tenant_id, user_id, content)
    WHERE status = 'active' AND projection_source = 'tool';
"""

#: 降级形态：同一个名字、同一组列，只是不带唯一性。单独拎出来是因为 `DROP` 之后
#: 任何失败路径都要能补回它（审查 Corr C5）。
_TOOL_PROJECTION_PLAIN_INDEX = """
CREATE INDEX IF NOT EXISTS memory_v2_one_tool_projection
    ON memory_v2_records(tenant_id, user_id, content)
    WHERE status = 'active' AND projection_source = 'tool';
"""

#: FTS5 的 `unicode61` 分词器把连续 CJK 当成**一个** token（实测 `用户喜欢冰美式咖啡` 是一个
#: token），于是"咖啡"永远匹配不到。索引侧按**单字**切 CJK、查询侧把词也切成单字短语，
#: 两边就对齐了：`"冰 美 式"` 命中 `冰美式` 且不乱序。英文仍按空白/标点分词，不受影响。
_CJK_RUN = re.compile(r"[\u3400-\u9fff\uf900-\ufaff]+")

_COLUMNS = (
    "memory_id, schema_version, root_id, version, kind, tier, scope, tenant_id, user_id, "
    "project_id, content, payload, status, importance, strength, source_type, "
    "source_session_id, source_event_ids, evidence, valid_at, invalidated_at, superseded_by, "
    "created_at, updated_at, projection_source, projection_target"
)

#: #1437：`projection_target` 的合法取值。待投影账本表上有 CHECK，记录表没有——
#: 给记录表补 CHECK 要重建表（SQLite 不支持 ALTER ADD CONSTRAINT），代价远大于收益，
#: 所以在写入口做同一份校验（`create` / `enqueue_projection` / 带 target 的投影查询）。
_PROJECTION_TARGETS = frozenset({"memory", "user"})


def _require_projection_target(target: str | None) -> None:
    """`projection_target` 只允许两个文件名（服务来源行为 `None`）。"""
    if target is not None and target not in _PROJECTION_TARGETS:
        raise ValueError(f"unknown projection target {target!r}")


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _fts_indexed(content: str) -> str:
    """索引侧文本：CJK 逐字切开，其余原样（见 `_CJK_RUN` 的说明）。"""
    return _CJK_RUN.sub(lambda match: " " + " ".join(match.group()) + " ", content)


def _fts_query(terms: Sequence[str]) -> str | None:
    """把关键词列表折成一条 FTS5 MATCH 表达式；没有可用词时返回 `None`。

    每个词都当**短语**（双引号包裹），所以用户词里的 `AND` / `*` / `-` 不会变成 FTS 语法。
    引号自身按 FTS5 规则双写。CJK 词切成单字短语，与索引侧的分词对齐。
    """
    clauses: list[str] = []
    for term in terms:
        if _CJK_RUN.search(term):
            phrase = " ".join(_CJK_RUN.sub(lambda m: " ".join(m.group()), term).split())
        else:
            phrase = term.replace('"', '""')
        if phrase:
            clauses.append(f'"{phrase}"')
    return " OR ".join(clauses) or None


class MemoryOperationV2(str, Enum):
    """outbox 里一条变更要求索引侧做什么。

    V2 **没有硬删记录行**的入口（删除/tombstone 属 MEM-V2-7），`delete` 指的是
    "把某个 memory_id 从派生索引里移除"——它由 supersede / invalidate 产生，
    因为这些状态不再允许出现在检索结果里（AC5）。
    """

    UPSERT = "upsert"
    DELETE = "delete"


@dataclass(frozen=True, slots=True)
class PendingMemoryChangeV2:
    """outbox 里的一条待同步变更，**自足**（删除变更没有"当前记录行"可依赖）。

    路由事实（tenant/user/scope/project）全部来自 outbox 自己的列：relay 要在
    记录行已改变状态之后仍能正确地把向量删掉，所以不能 JOIN 记录行去做路由。
    """

    operation: MemoryOperationV2
    memory_id: str
    tenant_id: str
    user_id: str
    scope: MemoryScope
    project_id: str | None
    revision: str
    record: MemoryRecordV2 | None = None

    def __post_init__(self) -> None:
        if self.operation is MemoryOperationV2.DELETE and self.record is not None:
            raise ValueError("delete change must not carry a record")
        if self.operation is MemoryOperationV2.UPSERT and self.record is None:
            raise ValueError("upsert change requires a record")


@dataclass(frozen=True, slots=True)
class DeletedMemoryV2:
    """One erased version: routing facts for the vector delete + what the markdown
    mirror needs to remove the entry.

    #1353 B-1：`tier` / `kind` / `content` 必须在这里——markdown 是权威层，删除后
    镜像要用**同一批**行做分拣与定位。放在 receipt 里而不是让 capability 删除前
    再读一次，是因为那次读与删除之间有窗口，且 bulk_delete 一次删多个 root。
    receipt 不序列化、不落日志（web 只读 `memory_id`），所以带上正文不改变任何
    对外契约。
    """

    memory_id: str
    tenant_id: str
    user_id: str
    scope: MemoryScope
    project_id: str | None
    kind: MemoryKind
    tier: MemoryTier
    content: str


@dataclass(frozen=True, slots=True)
class MemoryDeletionReceiptV2:
    """Content-free result for idempotent single or bulk deletion.

    ``content_hashes`` 只在 **tombstone-only** 分支填充：记录行早已不在，markdown 侧
    唯一还能对上的只剩条目正文的哈希（同样不含正文）。删除的正向路径仍走
    ``memories``——那里有原文，不需要哈希。
    """

    deleted: bool
    memories: tuple[DeletedMemoryV2, ...] = ()
    content_hashes: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class MemorySettingsV2:
    extraction_enabled: bool = True
    recall_enabled: bool = True
    #: #1039 / PRD §5.3 第 4 条修订：主端点确定性 provider 类错误是否允许落一次备用。
    fallback_enabled: bool = True
    #: #1353 M6：`memory` 工具的写入是否需要人工审批（默认关 = 直接落盘）。
    write_approval: bool = False


async def _add_column_if_missing(
    connection, table: str, column: str, definition: str,
) -> None:
    """幂等补列（旧库迁移）：`definition` 是完整列定义（含类型与 DEFAULT）。

    `table` / `column` / `definition` 全部是本模块的字面量，不来自输入。
    """
    async with connection.execute(f"PRAGMA table_info({table})") as cursor:
        columns = {row["name"] for row in await cursor.fetchall()}
    if column not in columns:
        await connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


async def _ensure_tool_projection_index(connection) -> None:
    """建 `_TOOL_PROJECTION_INDEX`；撞上历史重复行就降级为普通索引（审查 Corr N1b）。

    重复行只可能来自 P2-2 竞态存在的那段时间，且它们本就是同一个逻辑事实——删掉
    后来的那条不丢信息（markdown 才是权威）。但**不在迁移里删数据**：升级路径不该
    悄悄丢行，降级 + 一条 warning 让人看得见。

    降级态的代价要说清（审查 Std R3-1 / Corr C4）：普通索引不带唯一性，并发写入的
    竞态会原样复现（实测 8 并发 ⇒ 8 行），代码层没有第二道闸——`project_entry` 是
    read-then-write，正是竞态本身。而且 `reproject` **清不掉**还在 markdown 里的重复行
    （它按摘要去重，三条同文本只会看见一条）；只有当这段文本从两个文件里都移除之后，
    每轮 `reproject` 才删掉一条，删空后的**下一次 `initialize`** 才会升级回唯一索引。
    """
    async with connection.execute("PRAGMA index_list(memory_v2_records)") as cursor:
        existing = {row["name"]: bool(row["unique"]) for row in await cursor.fetchall()}
    if existing.get("memory_v2_one_tool_projection") is True:
        return
    if "memory_v2_one_tool_projection" in existing:
        await connection.execute("DROP INDEX IF EXISTS memory_v2_one_tool_projection")
    try:
        await connection.executescript(_TOOL_PROJECTION_INDEX)
    except Exception as error:
        # DROP 已经执行过了：任何失败都先补回普通索引，否则这个库连查询索引都没有，
        # 比降级态更差（审查 Corr C5）。
        await connection.execute(_TOOL_PROJECTION_PLAIN_INDEX)
        if not isinstance(error, aiosqlite.IntegrityError):
            raise
        logger.warning(
            "memory_v2_one_tool_projection could not be created (duplicate tool rows from "
            "an older build); falling back to a non-unique index. Uniqueness is NOT enforced "
            "until the duplicates are gone: remove the duplicated text from both markdown "
            "files, then run a reprojection once per duplicate (each pass deletes one row); "
            "the unique index is restored on the next start after the last one is gone.")


class SqliteMemoryV2Store:
    """V2 权威记录存储。所有写方法都是"记录行 + outbox 意图"单事务。"""

    def __init__(self, database_path: str | Path, *, clock=None) -> None:
        self.database_path = Path(database_path)
        self._clock = clock or (lambda: datetime.now(UTC))

    def _now(self) -> datetime:
        return self._clock().astimezone(UTC)

    async def initialize(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        async with connect(self.database_path) as connection:
            await connection.execute("PRAGMA journal_mode=WAL")
            await connection.executescript(_SCHEMA)
            # Existing content-free tombstones cannot recover their old version ordinal.
            await _add_column_if_missing(
                connection, "memory_v2_tombstones", "version", "INTEGER")
            # #1039：旧行未表达过该开关 ⇒ 落在裁决的默认「开」（DEFAULT 1）。
            await _add_column_if_missing(
                connection, "memory_v2_settings", "fallback_enabled",
                "INTEGER NOT NULL DEFAULT 1")
            # #1353 M6：旧库没有这一列 ⇒ 默认「关」，与既有行为一致（写入直接落盘）。
            await _add_column_if_missing(
                connection, "memory_v2_settings", "write_approval",
                "INTEGER NOT NULL DEFAULT 0")
            # #1437：工具写入的来源标记。旧行自动落 'service'——现存行确实都来自服务路径。
            await _add_column_if_missing(
                connection, "memory_v2_records", "projection_source",
                "TEXT NOT NULL DEFAULT 'service'")
            await _add_column_if_missing(
                connection, "memory_v2_records", "projection_target", "TEXT")
            await _ensure_tool_projection_index(connection)
            # #1353 §5：虚表与记录表行数一致 ⇒ 已同步，跳过全量重扫。
            # ponytail: 行数比对只能发现"少了/多了"（典型是 FTS 建表前就存在的旧库），
            # 发现不了"条数相同但内容被外部改过"；真需要就加一张 fts_version 表，
            # 等这种情形真的出现再说。
            # SELECT→DELETE→INSERT 必须是**一笔**写事务（#1353 审查 P2）：行数比对与
            # 取行若落在事务外，并发写者在两者之间插入的行会从索引里消失，而下一轮行数
            # 比对才勉强补回来。
            await connection.execute("BEGIN IMMEDIATE")
            rows = await (await connection.execute(
                "SELECT rowid, content FROM memory_v2_records")).fetchall()
            indexed = await (await connection.execute(
                "SELECT count(*) AS n FROM memory_v2_fts")).fetchone()
            if indexed is None or indexed["n"] != len(rows):
                await connection.execute("DELETE FROM memory_v2_fts")
                await connection.executemany(
                    "INSERT INTO memory_v2_fts(rowid, content) VALUES (?, ?)",
                    [(row["rowid"], _fts_indexed(row["content"])) for row in rows],
                )
            await connection.commit()

    # ----------------------------------------------------------------------------------
    # 写路径
    # ----------------------------------------------------------------------------------
    #
    # #376-1：这里的每个写方法都自开 `BEGIN IMMEDIATE`（直接，或经 `write_connection`），
    # 挂 `@retry_on_busy`（#515 模式，正本 `storage/sqlite.py`）。重试安全性：`BEGIN
    # IMMEDIATE` 是方法体内的第一条语句，写锁超时只可能发生在它、此时事务未提交，整块重跑
    # 等价于首次执行；拿到写锁后本连接独占写者，后续语句不会再撞 `database is locked`。
    #
    # 借用路径（`create` / `update` / `invalidate` 的 `connection=`）下装饰器是 no-op：
    # 借用者只在"调用方事务已持有写锁"时被传入（唯一借用方 `capability.*_in` 由
    # `jobs.commit_with_outcome` 的 `BEGIN IMMEDIATE` 之内调用）⇒ 同一条连接不可能再超时。
    # 该前提写在测试里（`tests/memory/v2/test_v2_write_retry.py`），不是新契约。

    @retry_on_busy
    async def create(
        self, draft: MemoryDraftV2, trusted: TrustedMemoryIdentity, *,
        connection: aiosqlite.Connection | None = None,
        projection_source: str = "service", projection_target: str | None = None,
    ) -> MemoryRecordV2:
        """新建一条逻辑记忆（version=1，root_id=自己的 id）。

        ``projection_source``/``projection_target`` 是**存储层内部**的来源标记（#1437）：
        只在 SQLite 列上存在，不进 `MemoryRecordV2` 契约。工具写入传
        ``projection_source="tool"`` + 目标文件名，服务写入用默认值。
        """
        _require_projection_target(projection_target)
        return await self._insert(
            draft, trusted, root_id=None, version=1, previous=None, connection=connection,
            projection_source=projection_source, projection_target=projection_target)

    @retry_on_busy
    async def update(
        self, previous_id: str, draft: MemoryDraftV2, trusted: TrustedMemoryIdentity, *,
        connection: aiosqlite.Connection | None = None,
    ) -> MemoryRecordV2:
        """基于 `previous_id` 派生下一版：新 active 版本 + 旧版本置 superseded。

        版本号由**这里**决定（`previous.version + 1`），调用方给不了——R3 要求单调性
        是存储的保证，不是调用方的纪律。

        读与写**不共用**一个自开事务：并发写者各读到同一版 active，然后在 `_insert`
        的写事务里撞上 `memory_v2_one_active` 部分唯一索引。把"至多一条 active"交给
        数据库判定（而不是把读并进写事务去串行化全部写者）既贴合 R3 的原文——不变量在
        数据库层、不在调用方纪律——也不让一个更长的事务放大锁等待。
        借用路径下调用方的事务已经在跑，读自然落在里面（`connection` 透传）。
        """
        previous = await self._authorized_row(previous_id, trusted, connection=connection)
        self._require_active(previous, action="update")
        if (previous["source_type"] == SourceType.USER_EDIT.value
                and draft.source_type is SourceType.AUTOMATIC):
            raise PermissionError("automatic evidence cannot supersede a user-edited memory")
        if draft.scope.value != previous["scope"] or draft.project_id != previous["project_id"]:
            # 允许 draft 改写 scope/project 等于允许"把一条 user_global 记忆改成项目记忆"
            # 或反向——两者的可见性集合不同，是越权而不是编辑。
            raise ValueError("update must keep the logical memory's scope and project")
        return await self._insert(
            draft, trusted, root_id=previous["root_id"], version=previous["version"] + 1,
            previous=previous, connection=connection,
        )

    @retry_on_busy
    async def invalidate(
        self, memory_id: str, trusted: TrustedMemoryIdentity, *,
        connection: aiosqlite.Connection | None = None,
    ) -> MemoryRecordV2:
        """把 active 记录置为 invalidated（内容保留，§5.4.2）。"""
        row = await self._authorized_row(memory_id, trusted, connection=connection)
        self._require_active(row, action="invalidate")
        if row["source_type"] == SourceType.USER_EDIT.value:
            raise PermissionError("user-edited memory cannot be invalidated by automatic evidence")
        now = self._now().isoformat()
        async with write_connection(self.database_path, connection) as conn:
            cursor = await conn.execute(
                "UPDATE memory_v2_records SET status=?, invalidated_at=?, updated_at=? "
                "WHERE memory_id=? AND root_id=? AND version=? AND status=?",
                (MemoryStatus.INVALIDATED.value, now, now, memory_id, row["root_id"],
                 row["version"], MemoryStatus.ACTIVE.value))
            if cursor.rowcount != 1:
                raise ValueError("memory changed or is no longer active")
            await self._enqueue(conn, memory_id, MemoryOperationV2.DELETE,
                                row["tenant_id"], row["user_id"], row["scope"], row["project_id"])
            # 回读走**同一个**连接：借用进来的事务还没提交，另开一条连接在 WAL 下看不到
            # 本次改动，会读回旧状态（调用方据此发事件、报 action）。
            return await self._read_in(conn, memory_id, trusted)

    async def _insert(
        self, draft: MemoryDraftV2, trusted: TrustedMemoryIdentity, *,
        root_id: str | None, version: int, previous: aiosqlite.Row | None,
        connection: aiosqlite.Connection | None = None,
        projection_source: str = "service", projection_target: str | None = None,
    ) -> MemoryRecordV2:
        """插入一条新的 active 版本；`previous` 非空时同事务把它置为 superseded。"""
        memory_id = str(uuid4())
        now = self._now().isoformat()
        record = MemoryRecordV2(
            **draft.model_dump(),
            id=memory_id,
            root_id=root_id or memory_id,
            version=version,
            tenant_id=trusted.tenant_id,
            user_id=trusted.user_id,
            status=MemoryStatus.ACTIVE,
            created_at=now,
            updated_at=now,
        )
        # 身份字段由本方法从可信身份补齐，所以这条断言只在 project_id 上可能失败：
        # 把 A 项目的事实标成 B 项目就是一次越权的跨项目写入，必须在落盘前拒绝。
        assert_trusted_identity(record, trusted)
        async with write_connection(self.database_path, connection) as conn:
            if draft.source_type is SourceType.AUTOMATIC and await self._is_tombstoned_replay(
                conn, draft, trusted,
            ):
                raise ValueError("automatic memory matches a deleted source")
            if previous is not None:
                # 更新必须仍命中最初读取的 active 版本；delete/update 可能在读写间提交。
                cursor = await conn.execute(
                    "UPDATE memory_v2_records SET status=?, superseded_by=?, updated_at=? "
                    "WHERE memory_id=? AND root_id=? AND version=? AND status=?",
                    (MemoryStatus.SUPERSEDED.value, memory_id, now, previous["memory_id"],
                     previous["root_id"], previous["version"], MemoryStatus.ACTIVE.value))
                if cursor.rowcount != 1:
                    raise ValueError("memory changed or is no longer active")
                await self._enqueue(conn, previous["memory_id"], MemoryOperationV2.DELETE,
                                    previous["tenant_id"], previous["user_id"], previous["scope"],
                                    previous["project_id"])
            try:
                await conn.execute(
                    f"INSERT INTO memory_v2_records ({_COLUMNS}) VALUES "
                    "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    self._values(record, projection_source, projection_target))
            except aiosqlite.IntegrityError as error:
                # 不在这里 rollback：自开事务时由 `write_connection` 收尾，借用事务时
                # 回滚是拥有者的事（见 `write_connection` 的说明）。
                raise ValueError(f"memory record conflicts with an existing row: {error}") from None
            # 词法腿同步（#1353 §5）：与记录行同一个事务，读到的永远是已提交的内容。
            await conn.execute(
                "INSERT INTO memory_v2_fts(rowid, content) "
                "SELECT rowid, ? FROM memory_v2_records WHERE memory_id=?",
                (_fts_indexed(record.content), memory_id))
            await self._enqueue(conn, memory_id, MemoryOperationV2.UPSERT,
                                trusted.tenant_id, trusted.user_id, record.scope.value,
                                record.project_id)
        return record

    # ----------------------------------------------------------------------------------
    # 读路径
    # ----------------------------------------------------------------------------------

    async def get(self, memory_id: str, trusted: TrustedMemoryIdentity) -> MemoryRecordV2:
        """按 id 取一条。不可见（不存在 / 别的身份 / 别的项目）一律 `KeyError`。

        读路径刻意不区分"不存在"与"不是你的"：区分等于告诉调用方别人的 id 是否存在。
        """
        async with connect(self.database_path) as connection:
            return await self._read_in(connection, memory_id, trusted)

    async def _read_in(
        self, connection: aiosqlite.Connection, memory_id: str,
        trusted: TrustedMemoryIdentity,
    ) -> MemoryRecordV2:
        """在**给定**连接上读一条（`invalidate` 借事务回读自己的改动时用它）。"""
        row = await _row_in(connection, memory_id)
        if row is None or not self._visible(row, trusted):
            raise KeyError(memory_id)
        return _to_record(row)

    async def list_active(
        self, trusted: TrustedMemoryIdentity, *,
        scope: MemoryScope = MemoryScope.USER_GLOBAL, limit: int, offset: int = 0,
    ) -> list[MemoryRecordV2]:
        """列出**当前可见**的 active 记录（不含 superseded / invalidated → AC5）。

        `project` 作用域在没有可信项目上下文时返回空列表而不是报错：那是
        "这次调用没有项目视野"，不是调用方错误。

        ⚠ 项目过滤**只对 `project` 作用域生效**（`scope <> 'project' OR project_id = ?`）：
        `user_global` 行的 `project_id` 恒为 `NULL`，若把谓词写成"调用方有 project_id 就过滤"，
        则 `NULL = 'project-x'` 求值为 NULL ⇒ 整条谓词非真 ⇒ **user_global 记录整批被静默筛掉**。
        而带项目上下文的调用方照样必须看得见自己的 user_global 记忆（§4.3 / R5）。
        判别性测试：`test_list_active_user_global_is_visible_with_a_project_context`。
        """
        if scope is MemoryScope.PROJECT and trusted.project_id is None:
            return []
        async with (
            connect(self.database_path) as connection,
            connection.execute("""
                SELECT * FROM memory_v2_records
                WHERE tenant_id=? AND user_id=? AND scope=? AND status='active'
                  AND (scope <> 'project' OR project_id = ?)
                ORDER BY created_at DESC, memory_id DESC LIMIT ? OFFSET ?
            """, (trusted.tenant_id, trusted.user_id, scope.value,
                  trusted.project_id, max(0, limit), max(0, offset))) as cursor,
        ):
            return [_to_record(row) for row in await cursor.fetchall()]

    async def list_profiles(
        self, trusted: TrustedMemoryIdentity, *, limit: int = 256,
    ) -> list[MemoryRecordV2]:
        """Read the bounded, active user-global semantic profile candidate set."""
        async with (
            connect(self.database_path) as connection,
            connection.execute("""
                SELECT * FROM memory_v2_records
                WHERE tenant_id=? AND user_id=? AND scope=? AND tier='profile'
                  AND kind='semantic' AND status='active'
                ORDER BY importance DESC, strength DESC, updated_at DESC, memory_id
                LIMIT ?
            """, (trusted.tenant_id, trusted.user_id, MemoryScope.USER_GLOBAL.value,
                  max(0, min(limit, 256)))) as cursor,
        ):
            return [_to_record(row) for row in await cursor.fetchall()]

    async def keyword_search(
        self, terms: Sequence[str], trusted: TrustedMemoryIdentity, *,
        scope: MemoryScope, limit: int,
    ) -> list[MemoryRecordV2]:
        """Return authorized active Collection rows matching any normalized term.

        SQLite is authoritative. The bounded lexical lane deliberately searches only the
        canonical content, never SessionEvent history, payload evidence, or deleted rows.
        The lexical leg is the `memory_v2_fts` virtual table (#1353 §5) instead of a
        `content LIKE '%term%'` full scan.
        """
        safe_terms = list(dict.fromkeys(term for term in terms if term))[:64]
        if not safe_terms or limit <= 0 or (scope is MemoryScope.PROJECT and trusted.project_id is None):
            return []
        match = _fts_query(safe_terms)
        if match is None:
            return []
        params: list[object] = [match, trusted.tenant_id, trusted.user_id, scope.value]
        where = "tenant_id=? AND user_id=? AND scope=? AND tier='collection' AND status='active'"
        if scope is MemoryScope.PROJECT:
            where += " AND project_id=?"
            params.append(trusted.project_id)
        params.append(max(0, min(limit, 128)))
        async with (
            connect(self.database_path) as connection,
            connection.execute(f"""
                SELECT * FROM memory_v2_records
                WHERE rowid IN (SELECT rowid FROM memory_v2_fts WHERE memory_v2_fts MATCH ?)
                  AND {where}
                ORDER BY updated_at DESC, memory_id LIMIT ?
            """, params) as cursor,
        ):
            return [_to_record(row) for row in await cursor.fetchall()]

    async def list_records(
        self, trusted: TrustedMemoryIdentity, *,
        query: str | None = None, kind: MemoryKind | None = None,
        status: MemoryStatus | None = None, scope: MemoryScope | None = None,
        project_id: str | None = None, limit: int = 50, offset: int = 0,
    ) -> list[MemoryRecordV2]:
        """List authorized, non-deleted versions using the governance API filters."""
        if project_id is not None and project_id != trusted.project_id:
            raise PermissionError("project filter is not part of the trusted identity")
        if scope is MemoryScope.PROJECT and trusted.project_id is None:
            return []
        clauses = ["tenant_id=?", "user_id=?", "(scope <> 'project' OR project_id = ?)"]
        values: list[object] = [trusted.tenant_id, trusted.user_id, trusted.project_id]
        if scope is not None:
            clauses.append("scope=?")
            values.append(scope.value)
        if project_id is not None:
            clauses.append("scope='project' AND project_id=?")
            values.append(project_id)
        if kind is not None:
            clauses.append("kind=?")
            values.append(kind.value)
        if status is not None:
            clauses.append("status=?")
            values.append(status.value)
        if query:
            clauses.append(
                "(instr(lower(content), lower(?)) > 0 "
                "OR instr(lower(payload), lower(?)) > 0)"
            )
            values.extend((query, query))
        values.extend((max(0, limit), max(0, offset)))
        async with (
            connect(self.database_path) as connection,
            connection.execute(
                f"SELECT * FROM memory_v2_records WHERE {' AND '.join(clauses)} "
                "ORDER BY created_at DESC, memory_id DESC LIMIT ? OFFSET ?", values,
            ) as cursor,
        ):
            return [_to_record(row) for row in await cursor.fetchall()]

    async def list_tombstones(
        self, trusted: TrustedMemoryIdentity, *, query: str | None = None,
        scope: MemoryScope | None = None, project_id: str | None = None,
        limit: int = 50, offset: int = 0,
    ) -> list[MemoryTombstoneV2]:
        """List only unexpired, authorized tombstone identifiers and deletion times."""
        if project_id is not None and project_id != trusted.project_id:
            raise PermissionError("project filter is not part of the trusted identity")
        if scope is MemoryScope.PROJECT and trusted.project_id is None:
            return []
        clauses = [
            "tenant_id=?", "user_id=?", "(scope <> 'project' OR project_id = ?)",
            "expires_at > ?",
        ]
        values: list[object] = [
            trusted.tenant_id, trusted.user_id, trusted.project_id, self._now().isoformat(),
        ]
        if scope is not None:
            clauses.append("scope=?")
            values.append(scope.value)
        if project_id is not None:
            clauses.append("scope='project' AND project_id=?")
            values.append(project_id)
        if query:
            clauses.append(
                "(instr(lower(memory_id), lower(?)) > 0 "
                "OR instr(lower(root_id), lower(?)) > 0)"
            )
            values.extend((query, query))
        values.extend((max(0, limit), max(0, offset)))
        async with (
            connect(self.database_path) as connection,
            connection.execute(
                f"SELECT memory_id, root_id, scope, project_id, deleted_at "
                f"FROM memory_v2_tombstones WHERE {' AND '.join(clauses)} "
                "ORDER BY deleted_at DESC, memory_id DESC LIMIT ? OFFSET ?", values,
            ) as cursor,
        ):
            return [_to_tombstone(row) for row in await cursor.fetchall()]

    async def get_tombstone(
        self, memory_id: str, trusted: TrustedMemoryIdentity,
    ) -> MemoryTombstoneV2:
        """Read one unexpired tombstone without exposing hashes or deletion reason."""
        async with (
            connect(self.database_path) as connection,
            connection.execute(
                "SELECT memory_id, root_id, tenant_id, user_id, scope, project_id, deleted_at "
                "FROM memory_v2_tombstones WHERE memory_id=? AND expires_at > ?",
                (memory_id, self._now().isoformat()),
            ) as cursor,
        ):
            row = await cursor.fetchone()
        if row is None or not self._tombstone_visible(row, trusted):
            raise KeyError(memory_id)
        return _to_tombstone(row)

    async def list_tombstone_versions(
        self, root_id: str, trusted: TrustedMemoryIdentity,
    ) -> list[MemoryTombstoneV2]:
        """List the content-free deletion markers for one authorized logical memory."""
        async with (
            connect(self.database_path) as connection,
            connection.execute(
                "SELECT memory_id, root_id, scope, project_id, deleted_at "
                "FROM memory_v2_tombstones WHERE root_id=? AND tenant_id=? AND user_id=? "
                "AND (scope <> 'project' OR project_id = ?) AND expires_at > ? "
                "ORDER BY version DESC, deleted_at DESC, memory_id DESC",
                (root_id, trusted.tenant_id, trusted.user_id, trusted.project_id,
                 self._now().isoformat()),
            ) as cursor,
        ):
            rows = await cursor.fetchall()
        if not rows:
            raise KeyError(root_id)
        return [_to_tombstone(row) for row in rows]

    @retry_on_busy
    async def delete(
        self, memory_id: str, trusted: TrustedMemoryIdentity, *,
        reason: str = "user_request",
    ) -> MemoryDeletionReceiptV2:
        """Erase every version of a logical memory and retain only 30-day hashes."""
        async with connect(self.database_path) as connection:
            await connection.execute("BEGIN IMMEDIATE")
            row = await _row_in(connection, memory_id)
            if row is None:
                tombstone = await self._tombstone_in(connection, memory_id)
                if tombstone is None or not self._tombstone_visible(tombstone, trusted):
                    await connection.rollback()
                    raise KeyError(memory_id)
                await connection.commit()
                # #1353 审查 P3：记录行早已不在时，markdown 侧唯一还能对上的线索是
                # 墓碑里的正文哈希（全版本）。不回传它，残留条目没有任何清理入口。
                return MemoryDeletionReceiptV2(
                    deleted=False,
                    content_hashes=tuple(json.loads(tombstone["content_hashes"])),
                )
            if not self._visible(row, trusted):
                await connection.rollback()
                raise KeyError(memory_id)
            receipt = await self._delete_root_in(
                connection, row["root_id"], trusted, reason=reason, now=self._now(),
            )
            await connection.commit()
        return receipt

    @retry_on_busy
    async def bulk_delete(
        self, trusted: TrustedMemoryIdentity, *, kind: MemoryKind | None,
    ) -> list[MemoryDeletionReceiptV2]:
        """Atomically erase all visible logical records, optionally restricted by kind."""
        async with connect(self.database_path) as connection:
            await connection.execute("BEGIN IMMEDIATE")
            clauses = [
                "tenant_id=?", "user_id=?", "(scope <> 'project' OR project_id = ?)",
            ]
            values: list[object] = [trusted.tenant_id, trusted.user_id, trusted.project_id]
            if kind is not None:
                clauses.append("kind=?")
                values.append(kind.value)
            async with connection.execute(
                "SELECT DISTINCT root_id FROM memory_v2_records WHERE "
                + " AND ".join(clauses) + " ORDER BY root_id", values,
            ) as cursor:
                roots = [row["root_id"] for row in await cursor.fetchall()]
            now = self._now()
            receipts = [
                await self._delete_root_in(
                    connection, root_id, trusted, reason="bulk_delete", now=now,
                )
                for root_id in roots
            ]
            await connection.commit()
        return receipts

    async def _delete_root_in(
        self, connection: aiosqlite.Connection, root_id: str,
        trusted: TrustedMemoryIdentity, *, reason: str, now: datetime,
    ) -> MemoryDeletionReceiptV2:
        rows = await self._root_rows(connection, root_id, trusted)
        deleted: list[DeletedMemoryV2] = []
        for item in rows:
            deleted.append(DeletedMemoryV2(
                memory_id=item["memory_id"], tenant_id=item["tenant_id"],
                user_id=item["user_id"], scope=MemoryScope(item["scope"]),
                project_id=item["project_id"], kind=MemoryKind(item["kind"]),
                tier=MemoryTier(item["tier"]), content=item["content"],
            ))
            await self._enqueue(
                connection, item["memory_id"], MemoryOperationV2.DELETE,
                item["tenant_id"], item["user_id"], item["scope"], item["project_id"],
            )
            await connection.execute(
                "INSERT OR REPLACE INTO memory_v2_tombstones "
                "(memory_id, root_id, tenant_id, user_id, scope, project_id, version, deleted_at, "
                "expires_at, deletion_reason, content_hashes, source_hashes) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (item["memory_id"], item["root_id"], item["tenant_id"], item["user_id"],
                 item["scope"], item["project_id"], item["version"], now.isoformat(),
                 (now + timedelta(days=30)).isoformat(), reason,
                 json.dumps([self._digest(item["content"])], separators=(",", ":")),
                 json.dumps([self._digest(value) for value in json.loads(item["source_event_ids"])],
                            separators=(",", ":")))
            )
        await connection.execute(
            "DELETE FROM memory_v2_fts WHERE rowid IN "
            "(SELECT rowid FROM memory_v2_records WHERE root_id=?)", (root_id,))
        await connection.execute("DELETE FROM memory_v2_records WHERE root_id=?", (root_id,))
        return MemoryDeletionReceiptV2(deleted=bool(deleted), memories=tuple(deleted))

    async def get_settings(self, trusted: TrustedMemoryIdentity) -> MemorySettingsV2:
        async with (
            connect(self.database_path) as connection,
            connection.execute(
                "SELECT extraction_enabled, recall_enabled, fallback_enabled, write_approval "
                "FROM memory_v2_settings "
                "WHERE tenant_id=? AND user_id=?", (trusted.tenant_id, trusted.user_id),
            ) as cursor,
        ):
            row = await cursor.fetchone()
        if row is None:
            return MemorySettingsV2()
        return MemorySettingsV2(
            bool(row["extraction_enabled"]), bool(row["recall_enabled"]),
            bool(row["fallback_enabled"]), bool(row["write_approval"]),
        )

    @retry_on_busy
    async def update_settings(
        self, trusted: TrustedMemoryIdentity, *,
        extraction_enabled: bool | None = None, recall_enabled: bool | None = None,
        fallback_enabled: bool | None = None, write_approval: bool | None = None,
    ) -> MemorySettingsV2:
        async with connect(self.database_path) as connection:
            await connection.execute("BEGIN IMMEDIATE")
            async with connection.execute(
                "SELECT extraction_enabled, recall_enabled, fallback_enabled, write_approval "
                "FROM memory_v2_settings "
                "WHERE tenant_id=? AND user_id=?", (trusted.tenant_id, trusted.user_id),
            ) as cursor:
                row = await cursor.fetchone()
            current = MemorySettingsV2() if row is None else MemorySettingsV2(
                bool(row["extraction_enabled"]), bool(row["recall_enabled"]),
                bool(row["fallback_enabled"]), bool(row["write_approval"]),
            )
            extraction = (
                current.extraction_enabled if extraction_enabled is None else extraction_enabled
            )
            recall = current.recall_enabled if recall_enabled is None else recall_enabled
            fallback = (
                current.fallback_enabled if fallback_enabled is None else fallback_enabled
            )
            approval = (
                current.write_approval if write_approval is None else write_approval
            )
            await connection.execute(
                "INSERT INTO memory_v2_settings "
                "(tenant_id, user_id, extraction_enabled, recall_enabled, "
                "fallback_enabled, write_approval, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(tenant_id, user_id) DO UPDATE SET "
                "extraction_enabled=excluded.extraction_enabled, "
                "recall_enabled=excluded.recall_enabled, "
                "fallback_enabled=excluded.fallback_enabled, "
                "write_approval=excluded.write_approval, updated_at=excluded.updated_at",
                (trusted.tenant_id, trusted.user_id, int(extraction), int(recall),
                 int(fallback), int(approval), self._now().isoformat()),
            )
            await connection.commit()
        return MemorySettingsV2(extraction, recall, fallback, approval)

    @retry_on_busy
    async def purge_expired_tombstones(self, *, now: datetime | None = None) -> int:
        """Purge tombstone hashes after 30 days; ``now`` is an injectable test clock."""
        cutoff = (now or self._now()).astimezone(UTC).isoformat()
        async with connect(self.database_path) as connection:
            await connection.execute("BEGIN IMMEDIATE")
            cursor = await connection.execute(
                "DELETE FROM memory_v2_tombstones WHERE expires_at <= ?", (cutoff,),
            )
            count = cursor.rowcount
            await connection.commit()
        return count

    # ----------------------------------------------------------------------------------
    # 投影对账（#1437 F-06 / F-06b）
    # ----------------------------------------------------------------------------------
    #
    # 来源过滤按用途分：`tool_projection_digests` 写死 `projection_source='tool'`（对账
    # 只认工具行，服务行永不参与），`find_active_by_digest` 则**不过滤来源**——投影去重
    # 要把同文本的服务行也算进来（审查 Corr N2）。前者可按 target 收窄，后者**刻意不按
    # target 过滤**（一条工具行可同时服务两个文件）。正文摘要仍在 Python 侧比对：
    # `entry_digest` 是 sha256，SQLite 没有内置函数，
    # 想下推就得另存一列摘要。当前规模（单用户数百条）下这几次扫描是常数级开销，
    # 不值得为此加列 + 回填迁移；真到万级再按 `ponytail` 的口径加 `content_digest`
    # 列与索引（与 `memory_v2_one_tool_projection` 合并建设）。

    @staticmethod
    def _require_projection_target(target: str) -> str:
        """`projection_target` 只允许两个文件名（代码层校验，理由见常量注释）。"""
        _require_projection_target(target)
        return target

    async def tool_projection_digests(
        self, trusted: TrustedMemoryIdentity, target: str | None = None,
    ) -> dict[str, str]:
        """该身份下 active 工具来源行的 ``{正文摘要: 正文}``；``target`` 省略即不分文件。

        `reproject_target` 用它算「派生层已有 vs markdown 现有」的差集；扫描条件写死
        `projection_source='tool'`，所以服务来源行永远不在结果里（非干扰论证第 1 条）。
        带正文是为了让删除侧能直接复用撤回逻辑（它按 content 定位），不必再查一次。

        对账必须**不分 target**：一条工具行可以同时服务两个文件（去重键不带 target），
        按 target 取会让共享行在对不上的那一侧变成看不见的孤儿（审查 Corr N5）。
        """
        sql = ("SELECT content FROM memory_v2_records "
               "WHERE tenant_id=? AND user_id=? AND projection_source='tool' AND status='active'")
        params: list[object] = [trusted.tenant_id, trusted.user_id]
        if target is not None:
            self._require_projection_target(target)
            sql += " AND projection_target=?"
            params.append(target)
        async with (
            connect(self.database_path) as connection,
            connection.execute(sql, params) as cursor,
        ):
            return {self._digest(row["content"]): row["content"]
                    for row in await cursor.fetchall()}

    async def find_active_by_digest(
        self, trusted: TrustedMemoryIdentity, digest: str, *,
        scope: MemoryScope | None = None, project_id: str | None = None,
        tier: MemoryTier | None = None,
    ) -> MemoryRecordV2 | None:
        """按正文摘要找一条 active 行，用于「这条要不要再投影」的去重判定。

        唯一调用方是 `project_entry`：去重域是「**目标可见范围内**的任何来源」（设计
        §5.3 第 2 条，审查 Corr N2）——服务行本身就是这句话的派生态，再建工具行会让
        检索命中两次。撤回不走这里，`_retract` 用 `find_tool_by_digest`（只认工具来源，
        服务行不得被撤回删掉）。

        ``scope``/``project_id``/``tier`` 是**可见性**参数，调用方必须传成与
        `keyword_search` 相同的谓词，否则会拿一条检索永远看不见的行去抑制投影：
        工具行恒为 `user_global` + `collection`，所以 PROJECT 行不该挡 `USER_GLOBAL`
        （审查 Corr P2-3），`profile` 行也不该挡 `collection`（审查 Corr C1：它满足
        scope 判定但被 `keyword_search` 的 `tier='collection'` 硬过滤，抑制了就等于
        这条文本在派生层彻底检索不到）。

        不按 target 过滤：一条工具行可以同时服务两个文件（见 `project_entry` 的
        跨文件去重语义与 `memory_v2_one_tool_projection` 的键形状）。
        """
        sql = ("SELECT * FROM memory_v2_records "
               "WHERE tenant_id=? AND user_id=? AND status='active'")
        params: list[object] = [trusted.tenant_id, trusted.user_id]
        if scope is not None:
            sql += " AND scope=?"
            params.append(scope.value)
            if scope is MemoryScope.PROJECT:
                sql += " AND project_id=?"
                params.append(project_id)
        if tier is not None:
            sql += " AND tier=?"
            params.append(tier.value)
        async with (
            connect(self.database_path) as connection,
            connection.execute(sql, params) as cursor,
        ):
            rows = await cursor.fetchall()
        for row in rows:
            if self._digest(row["content"]) == digest:
                return _to_record(row)
        return None

    async def find_tool_by_digest(
        self, trusted: TrustedMemoryIdentity, digest: str,
    ) -> MemoryRecordV2 | None:
        """按正文摘要找一条 active **工具来源**行（任一 target）——撤回用。

        服务来源行即使内容相同也不返回：删它会连带删掉一条独立的逻辑记忆。
        """
        async with (
            connect(self.database_path) as connection,
            connection.execute(
                "SELECT * FROM memory_v2_records "
                "WHERE tenant_id=? AND user_id=? AND projection_source='tool' "
                "AND status='active'",
                (trusted.tenant_id, trusted.user_id),
            ) as cursor,
        ):
            rows = await cursor.fetchall()
        for row in rows:
            if self._digest(row["content"]) == digest:
                return _to_record(row)
        return None

    @retry_on_busy
    async def enqueue_projection(
        self, trusted: TrustedMemoryIdentity, target: str, content: str, *,
        session_id: str | None = None, event_id: str | None = None,
    ) -> None:
        """写一行待投影账本（`ON CONFLICT DO UPDATE`：保留已有 `attempts`/`last_error`）。"""
        self._require_projection_target(target)
        async with connect(self.database_path) as connection:
            await connection.execute("BEGIN IMMEDIATE")
            await connection.execute(
                "INSERT INTO memory_v2_projection_pending "
                "(tenant_id, user_id, target, content_digest, content, session_id, event_id, "
                "attempts, last_error, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 0, NULL, ?) "
                "ON CONFLICT(tenant_id, user_id, target, content_digest) DO UPDATE SET "
                "content=excluded.content, session_id=excluded.session_id, "
                "event_id=excluded.event_id, updated_at=excluded.updated_at",
                (trusted.tenant_id, trusted.user_id, target, self._digest(content), content,
                 session_id, event_id, self._now().isoformat()),
            )
            await connection.commit()

    async def pending_projections(
        self, trusted: TrustedMemoryIdentity,
    ) -> list[dict[str, object]]:
        async with (
            connect(self.database_path) as connection,
            connection.execute(
                "SELECT target, content_digest, content, session_id, event_id, attempts "
                "FROM memory_v2_projection_pending WHERE tenant_id=? AND user_id=? "
                "ORDER BY updated_at, target, content_digest",
                (trusted.tenant_id, trusted.user_id),
            ) as cursor,
        ):
            return [dict(row) for row in await cursor.fetchall()]

    @retry_on_busy
    async def clear_projection(
        self, trusted: TrustedMemoryIdentity, target: str, digest: str,
    ) -> None:
        async with connect(self.database_path) as connection:
            await connection.execute("BEGIN IMMEDIATE")
            await connection.execute(
                "DELETE FROM memory_v2_projection_pending "
                "WHERE tenant_id=? AND user_id=? AND target=? AND content_digest=?",
                (trusted.tenant_id, trusted.user_id, target, digest),
            )
            await connection.commit()

    @retry_on_busy
    async def record_projection_failure(
        self, trusted: TrustedMemoryIdentity, target: str, digest: str, error_type: str,
    ) -> None:
        """失败留痕：只记**异常类型名**，不记内容（与 `memory_tool` 的告警同口径）。"""
        async with connect(self.database_path) as connection:
            await connection.execute("BEGIN IMMEDIATE")
            await connection.execute(
                "UPDATE memory_v2_projection_pending "
                "SET attempts=attempts+1, last_error=?, updated_at=? "
                "WHERE tenant_id=? AND user_id=? AND target=? AND content_digest=?",
                (error_type, self._now().isoformat(), trusted.tenant_id, trusted.user_id,
                 target, digest),
            )
            await connection.commit()

    async def _root_rows(
        self, connection: aiosqlite.Connection, root_id: str,
        trusted: TrustedMemoryIdentity,
    ) -> list[aiosqlite.Row]:
        async with connection.execute(
            "SELECT * FROM memory_v2_records WHERE root_id=? ORDER BY version", (root_id,),
        ) as cursor:
            rows = await cursor.fetchall()
        return [item for item in rows if self._visible(item, trusted)]

    async def _is_tombstoned_replay(
        self, connection: aiosqlite.Connection, draft: MemoryDraftV2,
        trusted: TrustedMemoryIdentity,
    ) -> bool:
        now = self._now().isoformat()
        async with connection.execute(
            "SELECT content_hashes, source_hashes FROM memory_v2_tombstones "
            "WHERE tenant_id=? AND user_id=? AND scope=? AND project_id IS ? AND expires_at > ?",
            (trusted.tenant_id, trusted.user_id, draft.scope.value, draft.project_id, now),
        ) as cursor:
            rows = await cursor.fetchall()
        sources = {self._digest(value) for value in draft.source_event_ids}
        content_hash = self._digest(draft.content)
        return any(
            content_hash in json.loads(row["content_hashes"])
            or bool(sources.intersection(json.loads(row["source_hashes"])))
            for row in rows
        )

    @staticmethod
    def _digest(value: str) -> str:
        return entry_digest(value)

    @staticmethod
    async def _tombstone_in(
        connection: aiosqlite.Connection, memory_id: str,
    ) -> aiosqlite.Row | None:
        async with connection.execute(
            "SELECT * FROM memory_v2_tombstones WHERE memory_id=?", (memory_id,),
        ) as cursor:
            return await cursor.fetchone()

    @staticmethod
    def _tombstone_visible(row: aiosqlite.Row, trusted: TrustedMemoryIdentity) -> bool:
        return (
            row["tenant_id"] == trusted.tenant_id
            and row["user_id"] == trusted.user_id
            and (row["scope"] != MemoryScope.PROJECT.value
                 or row["project_id"] == trusted.project_id)
        )

    async def list_versions(
        self, root_id: str, trusted: TrustedMemoryIdentity,
    ) -> list[MemoryRecordV2]:
        """一个逻辑记忆的全部版本，新→旧（§6.4 的版本历史；不含被删除的内容）。"""
        async with (
            connect(self.database_path) as connection,
            connection.execute(
                "SELECT * FROM memory_v2_records WHERE root_id=? ORDER BY version DESC", (root_id,)
            ) as cursor,
        ):
            rows = await cursor.fetchall()
        if not rows or not self._visible(rows[0], trusted):
            raise KeyError(root_id)
        return [_to_record(row) for row in rows]

    # ----------------------------------------------------------------------------------
    # outbox（relay 专用，不暴露给模型/请求）
    # ----------------------------------------------------------------------------------

    @retry_on_busy
    async def enqueue_active_index_rebuild(self, *, page_size: int = 500) -> int:
        """Requeue every authoritative active row for an operator-led index rebuild.

        This is intentionally not part of the user-facing capability. It reads routing
        columns only and atomically records the current desired state in the durable outbox.
        The caller must hold the workspace instance lock while the derived index is rebuilt.
        """
        if page_size <= 0:
            raise ValueError("page_size must be positive")
        count = 0
        after_id = ""
        async with write_connection(self.database_path) as connection:
            while True:
                async with connection.execute(
                    "SELECT memory_id, tenant_id, user_id, scope, project_id "
                    "FROM memory_v2_records WHERE status=? AND memory_id>? "
                    "ORDER BY memory_id LIMIT ?",
                    (MemoryStatus.ACTIVE.value, after_id, page_size),
                ) as cursor:
                    rows = await cursor.fetchall()
                if not rows:
                    break
                for row in rows:
                    await self._enqueue(
                        connection, row["memory_id"], MemoryOperationV2.UPSERT,
                        row["tenant_id"], row["user_id"], row["scope"], row["project_id"],
                    )
                count += len(rows)
                after_id = rows[-1]["memory_id"]
        return count

    async def pending(self, limit: int = 100, after_id: str = "") -> list[PendingMemoryChangeV2]:
        """relay 的读取入口：以 **outbox 为驱动表**（LEFT JOIN 记录行）。

        路由列（tenant/user/scope/project）与 memory_id 一律取 outbox 自己的值——
        记录行可能已经不在或已改状态，JOIN 只用来取"要索引的内容"。
        """
        async with (
            connect(self.database_path) as connection,
            connection.execute("""
                SELECT o.memory_id AS memory_id, o.revision AS revision, o.operation AS operation,
                       o.tenant_id AS tenant_id, o.user_id AS user_id, o.scope AS scope,
                       o.project_id AS project_id,
                       r.schema_version AS schema_version, r.root_id AS root_id, r.version AS version,
                       r.kind AS kind, r.tier AS tier, r.content AS content, r.payload AS payload,
                       r.status AS status, r.importance AS importance, r.strength AS strength,
                       r.source_type AS source_type, r.source_session_id AS source_session_id,
                       r.source_event_ids AS source_event_ids, r.evidence AS evidence,
                       r.valid_at AS valid_at, r.invalidated_at AS invalidated_at,
                       r.superseded_by AS superseded_by, r.created_at AS created_at,
                       r.updated_at AS updated_at
                FROM memory_v2_outbox o LEFT JOIN memory_v2_records r ON r.memory_id = o.memory_id
                WHERE o.memory_id > ? ORDER BY o.memory_id LIMIT ?
            """, (after_id, max(0, limit))) as cursor,
        ):
            rows = await cursor.fetchall()
        return [self._change(row) for row in rows]

    @retry_on_busy
    async def acknowledge(self, change: PendingMemoryChangeV2) -> bool:
        """按 `revision` 原子确认一条变更已收敛；匹配到才返回 `True`（重放第二次即 `False`）。

        **outbox 行自身就是"还没收敛"的标记**：删掉它 = 收敛完成。这里刻意不在记录行上
        再写一个"已索引"列——那个标记没有任何读者，而多一份状态就多一处可能与 outbox 不一致。
        """
        async with connect(self.database_path) as connection:
            await connection.execute("BEGIN IMMEDIATE")
            cursor = await connection.execute(
                "DELETE FROM memory_v2_outbox WHERE memory_id=? AND revision=?",
                (change.memory_id, change.revision))
            matched = cursor.rowcount == 1
            await connection.commit()
        return matched

    @staticmethod
    async def _enqueue(connection, memory_id: str, operation: MemoryOperationV2,
                       tenant_id: str, user_id: str, scope: str, project_id: str | None) -> None:
        """outbox 每个 memory_id 一行：表达"该 id 的索引期望状态"。

        同 id 后来的写覆盖前一版本（含 upsert↔delete 互相覆盖）——索引只需收敛到
        最新期望；`revision` 变化同时是 relay 重试预算的重置信号。
        """
        await connection.execute("""
            INSERT INTO memory_v2_outbox
                (memory_id, revision, operation, tenant_id, user_id, scope, project_id)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(memory_id) DO UPDATE SET revision=excluded.revision,
                operation=excluded.operation, tenant_id=excluded.tenant_id,
                user_id=excluded.user_id, scope=excluded.scope, project_id=excluded.project_id
        """, (memory_id, str(uuid4()), operation.value, tenant_id, user_id, scope, project_id))

    @staticmethod
    def _change(row: aiosqlite.Row) -> PendingMemoryChangeV2:
        operation = MemoryOperationV2(row["operation"])
        record = _to_record(row) if operation is MemoryOperationV2.UPSERT and row["status"] is not None else None
        if operation is MemoryOperationV2.UPSERT and record is None:
            # 记录行已经不在（不可能来自本模块的路径，但磁盘上的状态不保证合法）：
            # 期望状态按"索引里不该有这条"处理，否则索引会停在一个无人认领的向量上。
            logger.warning("Memory V2 outbox entry %s has no record row; treating as delete",
                           row["memory_id"])
            operation = MemoryOperationV2.DELETE
        return PendingMemoryChangeV2(
            operation=operation, memory_id=row["memory_id"], tenant_id=row["tenant_id"],
            user_id=row["user_id"], scope=MemoryScope(row["scope"]),
            project_id=row["project_id"], revision=str(row["revision"]), record=record)

    # ----------------------------------------------------------------------------------
    # 授权
    # ----------------------------------------------------------------------------------

    @staticmethod
    def _visible(row: aiosqlite.Row, trusted: TrustedMemoryIdentity) -> bool:
        if row["tenant_id"] != trusted.tenant_id or row["user_id"] != trusted.user_id:
            return False
        if MemoryScope(row["scope"]) is MemoryScope.PROJECT:
            return row["project_id"] is not None and row["project_id"] == trusted.project_id
        return True

    async def _authorized_row(
        self, memory_id: str, trusted: TrustedMemoryIdentity, *,
        connection: aiosqlite.Connection | None = None,
    ) -> aiosqlite.Row:
        """取一条并核对归属；给了 `connection` 就在**借来的事务**里读（`update` 用它）。"""
        if connection is not None:
            row = await _row_in(connection, memory_id)
        else:
            async with connect(self.database_path) as owned:
                row = await _row_in(owned, memory_id)
        if row is None:
            raise KeyError(memory_id)
        if not self._visible(row, trusted):
            # 写路径如实拒绝（入口层据此给 403），与读路径的"伪装成不存在"刻意不同。
            raise PermissionError("Memory record belongs to a different identity")
        return row

    @staticmethod
    def _require_active(row: aiosqlite.Row, *, action: str) -> None:
        if row["status"] != MemoryStatus.ACTIVE.value:
            raise ValueError(f"cannot {action} a {row['status']} memory record")

    @staticmethod
    def _values(
        record: MemoryRecordV2, projection_source: str = "service",
        projection_target: str | None = None,
    ) -> tuple:
        return (
            record.id, record.schema_version, record.root_id, record.version,
            record.kind.value, record.tier.value, record.scope.value,
            record.tenant_id, record.user_id, record.project_id,
            record.content, record.payload.model_dump_json(), record.status.value,
            record.importance, record.strength, record.source_type.value,
            record.source_session_id, json.dumps(record.source_event_ids, ensure_ascii=False),
            json.dumps([item.model_dump() for item in record.evidence], ensure_ascii=False),
            record.valid_at, record.invalidated_at, record.superseded_by,
            record.created_at, record.updated_at,
            projection_source, projection_target,
        )


async def _row_in(connection: aiosqlite.Connection, memory_id: str) -> aiosqlite.Row | None:
    """按 id 读一行；读不到返回 `None`——授权判定是调用方的事，不是这一行的事。"""
    async with connection.execute(
        "SELECT * FROM memory_v2_records WHERE memory_id=?", (memory_id,)
    ) as cursor:
        return await cursor.fetchone()


def _to_record(row: aiosqlite.Row) -> MemoryRecordV2:
    return MemoryRecordV2(
        id=row["memory_id"],
        schema_version=row["schema_version"],
        root_id=row["root_id"],
        version=row["version"],
        kind=MemoryKind(row["kind"]),
        tier=MemoryTier(row["tier"]),
        scope=MemoryScope(row["scope"]),
        tenant_id=row["tenant_id"],
        user_id=row["user_id"],
        project_id=row["project_id"],
        content=row["content"],
        payload=json.loads(row["payload"]),
        status=MemoryStatus(row["status"]),
        importance=row["importance"],
        strength=row["strength"],
        source_type=SourceType(row["source_type"]),
        source_session_id=row["source_session_id"],
        source_event_ids=json.loads(row["source_event_ids"]),
        evidence=[EvidenceItem(**item) for item in json.loads(row["evidence"])],
        valid_at=row["valid_at"],
        invalidated_at=row["invalidated_at"],
        superseded_by=row["superseded_by"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _to_tombstone(row: aiosqlite.Row) -> MemoryTombstoneV2:
    return MemoryTombstoneV2(
        id=row["memory_id"],
        root_id=row["root_id"],
        scope=MemoryScope(row["scope"]),
        project_id=row["project_id"],
        deleted_at=row["deleted_at"],
    )
