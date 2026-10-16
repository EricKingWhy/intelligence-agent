"""会话级约束管理面板 HTTP 面（#1190 / 归属 #1176 Phase 1）。

独立 router 文件（与 `lineage.py` / `memory.py` 同款）：`app.py` 注册只经
`create_app` 里的一行调用接入，把冲突面压到最小。

## 契约形状（前端消费）

```
GET  /api/sessions/{session_id}/protected-facts
     → {facts: [{fact_id, type, value, status, source_event_id, source_seq,
                 registered_at, revoked_at, revoked_by}, ...]}      # source_seq 倒序
POST /api/sessions/{session_id}/protected-facts/{fact_id}/revoke
     → {fact_id, status: "revoked", revoked_at}
```

## 两条语义（票面硬约束，PRD §4.2 / issue §五）

1. **数据是后端实时投影**：GET 直接返回 `derive_protected_facts` 的全量投影
   （active/revoked/superseded），不做过滤或加工——前端不维护第二套状态。
2. **撤销走 Runtime 管理写入口、不物理删除**：POST 经 `SessionService.revoke_protected_fact`
   追加 `task/fact_revocation` 事件（历史保留链）；被撤销 fact 的状态由投影派生为
   `revoked`，不等同于被动取代的 `superseded`。

## 来源闸（ADR-0025 D1）

会话约束值属会话内容（宿主侧信息），两个端点都挂 `require_trusted_origin`——
本地信任模式下只接受本机来源（跨源 → 403），配置 `jwt_secret` 时由认证层接管。
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any, Literal

from fastapi import Depends, HTTPException
from pydantic import BaseModel, ConfigDict

from agent_harness.identity import get_identity_context
from agent_harness.session.errors import (
    EventLogCorruptError,
    InvalidSessionId,
    ProtectedFactNotFound,
    ProtectedFactNotRevocable,
    SeqConflict,
    SessionNotFound,
)
from agent_harness.storage.sqlite import StorageBusyError
from agent_harness.web.domain_errors import http_error, storage_http_error
from agent_harness.web.projects import require_trusted_origin

if TYPE_CHECKING:
    from fastapi import FastAPI

#: fact_id 的路径安全形态：`pf-<hex>`（注册事实）与 `<type>:<event_id>`（系统事实）
#: 都落在字母数字 + `-` `_` `:` `.` 内。含 `/` `\` 等路径分隔符即 422（先于 404，
#: 路径穿越防线；与 `SESSION_KEY_PATTERN` 同立场，只是事实 id 允许 `:` 与 `.`）。
#: 长度上限 200 与 `supersedes_fact_id` 的校验同量级。
_FACT_ID_PATTERN = re.compile(r"[A-Za-z0-9_.:-]{1,200}")

#: 面板展示的状态值域钉死三值（issue §四 / §五）。投影只产这三个值
#: （`derive_protected_facts` 的终态分支）；`pending_approval` 是**请求级**状态，
#: 永不进入 fact 的 status（也不在本模型值域内）。
FactStatus = Literal["active", "revoked", "superseded"]


class ProtectedFactRow(BaseModel):
    """单条受保护事实的面板行（原样投影，无二次加工）。"""

    model_config = ConfigDict(extra="forbid")

    fact_id: str
    type: str
    value: Any
    status: FactStatus
    source_event_id: str
    source_seq: int
    #: 来源事件时间（事件流是时间的唯一来源）；来源事件缺失时为 null。
    registered_at: str | None
    #: 主动撤销派生字段（`task/fact_revocation`）；未被主动撤销时为 null。
    revoked_at: str | None
    revoked_by: str | None


class ProtectedFactsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    facts: list[ProtectedFactRow]


class ProtectedFactRevoked(BaseModel):
    """同步撤销回执（issue §五：同步审批在当前请求内完成）。"""

    model_config = ConfigDict(extra="forbid")

    fact_id: str
    status: Literal["revoked"]
    revoked_at: str | None


def register_protected_fact_routes(app: FastAPI) -> None:
    """把约束管理面板路由挂到既有 app（`create_app` 里一行调用的接入面）。"""

    @app.get("/api/sessions/{session_id}/protected-facts")
    async def list_protected_facts(
        session_id: str,
        _: None = Depends(require_trusted_origin),
    ) -> ProtectedFactsResponse:
        """列出本会话全量受保护事实（active/revoked/superseded），按来源 seq 倒序。

        语义：422 → session_id 形态非法；404 → 会话不存在；409 → 事件日志损坏
        （`EventLogCorruptError`，不可重试）；503 → 存储暂忙（磁盘满/锁冲突，可重试）。
        走到 200 即"投影完整"——前端据此渲染，不展示任何缓存副本。
        """
        from agent_harness.web.app import session_service

        service = session_service(app.state.agent)
        try:
            rows = await service.list_protected_facts(session_id)
        except (InvalidSessionId, SessionNotFound, EventLogCorruptError) as error:
            raise http_error(error) from error
        except StorageBusyError as error:
            raise storage_http_error(error) from error
        return ProtectedFactsResponse(
            facts=[ProtectedFactRow(**row) for row in rows]
        )

    @app.post("/api/sessions/{session_id}/protected-facts/{fact_id}/revoke")
    async def revoke_protected_fact(
        session_id: str,
        fact_id: str,
        _: None = Depends(require_trusted_origin),
    ) -> ProtectedFactRevoked:
        """主动撤销一条 active 受保护事实（走会话级管理写入口，不物理删除）。

        语义：422 → session_id / fact_id 形态非法（先于 404，路径穿越防线）；
        404 → 会话或目标 fact 不存在；409 → fact 已被取代（非 active，不可主动撤销）、
        会话日志损坏（`EventLogCorruptError`）或写号与在途 run 竞态（`SeqConflict`）；
        503 → 存储暂忙（可重试）；200 → `{fact_id, status: "revoked", revoked_at}`
        （幂等：重复撤销仍 200）。操作人取当前身份（本地信任模式为 `local`）。
        """
        if not _FACT_ID_PATTERN.fullmatch(fact_id):
            raise HTTPException(
                status_code=422,
                detail=f"fact_id 只接受单个安全名字段：{fact_id!r}",
            )
        from agent_harness.web.app import session_service

        identity = get_identity_context()
        service = session_service(app.state.agent)
        try:
            result = await service.revoke_protected_fact(
                session_id, fact_id, revoked_by=identity.user_id
            )
        except (
            InvalidSessionId,
            SessionNotFound,
            ProtectedFactNotFound,
            ProtectedFactNotRevocable,
            EventLogCorruptError,
            SeqConflict,
        ) as error:
            raise http_error(error) from error
        except StorageBusyError as error:
            raise storage_http_error(error) from error
        return ProtectedFactRevoked(**result)
