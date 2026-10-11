"""Read-only status for installed MCP package identities in the current Runtime."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from fastapi import Request
from starlette.responses import JSONResponse

from agent_harness.host_service import (
    HOST_SKILLS_RESPONSE_PROOF_HEADER,
    host_skills_response_proof,
)

if TYPE_CHECKING:
    from fastapi import FastAPI


def register_mcp_package_routes(app: FastAPI) -> None:
    @app.get("/api/mcp/packages")
    async def list_mcp_packages(request: Request) -> dict:
        _, wiring = await app.state.agent.get_wiring()
        payload = {"packages": list(getattr(wiring, "mcp_packages", []))}
        nonce = getattr(request.state, "host_skills_challenge_nonce", None)
        service_uuid = getattr(request.state, "host_skills_service_uuid", None)
        timestamp = getattr(request.state, "host_skills_proof_timestamp", None)
        token = getattr(request.app.state, "host_service_token", None)
        if nonce and service_uuid and timestamp and token:
            response = JSONResponse(payload)
            response.headers[HOST_SKILLS_RESPONSE_PROOF_HEADER] = (
                host_skills_response_proof(
                    token,
                    nonce,
                    service_uuid,
                    timestamp,
                    response.status_code,
                    response.body,
                    path="/api/mcp/packages",
                )
            )
            return cast(dict, response)
        return payload
