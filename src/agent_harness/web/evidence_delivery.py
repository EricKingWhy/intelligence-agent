"""W-08（#352）：证据投影的最小 REST 查询/命令面（独立 router）。

票面：「API 刷新重建同样证据与 stale 标识」。投影只有服务端
``derive_evidence_state`` + 读取时陈旧求值一份（`session/evidence.py`），端点只做
传输边界的搬运 + 错误码翻译；载荷里只有证据事实与陈旧标识，**没有任何凭证值**
（本面根本不接触 credential 存储；敏感输出值不进正文是记录侧的责任，
见 `session/evidence.py` 脱敏纪律）。

错误码口径（spec 11 §6.1 + T4/#312 先例）：**形状非法 422、状态对不上 409**，
由 handler 判定（`EvidenceOutcome.error_kind`），任何落盘之前发生——被拒请求
零副作用。会话层词汇（InvalidSessionId / SessionNotFound / SeqConflict）走既有
`http_error` 单源表。无任务定义 → 404（与 `GET /task` 同口径，不伪装成空状态）。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from fastapi import HTTPException
from pydantic import BaseModel

from agent_harness.session.errors import (
    InvalidSessionId,
    SeqConflict,
    SessionNotFound,
)
from agent_harness.session.evidence import EvidenceOutcome
from agent_harness.web.app import session_service
from agent_harness.web.domain_errors import http_error

if TYPE_CHECKING:
    from fastapi import FastAPI


class EvidenceRecordRequest(BaseModel):
    """POST /evidence：票面 14 字段 DTO。

    Any 形状（`dict` 太宽会 500 太窄会 422 双轨）：字段级判据全部委托
    handler（session/evidence.py），Web 层不做第二套校验（TUI/Web 不各算一套）。
    """

    evidence: dict


#: EvidenceOutcome.error_kind → HTTP status 的显式映射（本面唯一的 kind 翻译点）。
#: 直接索引不回退（task_delivery 同哲学）：未来新增 kind 而忘了登记，就让它
#: 在这里 KeyError 炸出来，而不是静默归进 422 说谎。
_EVIDENCE_OUTCOME_STATUS = {"shape": 422, "conflict": 409}


def _evidence_http_error(outcome: EvidenceOutcome) -> None:
    """EvidenceOutcome → HTTP（形状 422 / 冲突 409，spec 11 §6.1 口径）。"""
    if outcome.ok:
        return
    status = _EVIDENCE_OUTCOME_STATUS[outcome.error_kind]
    raise HTTPException(status_code=status, detail=outcome.reason)


def register_evidence_routes(
    app: FastAPI, *, validate_session_id: Callable[[str], str]
) -> None:
    """把证据投影路由挂到既有 app（app.py 侧一行调用的接入面）。"""

    @app.get("/api/sessions/{session_id}/evidence")
    async def get_evidence_state(session_id: str) -> dict:
        """服务端证据投影 + 陈旧标识（刷新后由事件流重建同一结果）。"""
        try:
            validate_session_id(session_id)
        except InvalidSessionId as e:
            raise http_error(e) from e
        service = session_service(app.state.agent)
        try:
            projection = await service.evidence_state(session_id)
        except (InvalidSessionId, SessionNotFound) as e:
            raise http_error(e) from e
        if projection is None:
            raise HTTPException(
                status_code=404, detail=f"session '{session_id}' has no task definition"
            )
        return {"evidence": projection["by_criterion"]}

    @app.post("/api/sessions/{session_id}/evidence")
    async def record_evidence(session_id: str, req: EvidenceRecordRequest) -> dict:
        """记录一条结构化证据（append-only；evidence_id 重复明确 409）。"""
        try:
            validate_session_id(session_id)
        except InvalidSessionId as e:
            raise http_error(e) from e
        service = session_service(app.state.agent)
        try:
            outcome = await service.record_evidence(
                session_id, evidence=req.evidence
            )
        except (InvalidSessionId, SessionNotFound, SeqConflict) as e:
            raise http_error(e) from e
        _evidence_http_error(outcome)
        # POST 与 GET 同口径：返回的 record 带 freshness（读取时求值），
        # 客户端拿到的陈旧标识与刷新后一致。
        projection = await service.evidence_state(session_id)
        recorded: dict = {}
        if projection is not None:
            for item in projection["by_criterion"].get(
                req.evidence.get("criterion_id"), ()
            ):
                if item.get("evidence_id") == req.evidence.get("evidence_id"):
                    recorded = item
                    break
        return {"status": "recorded", "record": recorded}
