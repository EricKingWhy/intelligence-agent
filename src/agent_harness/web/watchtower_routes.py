"""HTTP surface for the serve-owned Watchtower scheduler."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    field_validator,
)

from agent_harness.session.service import SessionNotFound
from agent_harness.session.task import derive_task_state
from agent_harness.watchtower.service import WatchtowerService

router = APIRouter(prefix="/api/watchtower", tags=["watchtower"])


class CreateRuleRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    prompt: str = Field(min_length=1, max_length=100_000)
    trigger_kind: Literal["relative", "absolute"]
    timezone: str = Field(min_length=1, max_length=128)
    delay_seconds: StrictInt | None = Field(default=None, ge=60, le=31_536_000)
    local_time: str | None = Field(default=None, min_length=16, max_length=32)
    model: str | None = Field(default=None, min_length=1, max_length=256)
    sandbox_backend: Literal["local", "docker"] | None = None
    cwd: str | None = Field(default=None, max_length=4096)
    budget_turns: StrictInt | None = Field(default=None, gt=0)
    budget_tokens: StrictInt | None = Field(default=None, gt=0)
    budget_duration_seconds: StrictInt | None = None
    consent_acknowledged: StrictBool
    consent_version: str = Field(min_length=1, max_length=64)

    @field_validator("budget_duration_seconds")
    @classmethod
    def validate_duration(cls, value: int | None) -> int | None:
        if value is not None and value not in {1800, 7200, 14400}:
            raise ValueError("budget_duration_seconds must be 1800, 7200, 14400, or null")
        return value


class RescheduleRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    timezone: str = Field(min_length=1, max_length=128)
    local_time: str = Field(min_length=16, max_length=32)


def _service(request: Request) -> WatchtowerService:
    return request.app.state.agent.watchtower


def _rule_payload(rule) -> dict:
    return rule.model_dump(mode="json")


@router.get("")
async def get_watchtower(request: Request) -> dict:
    state = request.app.state.agent
    await state.ensure_stores()
    rules = await state.watchtower_store.list_rules()
    rows = []
    pending_verification: dict[str, dict] = {}
    for rule in rules:
        executions = await state.watchtower_store.list_executions(rule.rule_id)
        execution_rows = []
        for execution in executions:
            product_state = None
            if execution.state == "fired":
                try:
                    events = await state.watchtower.session_service.get_events(
                        execution.session_id,
                    )
                    task_state = derive_task_state(events)
                    product_state = task_state.product_state or None
                    if product_state == "pending_verification":
                        pending_verification[execution.session_id] = {
                            "session_id": execution.session_id,
                            "rule_id": rule.rule_id,
                            "prompt": rule.prompt,
                            "updated_at": execution.updated_at.isoformat(),
                        }
                except SessionNotFound:
                    pass
            execution_rows.append({
                **execution.model_dump(mode="json"),
                # A pre-model 429 is represented on the rule only. Do not present
                # its empty session as a run card in the Watchtower panel.
                "session_id": (
                    execution.session_id if execution.state == "fired" else None
                ),
                "product_state": product_state,
            })
        rows.append({
            **_rule_payload(rule),
            "occurrence_count": sum(e.state == "fired" for e in executions),
            "executions": execution_rows,
        })
    return {
        "scheduler": state.watchtower.status(),
        "rules": rows,
        "pending_verification": list(pending_verification.values()),
    }


@router.post("/rules")
async def create_watchtower_rule(body: CreateRuleRequest, request: Request) -> dict:
    state = request.app.state.agent
    await state.ensure_stores()
    if body.cwd is not None:
        try:
            state.watchtower.session_service._resolve_cwd(body.cwd)
        except Exception as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
    try:
        rule = await _service(request).create_rule(**body.model_dump())
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return {"rule": _rule_payload(rule)}


@router.post("/rules/{rule_id}/pause")
async def pause_watchtower_rule(rule_id: str, request: Request) -> dict:
    return await _transition_response(request, rule_id, "pause")


@router.post("/rules/{rule_id}/resume")
async def resume_watchtower_rule(rule_id: str, request: Request) -> dict:
    return await _transition_response(request, rule_id, "resume")


@router.post("/rules/{rule_id}/archive")
async def archive_watchtower_rule(rule_id: str, request: Request) -> dict:
    return await _transition_response(request, rule_id, "archive")


@router.post("/rules/{rule_id}/retry")
async def retry_watchtower_rule(rule_id: str, request: Request) -> dict:
    return await _transition_response(request, rule_id, "retry_now")


@router.post("/rules/{rule_id}/reschedule")
async def reschedule_watchtower_rule(
    rule_id: str, body: RescheduleRequest, request: Request,
) -> dict:
    await request.app.state.agent.ensure_stores()
    try:
        rule = await _service(request).reschedule(
            rule_id, timezone=body.timezone, local_time=body.local_time,
        )
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    if rule is None:
        raise HTTPException(status_code=409, detail="rule is missing or cannot be rescheduled")
    return {"rule": _rule_payload(rule)}


async def _transition_response(request: Request, rule_id: str, action: str) -> dict:
    await request.app.state.agent.ensure_stores()
    service = _service(request)
    rule = await getattr(service, action)(rule_id)
    if rule is None:
        raise HTTPException(status_code=409, detail=f"rule cannot {action}")
    return {"rule": _rule_payload(rule)}


def register_watchtower_routes(app) -> None:
    app.include_router(router)
