"""W-10（#354）：单目录写入租约的 minimal REST 查询/命令面（独立 router）。

票面范围「任务创建/释放 API」：acquire（创建/升级时取得，或排队）+ release
（用户接受/归档/显式释放）+ 排队撤销 + 状态查询。错误码口径与 task_delivery
同型（spec 11 §6.1）：**形状非法 422、冲突 409**——路径不能安全规范化
（fail-closed）是"形状"问题 → 422；父子相交、已持有其他目录是"与既有
租约/状态对不上" → 409。全部判定在任何 model/tool 工作之前，
被拒请求零副作用。

**写意图判定刻意不在这里**：`read_write_intent` 是自由文本（存在"只读 …"
这类值），自动判写会让只读任务占写锁（违反票面"只读 Task 不占租约"）。
创建入口（W-23）按用户明确的写意图调用 acquire；只读任务想写时本面即
升级路径。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

import anyio
from fastapi import HTTPException
from pydantic import BaseModel

from agent_harness.session.cwd import session_cwd
from agent_harness.session.errors import (
    InvalidSessionId,
    SessionNotFound,
)
from agent_harness.web.domain_errors import http_error
from agent_harness.workspace.lease import (
    LeaseAlreadyHeldElsewhere,
    LeaseError,
    LeasePathConflict,
    LeaseQueuedElsewhere,
)
from agent_harness.workspace.lease_paths import LeasePathError

if TYPE_CHECKING:
    from fastapi import FastAPI


class LeaseAcquireRequest(BaseModel):
    """省略 `path` 时用会话 cwd（session/started 单源锚）；显式给 path 是
    "用户另选独立目录"流程（PRD §4.1），授权边界与项目注册同型：本地单用户
    显式动作。"""

    path: str | None = None


def _lease_http_error(e: Exception) -> None:
    """租约域错误 → HTTP（422 fail-closed / 409 冲突；LeaseError 漏网 → 500 前先炸）。"""
    if isinstance(e, (LeasePathError, LeasePathConflict)):
        raise HTTPException(
            status_code=422 if isinstance(e, LeasePathError) else 409, detail=str(e)
        ) from e
    if isinstance(e, (LeaseAlreadyHeldElsewhere, LeaseQueuedElsewhere)):
        raise HTTPException(status_code=409, detail=str(e)) from e
    raise e  # 未知域错误不吞：宁可 500 也不过继语义


def register_task_lease_routes(
    app: FastAPI, *, validate_session_id: Callable[[str], str]
) -> None:
    """把 task 租约路由挂到既有 app（app.py 侧一行调用的接入面）。"""

    def _manager():
        """租约管理器（ensure_stores 已保证表存在后的取用点）。"""
        return app.state.agent.lease_manager

    async def _session_cwd(session_id: str) -> str | None:
        """会话 cwd 单源锚（WS-1：session/started 的 cwd 字段，不读任务定义）。

        与 `SessionService.task_state` 同一读取形态：`anyio.to_thread` 包同步
        `read_events`，空事件流 = 会话不存在（404，不伪装）。
        """
        state = app.state.agent
        await state.ensure_stores()  # 租约表 DDL + 重启对账的惰性入口
        events = await anyio.to_thread.run_sync(state.store.read_events, session_id)
        if not events:
            raise http_error(SessionNotFound(f"session '{session_id}' not found"))
        return session_cwd(events)

    @app.get("/api/sessions/{session_id}/task/lease")
    async def get_lease_status(session_id: str) -> dict:
        """本 Task 的租约视角：持有（目录）或排队（位置），可同时为无。"""
        try:
            validate_session_id(session_id)
        except InvalidSessionId as e:
            raise http_error(e) from e
        await app.state.agent.ensure_stores()  # 租约表 DDL + 重启对账的惰性入口
        status = await _manager().status(session_id)
        return {
            "lease": {
                "held": (
                    {"dir_key": status.held_dir_key, "dir_path": status.held_dir_path}
                    if status.held_dir_key is not None
                    else None
                ),
                "queued": (
                    {
                        "dir_key": status.queued_dir_key,
                        "position": status.queue_position,
                    }
                    if status.queued_dir_key is not None
                    else None
                ),
            }
        }

    @app.post("/api/sessions/{session_id}/task/lease/acquire")
    async def acquire_lease(session_id: str, req: LeaseAcquireRequest) -> dict:
        """取得（或排队等待）目录写入租约；granted=False 即不得启动写任务。"""
        try:
            validate_session_id(session_id)
        except InvalidSessionId as e:
            raise http_error(e) from e
        path = req.path or await _session_cwd(session_id)
        if not path:
            raise HTTPException(
                status_code=422,
                detail="session 无工作目录（cwd），且请求未指定 path",
            )
        await app.state.agent.ensure_stores()
        try:
            outcome = await _manager().acquire(session_id, path)
        except (LeaseError, LeasePathError) as e:
            _lease_http_error(e)
        return {
            "granted": outcome.granted,
            "dir_key": outcome.dir_key,
            "holder": outcome.holder,
            "queue_position": outcome.position,
        }

    @app.post("/api/sessions/{session_id}/task/lease/release")
    async def release_lease(session_id: str) -> dict:
        """显式释放（幂等：非持有者 released=false）；释放与队首原子选出同事务。"""
        try:
            validate_session_id(session_id)
        except InvalidSessionId as e:
            raise http_error(e) from e
        await app.state.agent.ensure_stores()
        outcome = await _manager().release(session_id)
        return {"released": outcome.released, "promoted_to": outcome.promoted_to}

    @app.post("/api/sessions/{session_id}/task/lease/queue/cancel")
    async def cancel_queued_lease(session_id: str) -> dict:
        """撤销本 Task 的排队项（不影响当前持有者；幂等）。"""
        try:
            validate_session_id(session_id)
        except InvalidSessionId as e:
            raise http_error(e) from e
        await app.state.agent.ensure_stores()
        cancelled = await _manager().cancel_queued(session_id)
        return {"cancelled": cancelled}
