"""Session metadata Store domain contract.

07 §2 五层逻辑分离中的 Metadata Store：索引、引用、版本、映射、配置状态。
ADR-0004 Round 3 §session_meta 表：session_id / created_at / agent_id /
last_checkpoint_seq / archived（默认 false）。
ADR-0004 Round 5 §Q18 a → a2：archived 标记 + 可选 cleanup(session_id)，
不自动执行——手动/运维触发。

与 CheckpointStore / OperationLedger 共享同一个 SQLite 文件，但保持独立 contract。
PostgreSQL 实现本 Phase 只留 ABC 替换边界，不实装。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Literal

from pydantic import BaseModel


class SessionMeta(BaseModel):
    """一条 session 的 metadata 索引行。

    lineage 三列（Phase 14, ADR-0017 决策 7，NULL = root）：事件流是真相、
    这里是索引——建树查索引（O(1) 组装），事实可从事件流审计重建。origin
    统一建模两类边（fork | delegation），一棵树两个来源。
    """

    session_id: str
    created_at: str
    agent_id: str | None = None
    last_checkpoint_seq: int | None = None
    archived: bool = False
    parent_session_id: str | None = None
    origin: Literal["fork", "delegation"] | None = None
    fork_point_seq: int | None = None


class SessionMetaStore(ABC):
    """Session metadata 的异步持久化边界。

    PostgreSQL 实现只在本 ABC 上形成替换边界，Phase 4 不实装。
    """

    @abstractmethod
    async def initialize(self) -> None:
        """创建 schema（幂等）。"""

    @abstractmethod
    async def upsert(self, meta: SessionMeta) -> SessionMeta:
        """新建或更新一条 session metadata 行；返回持久化后的最新值。"""

    @abstractmethod
    async def get(self, session_id: str) -> SessionMeta | None:
        """读取一条 session metadata；不存在返回 None。"""

    @abstractmethod
    async def list_all(self) -> list[SessionMeta]:
        """全量索引行（lineage 建树与 sessions 列表的读取面，ADR-0017 决策 7）。"""

    @abstractmethod
    async def set_archived(self, session_id: str, archived: bool = True) -> SessionMeta:
        """标记 session 是否 archived；不存在抛 KeyError。"""

    @abstractmethod
    async def update_last_checkpoint_seq(
        self, session_id: str, event_seq: int
    ) -> SessionMeta:
        """刷新 last_checkpoint_seq；不存在抛 KeyError。"""

    @abstractmethod
    async def cleanup(self, session_id: str) -> None:
        """显式删除一条 session metadata 行（不实现自动 TTL）。

        注意：该方法只清理 session_meta 自身的行，不级联清理 checkpoints /
        operations（跨 Store 的清理是后续运维工具的职责，避免一个 Store
        隐式拥有另一个 Store 的写语义）。需要级联清理时由调用方按顺序调用各 Store。
        """
