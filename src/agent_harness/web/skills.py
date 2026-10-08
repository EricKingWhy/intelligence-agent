"""Skill 目录只读展示（#529 §6.3 第一版：catalog 列表，无管理按钮）。

治理面第一版只做**只读**：skill 目录列表 + 待审草稿列表 + 发现阶段错误/冲突。
文件即真相（§6.3/不变量 #22）：编辑/删除走文件系统（registry 是 discover 的
投影），本路由不提供任何写操作——管理按钮 DEFER。

未装配 skills capability → 503 逐原因说法（#225 同款：不假装能力存在）。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from fastapi import HTTPException, Request
from starlette.responses import JSONResponse

from agent_harness.host_service import (
    HOST_SKILLS_RESPONSE_PROOF_HEADER,
    host_skills_response_proof,
)

if TYPE_CHECKING:
    from fastapi import FastAPI


def register_skill_routes(app: FastAPI) -> None:
    """把 skill 只读路由挂到既有 app（`create_app` 里一行调用的接入面）。"""

    @app.get("/api/skills")
    async def list_skills(request: Request) -> dict:
        _, wiring = await app.state.agent.get_wiring()
        capability = getattr(wiring, "skills", None)
        if capability is None:
            raise HTTPException(
                status_code=503,
                detail={
                    "code": "not_configured",
                    "message": "skills capability 未启用：请在 CAPABILITIES 中配置 skills。",
                },
            )
        entries = capability.catalog()
        promoter = getattr(capability, "promoter", None)
        drafts = (
            [{"name": d.name, "status": d.status} for d in promoter.drafts()]
            if promoter is not None else []
        )
        payload = {
            # 文件即真相的投影（只读）；正文经 load_skill / 文件系统按需读。
            "skills": [
                {
                    "name": entry.name,
                    "description": entry.description,
                    "when_to_use": entry.when_to_use,
                    "source": str(entry.source_path),
                }
                for entry in entries
            ],
            "drafts": drafts,
            "errors": capability.errors(),
            "conflicts": capability.conflicts(),
        }
        nonce = getattr(request.state, "host_skills_challenge_nonce", None)
        token = getattr(request.app.state, "host_service_token", None)
        if nonce and token:
            response = JSONResponse(payload)
            response.headers[HOST_SKILLS_RESPONSE_PROOF_HEADER] = host_skills_response_proof(
                token,
                nonce,
                response.status_code,
                response.body,
            )
            return cast(dict, response)
        return payload
