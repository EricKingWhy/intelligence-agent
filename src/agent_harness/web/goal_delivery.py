"""#1359 阶段 1：持久目标对象的最小 REST 面（独立 router）。

`GET /api/sessions/{id}/goal` 只读投影 + 进程内 activation；`POST` 是唯一的生命周期
命令入口（create/edit/pause/resume/complete/block/clear）。投影与写入口单源在
``session/goal.py``（``effective_goal`` / ``append_goal_change``），本模块只做传输
边界搬运 + 错误码翻译（与 ``task_delivery`` 同型）。

错误码口径（spec 11 §6.1 + task_delivery 同表）：**形状非法 422、状态对不上 409**，
由 handler 判定（``GoalOutcome.error_kind``），任何写入之前发生——被拒请求零副作用。
会话层词汇（InvalidSessionId / SessionNotFound）走既有 ``http_error`` 单源表。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Literal

from fastapi import Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from agent_harness.session.errors import (
    InvalidSessionId,
    SessionNotFound,
)
from agent_harness.session.goal import GoalOutcome
from agent_harness.storage.sqlite import StorageBusyError
from agent_harness.web.app import session_service
from agent_harness.web.domain_errors import http_error, storage_http_error
from agent_harness.web.projects import require_trusted_origin

if TYPE_CHECKING:
    from fastapi import FastAPI


class GoalCommandRequest(BaseModel):
    """`POST /api/sessions/{id}/goal` 的请求体。

    ``operation`` 用 Literal 钉死值域：未知操作是 422（pydantic 层），不进 handler。
    ``blocked_reason`` / ``max_goal_rounds`` / ``objective`` 的形状校验全在
    ``append_goal_change``（与 task 面同一分工：Web 层不做第二套校验）。
    """

    model_config = ConfigDict(extra="forbid")

    operation: Literal[
        "create", "edit", "pause", "resume", "complete", "block", "clear"
    ]
    objective: str | None = None
    blocked_reason: dict | None = None
    max_goal_rounds: int | None = Field(default=None, ge=1)
    # CAS：并发 edit 后者收 409（AC3）。缺省 = 不带 CAS（单客户端场景）。
    expected_revision: int | None = Field(default=None, ge=1)


#: GoalOutcome.error_kind → HTTP status（本面唯一的 kind 翻译点；直接索引不回退，
#: 未来新增 kind 忘了登记就 KeyError 炸出来，而不是静默归进 422 说谎）。
_GOAL_OUTCOME_STATUS = {"shape": 422, "conflict": 409}


def _apply(outcome: GoalOutcome, activation: str) -> dict:
    if not outcome.ok:
        raise HTTPException(
            status_code=_GOAL_OUTCOME_STATUS[outcome.error_kind],
            detail=outcome.reason,
        )
    # 回执带 activation：命令本身会改它（create/resume → armed，pause/... → disarmed），
    # 前端不必再 GET 一次才能渲染（票面 UX 要求 `/goal` 输出含 activation）。
    return {
        "status": "applied",
        "goal": outcome.state.to_payload(),
        "activation": activation,
    }


def register_goal_routes(
    app: FastAPI, *, validate_session_id: Callable[[str], str]
) -> None:
    """把目标对象路由挂到既有 app（app.py 侧一行调用的接入面）。"""

    @app.get("/api/sessions/{session_id}/goal")
    async def get_goal_state(session_id: str) -> dict:
        """目标投影 + activation（只读，零副作用；未定义目标也 200）。"""
        try:
            validate_session_id(session_id)
        except InvalidSessionId as e:
            raise http_error(e) from e
        service = session_service(app.state.agent)
        try:
            return await service.goal_state(session_id)
        except (InvalidSessionId, SessionNotFound) as e:
            raise http_error(e) from e
        except StorageBusyError as e:
            raise storage_http_error(e) from e

    @app.post("/api/sessions/{session_id}/goal")
    async def change_goal(
        session_id: str,
        req: GoalCommandRequest,
        _: None = Depends(require_trusted_origin),
    ) -> dict:
        """目标生命周期命令（200 = 命令后快照；422 形状；409 状态/CAS 对不上）。"""
        try:
            validate_session_id(session_id)
        except InvalidSessionId as e:
            raise http_error(e) from e
        service = session_service(app.state.agent)
        try:
            outcome = await service.set_goal(
                session_id,
                req.operation,
                objective=req.objective,
                blocked_reason=req.blocked_reason,
                max_goal_rounds=req.max_goal_rounds,
                expected_revision=req.expected_revision,
            )
        except (InvalidSessionId, SessionNotFound) as e:
            raise http_error(e) from e
        except StorageBusyError as e:
            raise storage_http_error(e) from e
        return _apply(outcome, service.goal_activation(session_id))
