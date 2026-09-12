"""Memory 权威记录存取契约。

# 并发与版本语义（#156 MEM-1 冻结，后续三票共同前置）

结论：**内容按 memory_id 在 namespace 内 last-write-wins，不进版本戳**。

- 写入是单条 `BEGIN IMMEDIATE` 事务（SQLite 单写者），同一 id 的并发写被数据库
  串行化 → 后提交的内容获胜，不存在"半个更新"。
- 索引同步的乐观令牌是 outbox 的 `revision`（每次写生成新 uuid）：relay 同步的是哪个
  revision，就只允许 ack 那个 revision（`acknowledge` 按 `memory_id + revision` 匹配，
  匹配不到即放弃）。所以"同步期间内容又被改掉"不会让索引停在被覆盖前的版本上——
  这一轮不 ack，下一轮把新内容同步进去。
- **刻意不加 `updated_at` / `version` 字段**：没有调用方做 read-modify-write 的版本比较
  （"该不该更新、更新哪一条"是冲突消解 #158 的策略，它先检索再写），加了只是装饰；而给
  `MemoryEntry` 加字段会牵动每个 fake/adapter 与 LangMem 的 item 映射。将来若要按"更新
  时间"（而不是 `created_at`）排序，那时再加列 + 迁移，并同步更新本节结论。
"""

from dataclasses import dataclass
from enum import Enum
from typing import Protocol

from agent_harness.identity import IdentityContext
from agent_harness.memory.types import MemoryEntry, MemoryScope


class MemoryOperation(str, Enum):
    """outbox 里一条变更要求索引侧做什么。

    `DELETE` 是**硬删**（用户 2026-09-11 决策）：记录行与向量都真的消失，不留墓碑。
    """

    UPSERT = "upsert"
    DELETE = "delete"


class MemoryRecordStore(Protocol):
    async def store(self, entry: MemoryEntry, identity: IdentityContext) -> str: ...
    async def get(self, memory_id: str, identity: IdentityContext) -> MemoryEntry: ...
    async def list_by_scope(
        self, scope: MemoryScope, identity: IdentityContext, limit: int,
    ) -> list[MemoryEntry]: ...
    async def delete(self, memory_id: str, identity: IdentityContext) -> bool:
        """硬删一条记忆（记录行 + 一条 `DELETE` 索引意图，同事务）。

        返回值 = "确实删掉了一条"：
        - id 不存在 → `False`（**幂等**：忘了又忘不是错误；且没有记录行就没有可路由的
          namespace，无法安全地清理索引，所以也不产生索引意图）；
        - 存在但属于别的 namespace → `PermissionError`（与 `store` 同源：跨
          tenant/user/scope 的删除是权限违规，不是"没有这条"）；
        - SESSION 绑定的记忆要求当前上下文绑定同一 session，否则 `PermissionError`
          （与 `get`/`store` 一致）；完全没有绑定时 `ValueError`。

        "先看行是否存在、再校验归属"这个顺序是有意的：归属校验需要行本身（scope 从行里
        读），而且不存在时必须是幂等 `False`——否则"忘一条已经不存在的记忆"会因为当前
        上下文没有 session 绑定而报错。
        """
        ...


@dataclass(frozen=True)
class PendingMemory:
    """outbox 里的一条待同步变更（**自足**：删除变更没有记录行可依赖）。

    `operation is DELETE` ⟺ `entry is None`。路由事实（identity/scope/session）与
    记录行无关，全部来自 outbox 自己——这是"删掉记录行后变更仍能驱动 relay"的前提
    （旧实现的 `pending()` 以 records JOIN outbox 驱动，记录行一删变更就永远消失）。
    """

    operation: MemoryOperation
    memory_id: str
    identity: IdentityContext
    scope: MemoryScope
    session_id: str | None
    revision: str
    entry: MemoryEntry | None = None

    def __post_init__(self) -> None:
        if self.operation is MemoryOperation.DELETE and self.entry is not None:
            raise ValueError("delete change must not carry an entry")
        if self.operation is MemoryOperation.UPSERT and self.entry is None:
            raise ValueError("upsert change requires an entry")


class MemoryOutbox(Protocol):
    async def pending(self, limit: int = 100, after_id: str = "") -> list[PendingMemory]: ...
    async def acknowledge(self, change: PendingMemory) -> bool: ...
