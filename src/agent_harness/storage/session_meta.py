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
    #: #1191（Phase 2，票面 §三.3）：会话工作目录的 git 仓库根（`project_key`）。
    #: 记录后不再重解析；非 git 目录 / 未补写 = None（读取侧回退到按 cwd 现场解析）。
    project_key: str | None = None
    #: #1333（2026-10-20 已批准方案）：创建请求的关联值（前端每次 POST 生成
    #: UUID v4）。认领查询按它**精确匹配**回本请求创建的会话——20s 差分窗口
    #: 内「别的客户端/上一轮迟到」的会话不再可能被误认。写一次性事实
    #: （同 `project_key`：既有调用方的不带该字段全行 upsert 不得抹掉它）；
    #: NULL = 旧客户端/存量行。只进 Metadata Store 索引，不进 SessionEvent
    #: （关联属于索引，不属于会话真相）。
    client_request_id: str | None = None


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
    async def get_by_client_request_id(self, client_request_id: str) -> list[SessionMeta]:
        """#1333：按创建请求关联值**精确匹配**查会话行（全等，非前缀/子串）。

        返回全部命中行（可能 > 1——同一关联值多条 = 数据面异常，由调用方
        fail-closed 处理，本方法不挑选）。NULL 行永不命中（SQL 语义）；
        查询无结果返回 `[]`。走 `idx_session_meta_client_request_id` 索引。
        """

    @abstractmethod
    async def set_archived(self, session_id: str, archived: bool = True) -> SessionMeta:
        """标记 session 是否 archived；不存在抛 KeyError。"""

    @abstractmethod
    async def update_last_checkpoint_seq(
        self, session_id: str, event_seq: int
    ) -> SessionMeta:
        """刷新 last_checkpoint_seq；不存在抛 KeyError。"""

    @abstractmethod
    async def set_project_key(self, session_id: str, project_key: str) -> SessionMeta:
        """写入会话的 `project_key`（#1191 §三.3 的惰性补写通道）；不存在抛 KeyError。

        单独一个方法（不复用 `upsert`）的原因：`upsert` 的 `ON CONFLICT DO UPDATE`
        不写 `project_key`——否则既有调用方（不带该字段的全行 upsert）会把已补写的
        值静默抹成 NULL。本方法只更新这一列。
        """

    @abstractmethod
    async def clear_delegation_parent(self, parent_session_id: str) -> int:
        """清掉"父已被删"的**委派**子行的父链接，返回修复的行数（#172 / ADR-0029 D5）。

        硬删父会话时必须做这一步：委派子会话**不阻止**父被删（D5），但子行的
        `parent_session_id` 会继续指着一个已经不存在的会话——`build_lineage_tree`
        会把它渲染成 `(parent missing)` 的**悬空链接**，而这条边的事实已随父日志一起
        消失、**永远无法自愈**（`lineage._scan_edges` 只从父日志的 `delegation-started`
        事件推导边，父日志没了就再也推不出来）。清掉父链接即让它回到"根"这个诚实状态。

        **只动 `origin='delegation'` 的行**：fork 子会话在删除前已被 409 拒绝，不该
        出现 origin='fork' 的孤儿行；不碰其它 origin 的既有语义。

        幂等：没有匹配行 → 0、不抛错（重跑即自愈，ADR-0029 D3）。
        """

    @abstractmethod
    async def cleanup(self, session_id: str) -> None:
        """显式删除一条 session metadata 行（不实现自动 TTL）。

        注意：该方法只清理 session_meta 自身的行，不级联清理 checkpoints /
        operations（跨 Store 的清理是后续运维工具的职责，避免一个 Store
        隐式拥有另一个 Store 的写语义）。需要级联清理时由调用方按顺序调用各 Store。
        """
