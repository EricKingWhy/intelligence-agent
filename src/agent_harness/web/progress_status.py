"""W-06（#350）：进度文件重读对账与外部编辑冲突的最小 REST 状态面（独立 router）。

票面范围「及 API 状态」：`GET /progress` 返回磁盘文件与 SessionEvent 投影的
对账状态（含外部编辑的字段级差异与原事件引用——差异文本行内自带
`（来源 seq N）`）；`POST /progress/resolve` 是票面工作指令 2 的两个出口
（丢弃手改 / 确认为新用户指令）。投影单源在 `session/progress.py` +
`session/service.py`，端点只做传输边界搬运 + 错误码翻译（task_delivery 同型）。

载荷纪律：差异文本两侧在 `_diff_bodies` 出口已经 `sanitize_text` 脱敏；端点
不再回显文件全文，不返回任何凭证值。

错误码口径（spec 11 §6.1 + task_delivery 同表）：形状非法 422、状态对不上
409；会话层词汇（InvalidSessionId / SessionNotFound）走既有 `http_error` 单源表。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict
from typing import TYPE_CHECKING

from fastapi import HTTPException
from pydantic import BaseModel

from agent_harness.session.errors import (
    InvalidSessionId,
    SessionNotFound,
)
from agent_harness.web.app import session_service
from agent_harness.web.domain_errors import http_error

if TYPE_CHECKING:
    from fastapi import FastAPI


class ProgressResolveRequest(BaseModel):
    """POST /progress/resolve：丢弃手改（discard）或确认为新用户指令（confirm）。"""

    action: str


def _verification_payload(verification) -> dict:
    """ProgressVerification → JSON（dataclass 平铺；不新增第二套字段名）。"""
    payload = asdict(verification)
    payload.pop("diffs", None)
    payload["diffs"] = [
        {"field": diff.field, "expected": diff.expected, "actual": diff.actual}
        for diff in verification.diffs
    ]
    payload["verifiable"] = verification.verifiable
    return payload


def register_progress_routes(
    app: FastAPI, *, validate_session_id: Callable[[str], str]
) -> None:
    """把进度文件状态/冲突解决路由挂到既有 app（app.py 侧一行调用的接入面）。"""

    @app.get("/api/sessions/{session_id}/progress")
    async def get_progress_status(session_id: str) -> dict:
        """磁盘 progress.md 与 SessionEvent 投影的对账状态（只读，零副作用）。"""
        try:
            validate_session_id(session_id)
        except InvalidSessionId as e:
            raise http_error(e) from e
        service = session_service(app.state.agent)
        try:
            verification = await service.progress_file_status(session_id)
        except (InvalidSessionId, SessionNotFound) as e:
            raise http_error(e) from e
        if verification is None:
            raise HTTPException(
                status_code=404,
                detail=f"session '{session_id}' has no progress anchor",
            )
        return {"progress": _verification_payload(verification)}

    @app.post("/api/sessions/{session_id}/progress/resolve")
    async def resolve_progress_conflict(
        session_id: str, req: ProgressResolveRequest,
    ) -> dict:
        """解决外部编辑冲突：discard（投影重写）或 confirm（先追加用户确认
        事件再投影重写）。无待处理冲突 409；action 非法 422。"""
        try:
            validate_session_id(session_id)
        except InvalidSessionId as e:
            raise http_error(e) from e
        service = session_service(app.state.agent)
        try:
            verification, error_kind = await service.progress_resolve_external_edit(
                session_id, action=req.action,
            )
        except (InvalidSessionId, SessionNotFound) as e:
            raise http_error(e) from e
        if error_kind == "shape":
            raise HTTPException(
                status_code=422,
                detail="action 必须是 'discard' 或 'confirm'",
            )
        if error_kind == "no_anchor":
            raise HTTPException(
                status_code=404,
                detail=f"session '{session_id}' has no progress anchor",
            )
        if error_kind == "conflict":
            status = (
                verification.status if verification is not None else "unknown"
            )
            raise HTTPException(
                status_code=409,
                detail=f"no pending external edit（当前状态：{status}）",
            )
        return {"progress": _verification_payload(verification)}
