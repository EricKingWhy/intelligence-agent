"""MEM-V2-1 的 SQLite 权威存储：记录、版本生命周期与派生索引意图。

# 为什么是新表而不是扩 V1 的表

V1（`memory_records` / `memory_outbox`）由 #156/#158/#159 冻结，且 PRD §6.1 的
信封形状与 V1 的 `MemoryEntry`（generic content+metadata）不兼容：V1 没有
kind / payload / tier / scope / version / status / provenance。就地改造会让
"V2 写入"和"V1 读取"在同一列上语义分叉，而 #297 的 Must Not Do 明确要求
V1 继续可运行。新表让两条路径各自完整，cutover（MEM-V2-7）才是一次干净的替换。

# 权威性（PRD §6.6）

SQLite 是记录、版本、状态与 outbox 的唯一权威；Milvus 是可重建的派生索引。
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
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from uuid import uuid4

import aiosqlite

from agent_harness.memory.v2.types import (
    EvidenceItem,
    MemoryDraftV2,
    MemoryKind,
    MemoryRecordV2,
    MemoryScope,
    MemoryStatus,
    MemoryTier,
    SourceType,
    TrustedMemoryIdentity,
    assert_trusted_identity,
)

logger = logging.getLogger(__name__)

#: 与 V1（`memory/sqlite_record_store.py`）同款：每操作新连接 + busy_timeout，
#: 否则 writeback 与 relay 并发写（BEGIN IMMEDIATE）会立刻抛 "database is locked"。
_BUSY_TIMEOUT_MS = 10_000

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
    indexed BOOLEAN NOT NULL DEFAULT FALSE,
    UNIQUE(root_id, version)
);
CREATE UNIQUE INDEX IF NOT EXISTS memory_v2_one_active
    ON memory_v2_records(root_id) WHERE status = 'active';
CREATE INDEX IF NOT EXISTS memory_v2_owner
    ON memory_v2_records(tenant_id, user_id, scope, project_id);
CREATE TABLE IF NOT EXISTS memory_v2_outbox (
    memory_id TEXT PRIMARY KEY, revision TEXT NOT NULL,
    operation TEXT NOT NULL CHECK (operation IN ('upsert', 'delete')),
    tenant_id TEXT NOT NULL, user_id TEXT NOT NULL,
    scope TEXT NOT NULL, project_id TEXT
);
"""

_COLUMNS = (
    "memory_id, schema_version, root_id, version, kind, tier, scope, tenant_id, user_id, "
    "project_id, content, payload, status, importance, strength, source_type, "
    "source_session_id, source_event_ids, evidence, valid_at, invalidated_at, superseded_by, "
    "created_at, updated_at"
)


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


@asynccontextmanager
async def _connect(database_path: Path):
    connection = await aiosqlite.connect(database_path)
    try:
        connection.row_factory = aiosqlite.Row
        await connection.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
        yield connection
    finally:
        await connection.close()


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


class SqliteMemoryV2Store:
    """V2 权威记录存储。所有写方法都是"记录行 + outbox 意图"单事务。"""

    def __init__(self, database_path: str | Path) -> None:
        self.database_path = Path(database_path)

    async def initialize(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        async with _connect(self.database_path) as connection:
            await connection.execute("PRAGMA journal_mode=WAL")
            await connection.executescript(_SCHEMA)
            await connection.commit()

    # ----------------------------------------------------------------------------------
    # 写路径
    # ----------------------------------------------------------------------------------

    async def create(self, draft: MemoryDraftV2, trusted: TrustedMemoryIdentity) -> MemoryRecordV2:
        """新建一条逻辑记忆（version=1，root_id=自己的 id）。"""
        return await self._insert(draft, trusted, root_id=None, version=1, previous=None)

    async def update(
        self, previous_id: str, draft: MemoryDraftV2, trusted: TrustedMemoryIdentity,
    ) -> MemoryRecordV2:
        """基于 `previous_id` 派生下一版：新 active 版本 + 旧版本置 superseded。

        版本号由**这里**决定（`previous.version + 1`），调用方给不了——R3 要求单调性
        是存储的保证，不是调用方的纪律。
        """
        previous = await self._authorized_row(previous_id, trusted)
        self._require_active(previous, action="update")
        if draft.scope.value != previous["scope"] or draft.project_id != previous["project_id"]:
            # 允许 draft 改写 scope/project 等于允许"把一条 user_global 记忆改成项目记忆"
            # 或反向——两者的可见性集合不同，是越权而不是编辑。
            raise ValueError("update must keep the logical memory's scope and project")
        record = await self._insert(
            draft, trusted, root_id=previous["root_id"], version=previous["version"] + 1,
            previous=previous,
        )
        return record

    async def invalidate(self, memory_id: str, trusted: TrustedMemoryIdentity) -> MemoryRecordV2:
        """把 active 记录置为 invalidated（内容保留，§5.4.2）。"""
        row = await self._authorized_row(memory_id, trusted)
        self._require_active(row, action="invalidate")
        now = _utc_now()
        async with _connect(self.database_path) as connection:
            await connection.execute("BEGIN IMMEDIATE")
            await connection.execute(
                "UPDATE memory_v2_records SET status=?, invalidated_at=?, updated_at=?, indexed=FALSE "
                "WHERE memory_id=?", (MemoryStatus.INVALIDATED.value, now, now, memory_id))
            await self._enqueue(connection, memory_id, MemoryOperationV2.DELETE,
                                row["tenant_id"], row["user_id"], row["scope"], row["project_id"])
            await connection.commit()
        return await self.get(memory_id, trusted)

    async def _insert(
        self, draft: MemoryDraftV2, trusted: TrustedMemoryIdentity, *,
        root_id: str | None, version: int, previous: aiosqlite.Row | None,
    ) -> MemoryRecordV2:
        """插入一条新的 active 版本；`previous` 非空时同事务把它置为 superseded。"""
        memory_id = str(uuid4())
        now = _utc_now()
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
        async with _connect(self.database_path) as connection:
            await connection.execute("BEGIN IMMEDIATE")
            if previous is not None:
                # 先让出 root_id 上的 active 槽位，再插入新版本（部分唯一索引不允许两条 active）。
                await connection.execute(
                    "UPDATE memory_v2_records SET status=?, superseded_by=?, updated_at=?, indexed=FALSE "
                    "WHERE memory_id=?",
                    (MemoryStatus.SUPERSEDED.value, memory_id, now, previous["memory_id"]))
                await self._enqueue(connection, previous["memory_id"], MemoryOperationV2.DELETE,
                                    previous["tenant_id"], previous["user_id"], previous["scope"],
                                    previous["project_id"])
            try:
                await connection.execute(
                    f"INSERT INTO memory_v2_records ({_COLUMNS}) VALUES "
                    "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    self._values(record))
            except aiosqlite.IntegrityError as error:
                await connection.rollback()
                raise ValueError(f"memory record conflicts with an existing row: {error}") from None
            await self._enqueue(connection, memory_id, MemoryOperationV2.UPSERT,
                                trusted.tenant_id, trusted.user_id, record.scope.value,
                                record.project_id)
            await connection.commit()
        return record

    # ----------------------------------------------------------------------------------
    # 读路径
    # ----------------------------------------------------------------------------------

    async def get(self, memory_id: str, trusted: TrustedMemoryIdentity) -> MemoryRecordV2:
        """按 id 取一条。不可见（不存在 / 别的身份 / 别的项目）一律 `KeyError`。

        读路径刻意不区分"不存在"与"不是你的"：区分等于告诉调用方别人的 id 是否存在。
        """
        async with (
            _connect(self.database_path) as connection,
            connection.execute(
                "SELECT * FROM memory_v2_records WHERE memory_id=?", (memory_id,)
            ) as cursor,
        ):
            row = await cursor.fetchone()
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
        """
        if scope is MemoryScope.PROJECT and trusted.project_id is None:
            return []
        async with (
            _connect(self.database_path) as connection,
            connection.execute("""
                SELECT * FROM memory_v2_records
                WHERE tenant_id=? AND user_id=? AND scope=? AND status='active'
                  AND (? IS NULL OR project_id=?)
                ORDER BY created_at DESC, memory_id DESC LIMIT ? OFFSET ?
            """, (trusted.tenant_id, trusted.user_id, scope.value,
                  trusted.project_id, trusted.project_id, max(0, limit), max(0, offset))) as cursor,
        ):
            return [_to_record(row) for row in await cursor.fetchall()]

    async def list_versions(
        self, root_id: str, trusted: TrustedMemoryIdentity,
    ) -> list[MemoryRecordV2]:
        """一个逻辑记忆的全部版本，新→旧（§6.4 的版本历史；不含被删除的内容）。"""
        async with (
            _connect(self.database_path) as connection,
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

    async def pending(self, limit: int = 100, after_id: str = "") -> list[PendingMemoryChangeV2]:
        """relay 的读取入口：以 **outbox 为驱动表**（LEFT JOIN 记录行）。

        路由列（tenant/user/scope/project）与 memory_id 一律取 outbox 自己的值——
        记录行可能已经不在或已改状态，JOIN 只用来取"要索引的内容"。
        """
        async with (
            _connect(self.database_path) as connection,
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

    async def acknowledge(self, change: PendingMemoryChangeV2) -> bool:
        async with _connect(self.database_path) as connection:
            await connection.execute("BEGIN IMMEDIATE")
            cursor = await connection.execute(
                "DELETE FROM memory_v2_outbox WHERE memory_id=? AND revision=?",
                (change.memory_id, change.revision))
            matched = cursor.rowcount == 1
            if matched:
                # 记录行可能已不在（outbox 行的生命周期独立于记录行）→ 命中 0 行，幂等。
                await connection.execute(
                    "UPDATE memory_v2_records SET indexed=TRUE WHERE memory_id=?", (change.memory_id,))
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
        self, memory_id: str, trusted: TrustedMemoryIdentity,
    ) -> aiosqlite.Row:
        async with (
            _connect(self.database_path) as connection,
            connection.execute(
                "SELECT * FROM memory_v2_records WHERE memory_id=?", (memory_id,)
            ) as cursor,
        ):
            row = await cursor.fetchone()
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
    def _values(record: MemoryRecordV2) -> tuple:
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
        )


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
