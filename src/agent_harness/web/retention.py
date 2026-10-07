"""#368 [W-24]：会话保留 / 空间显示 / 显式清理的 REST 传输面（独立 router）。

票面：「空间显示 + 显式清理」。语义单源在 ``session/service.py``（``get_session_usage``
/ ``preview_artifact_cleanup`` / ``execute_artifact_cleanup``），端点只做传输边界的
搬运 + 错误码翻译。

错误码口径（spec 11 §6.1 + T4/#312 先例）：**形状非法 422、状态对不上 409**。
``preview`` 的 ``mode`` 用 ``Literal`` 收窄，非法值由 FastAPI 给 422（形状非法）；
``execute`` 的 ``snapshot_token`` 复验失败是 CAS token 过期——请求形状合法、是状态
对不上，映射 409（``SnapshotTokenMismatch``，Kubernetes resourceVersion 乐观并发）。
``execute`` 会删除本地 artifact，是宿主侧破坏性动作，故加 ``require_trusted_origin``
来源闸（ADR-0025 D1）。会话层词汇（InvalidSessionId / SessionNotFound /
ActiveRunConflict / SnapshotTokenMismatch）走既有 ``http_error`` 单源表。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Literal

from fastapi import Depends
from pydantic import BaseModel

from agent_harness.session.errors import (
    ActiveRunConflict,
    InvalidSessionId,
    SessionNotFound,
    SnapshotTokenMismatch,
)
from agent_harness.web.app import session_service
from agent_harness.web.domain_errors import http_error
from agent_harness.web.projects import require_trusted_origin

if TYPE_CHECKING:
    from fastapi import FastAPI


class CleanupPreviewRequest(BaseModel):
    """POST /cleanup/preview：只支持 ``mode="unreferenced"``（非法值 → 422）。

    ``selected_refs``（#368 P3-3 UX 方案 a）：前端勾选变化后重取预览时传入当前勾选
    集合，token 按 ``勾选 ∩ 可清理`` 生成（与 execute 复算口径一致）；不传 → 按
    affected 全集生成（旧行为）。形状非法（非 list）由 pydantic 给 422。
    """

    mode: Literal["unreferenced"] = "unreferenced"
    selected_refs: list[str] | None = None


class CleanupExecuteRequest(BaseModel):
    """POST /cleanup/execute：预览 token + 用户勾选的候选 ref 列表。"""

    snapshot_token: str
    artifact_refs: list[str]


def register_retention_routes(
    app: FastAPI, *, validate_session_id: Callable[[str], str]
) -> None:
    """把保留 / 清理路由挂到既有 app（app.py 侧一行调用的接入面）。"""

    @app.get("/api/sessions/{session_id}/usage")
    async def get_session_usage(session_id: str) -> dict:
        """会话占用的逐项字节数与可回收量（零副作用只读）。"""
        try:
            validate_session_id(session_id)
        except InvalidSessionId as e:
            raise http_error(e) from e
        service = session_service(app.state.agent)
        try:
            return await service.get_session_usage(session_id)
        except (InvalidSessionId, SessionNotFound) as e:
            raise http_error(e) from e

    @app.post("/api/sessions/{session_id}/cleanup/preview")
    async def preview_artifact_cleanup(
        session_id: str, req: CleanupPreviewRequest
    ) -> dict:
        """清理预览：可达集 + blocked/affected 明细 + 快照 token（CAS）。"""
        try:
            validate_session_id(session_id)
        except InvalidSessionId as e:
            raise http_error(e) from e
        service = session_service(app.state.agent)
        try:
            return await service.preview_artifact_cleanup(
                session_id, mode=req.mode, selected_refs=req.selected_refs
            )
        except (InvalidSessionId, SessionNotFound, ActiveRunConflict) as e:
            raise http_error(e) from e

    @app.post("/api/sessions/{session_id}/cleanup/execute")
    async def execute_artifact_cleanup(
        session_id: str,
        req: CleanupExecuteRequest,
        _: None = Depends(require_trusted_origin),
    ) -> dict:
        """执行清理：token 复验 + 逐 ref 重验不可达后才删（诚实明细）。

        来源闸（ADR-0025 D1）：删除本地 artifact 是宿主侧破坏性动作，只接受本机来源。
        """
        try:
            validate_session_id(session_id)
        except InvalidSessionId as e:
            raise http_error(e) from e
        service = session_service(app.state.agent)
        try:
            return await service.execute_artifact_cleanup(
                session_id, req.snapshot_token, req.artifact_refs
            )
        except (
            InvalidSessionId,
            SessionNotFound,
            ActiveRunConflict,
            SnapshotTokenMismatch,
        ) as e:
            raise http_error(e) from e
