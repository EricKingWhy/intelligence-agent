"""W-07（#351）：Task 交付状态的最小 REST 查询/命令面（独立 router）。

票面工作指令 3：「API 返回任务状态以及每项验证/接受依据，不返回凭证值。刷新后
由 Event 重建同一结果，TUI/Web 不各算一套。」——投影只有服务端
``derive_task_state`` 一份（`session/task.py`），端点只做传输边界的搬运 +
错误码翻译；载荷里只有任务事实（文本/清单/验证值/裁决），**没有任何凭证值**
（本面根本不接触 credential 存储）。

错误码口径（spec 11 §6.1 + T4/#312 先例）：**形状非法 422、状态对不上 409**，
由 handler 判定（`TaskOutcome.error_kind`），任何 model/tool/child 工作与任何
落盘之前发生——被拒请求零副作用（session 层测试钉住）。会话层词汇
（InvalidSessionId / SessionNotFound / SeqConflict）走既有 `http_error` 单源表。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from fastapi import HTTPException
from pydantic import BaseModel, Field

from agent_harness.session.errors import (
    InvalidSessionId,
    SeqConflict,
    SessionNotFound,
)
from agent_harness.session.task import TaskOutcome
from agent_harness.web.app import session_service
from agent_harness.web.domain_errors import http_error

if TYPE_CHECKING:
    from fastapi import FastAPI


class TaskDefinitionRequest(BaseModel):
    """POST /task/definition：原始目标 + 可选读写意图 + 可选验收清单。

    缺项合法（票面：缺项时 Agent 可提出清单，走 acceptance-revision）；判据
    形状校验在 handler（session/task.py），pydantic 只挡「不是对象」这一层。
    """

    task_text: str
    read_write_intent: str | None = None
    # Any 形状（list[dict] 太宽会 500 太窄会 422 双轨）：行级判据全部委托
    # handler，Web 层不做第二套校验（TUI/Web 不各算一套）。
    criteria: list[dict] | None = None


class AcceptanceRevisionRequest(BaseModel):
    criteria: list[dict] | None = None


class VerificationRequest(BaseModel):
    item_id: str
    value: str
    evidence: str | None = None


class AcceptanceRequest(BaseModel):
    decision: str
    reason: str | None = None
    # CAS 是命令契约的必填项（票面：「必须有 expected_version 或同等 CAS」），
    # 缺失就是客户端 bug，让 pydantic 直接 422。
    expected_version: int = Field(ge=0)


class AcceptanceReleaseRequest(BaseModel):
    reason: str | None = None
    expected_version: int = Field(ge=0)


#: TaskOutcome.error_kind → HTTP status 的显式映射（本面唯一的 kind 翻译点）。
#: 直接索引不回退（domain_errors 同哲学）：未来新增 kind 而忘了登记，就让它
#: 在这里 KeyError 炸出来，而不是静默归进 422 说谎。
_TASK_OUTCOME_STATUS = {"shape": 422, "conflict": 409}


def _task_http_error(outcome: TaskOutcome) -> None:
    """TaskOutcome → HTTP（形状 422 / 冲突 409，spec 11 §6.1 口径）。"""
    if outcome.ok:
        return
    status = _TASK_OUTCOME_STATUS[outcome.error_kind]
    raise HTTPException(status_code=status, detail=outcome.reason)


def _apply(outcome: TaskOutcome) -> dict:
    """成功回执 = 命令后投影快照（客户端拿新 version 发下一次 CAS 命令）。"""
    _task_http_error(outcome)
    return {"status": "applied", "task": outcome.state.to_payload()}


def register_task_routes(
    app: FastAPI, *, validate_session_id: Callable[[str], str]
) -> None:
    """把 task 交付状态路由挂到既有 app（app.py 侧一行调用的接入面）。"""

    @app.get("/api/sessions/{session_id}/task")
    async def get_task_state(session_id: str) -> dict:
        """任务状态 + 每项验证/接受依据（刷新后由事件流重建同一结果）。"""
        try:
            validate_session_id(session_id)
        except InvalidSessionId as e:
            raise http_error(e) from e
        service = session_service(app.state.agent)
        try:
            state = await service.task_state(session_id)
        except (InvalidSessionId, SessionNotFound) as e:
            raise http_error(e) from e
        if state is None:
            raise HTTPException(
                status_code=404, detail=f"session '{session_id}' has no task definition"
            )
        return {"task": state.to_payload()}

    @app.post("/api/sessions/{session_id}/task/definition")
    async def define_task(session_id: str, req: TaskDefinitionRequest) -> dict:
        """保存原始目标/读写意图/验收清单（全流至多一条；409 = 已定义）。"""
        try:
            validate_session_id(session_id)
        except InvalidSessionId as e:
            raise http_error(e) from e
        service = session_service(app.state.agent)
        try:
            outcome = await service.task_definition(
                session_id,
                task_text=req.task_text,
                read_write_intent=req.read_write_intent,
                criteria=req.criteria,
            )
        except (InvalidSessionId, SessionNotFound, SeqConflict) as e:
            raise http_error(e) from e
        return _apply(outcome)

    @app.post("/api/sessions/{session_id}/task/acceptance-revision")
    async def revise_acceptance(session_id: str, req: AcceptanceRevisionRequest) -> dict:
        """变更验收清单：只追加事件，source_event_ids 指向上一版（保留旧版来源）。"""
        try:
            validate_session_id(session_id)
        except InvalidSessionId as e:
            raise http_error(e) from e
        service = session_service(app.state.agent)
        try:
            outcome = await service.task_acceptance_revision(
                session_id, criteria=req.criteria
            )
        except (InvalidSessionId, SessionNotFound, SeqConflict) as e:
            raise http_error(e) from e
        return _apply(outcome)

    @app.post("/api/sessions/{session_id}/task/verification")
    async def update_verification(session_id: str, req: VerificationRequest) -> dict:
        """逐验收项观察事实（last-wins；重估覆盖合法，旧值留痕于更早事件）。"""
        try:
            validate_session_id(session_id)
        except InvalidSessionId as e:
            raise http_error(e) from e
        service = session_service(app.state.agent)
        try:
            outcome = await service.task_verification(
                session_id,
                item_id=req.item_id,
                value=req.value,
                evidence=req.evidence,
            )
        except (InvalidSessionId, SessionNotFound, SeqConflict) as e:
            raise http_error(e) from e
        return _apply(outcome)

    @app.post("/api/sessions/{session_id}/task/acceptance")
    async def accept_task(session_id: str, req: AcceptanceRequest) -> dict:
        """用户裁决（CAS：expected_version 必填；重复接受明确 409，不能双写）。"""
        try:
            validate_session_id(session_id)
        except InvalidSessionId as e:
            raise http_error(e) from e
        service = session_service(app.state.agent)
        try:
            outcome = await service.task_acceptance(
                session_id,
                decision=req.decision,
                reason=req.reason,
                expected_version=req.expected_version,
            )
        except (InvalidSessionId, SessionNotFound, SeqConflict) as e:
            raise http_error(e) from e
        return _apply(outcome)

    @app.post("/api/sessions/{session_id}/task/acceptance/release")
    async def release_acceptance(session_id: str, req: AcceptanceReleaseRequest) -> dict:
        """撤销裁决（同样 CAS；验证值保持原样——释放的是裁决不是观察事实）。"""
        try:
            validate_session_id(session_id)
        except InvalidSessionId as e:
            raise http_error(e) from e
        service = session_service(app.state.agent)
        try:
            outcome = await service.task_acceptance_release(
                session_id, reason=req.reason, expected_version=req.expected_version
            )
        except (InvalidSessionId, SessionNotFound, SeqConflict) as e:
            raise http_error(e) from e
        return _apply(outcome)
