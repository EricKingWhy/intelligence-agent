from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import jwt
from fastapi.testclient import TestClient

from agent_harness.config import Settings
from agent_harness.host_service import (
    HOST_SKILLS_NONCE_HEADER,
    HOST_SKILLS_PROOF_HEADER,
    HOST_SKILLS_RESPONSE_PROOF_HEADER,
    HOST_SKILLS_TIMESTAMP_HEADER,
    host_skills_request_proof,
    host_skills_response_proof,
)
from agent_harness.web.app import create_app

_SERVICE_UUID = "a" * 32


class _SkillCapability:
    def __init__(self, source: Path) -> None:
        self._entries = [
            SimpleNamespace(
                name="sample-skill",
                description="A sample.",
                when_to_use="",
                source_path=source,
            )
        ]
        self.promoter = None

    def catalog(self) -> list[SimpleNamespace]:
        return self._entries

    def errors(self) -> list[str]:
        return []

    def conflicts(self) -> list[str]:
        return []


class _FakeAgent:
    def __init__(self, capability: _SkillCapability) -> None:
        self._capability = capability

    async def get_wiring(self):
        return None, SimpleNamespace(skills=self._capability)


def _host_token(secret: str, *, expires_in: timedelta) -> str:
    expiry = datetime.now(UTC) + expires_in
    return jwt.encode(
        {
            "tenant_id": "local",
            "user_id": "local-user",
            "scopes": ["user", "session"],
            "service_uuid": _SERVICE_UUID,
            "exp": int(expiry.timestamp()),
        },
        secret,
        algorithm="HS256",
    )


def test_host_service_challenge_authenticates_and_signs_live_skills_response(
    tmp_path: Path,
) -> None:
    secret = "host-challenge-test-secret-with-sufficient-length"
    token = _host_token(secret, expires_in=timedelta(minutes=5))
    app = create_app(
        Settings(_env_file=None, workspace_dir=str(tmp_path), jwt_secret=secret),
        enable_cors=False,
        host_service_token=token,
    )
    app.state.agent = _FakeAgent(_SkillCapability(tmp_path / "skills" / "sample-skill" / "SKILL.md"))
    nonce = secrets.token_hex(32)
    timestamp = str(int(datetime.now(UTC).timestamp()))
    headers = {
        HOST_SKILLS_NONCE_HEADER: nonce,
        HOST_SKILLS_TIMESTAMP_HEADER: timestamp,
        HOST_SKILLS_PROOF_HEADER: host_skills_request_proof(
            token, nonce, _SERVICE_UUID, timestamp
        ),
    }

    with TestClient(app) as client:
        response = client.get("/api/skills", headers=headers)
        assert response.status_code == 200
        assert response.json()["skills"][0]["name"] == "sample-skill"
        assert response.headers[HOST_SKILLS_RESPONSE_PROOF_HEADER] == host_skills_response_proof(
            token, nonce, _SERVICE_UUID, timestamp, response.status_code, response.content
        )
        assert client.get("/api/skills", headers=headers).status_code == 401

        invalid = dict(headers)
        invalid[HOST_SKILLS_PROOF_HEADER] = "0" * 64
        assert client.get("/api/skills", headers=invalid).status_code == 401


def test_expired_host_token_cannot_use_skills_challenge(tmp_path: Path) -> None:
    secret = "host-challenge-test-secret-with-sufficient-length"
    token = _host_token(secret, expires_in=timedelta(days=-1))
    app = create_app(
        Settings(_env_file=None, workspace_dir=str(tmp_path), jwt_secret=secret),
        enable_cors=False,
        host_service_token=token,
    )
    nonce = secrets.token_hex(32)
    timestamp = str(int(datetime.now(UTC).timestamp()))

    with TestClient(app) as client:
        response = client.get(
            "/api/skills",
            headers={
                HOST_SKILLS_NONCE_HEADER: nonce,
                HOST_SKILLS_TIMESTAMP_HEADER: timestamp,
                HOST_SKILLS_PROOF_HEADER: host_skills_request_proof(
                    token, nonce, _SERVICE_UUID, timestamp
                ),
            },
        )
        assert response.status_code == 401


def test_host_skills_proof_is_bound_to_service_uuid(tmp_path: Path) -> None:
    secret = "host-challenge-test-secret-with-sufficient-length"
    token = _host_token(secret, expires_in=timedelta(minutes=5))
    app = create_app(
        Settings(_env_file=None, workspace_dir=str(tmp_path), jwt_secret=secret),
        enable_cors=False,
        host_service_token=token,
    )
    nonce = secrets.token_hex(32)
    timestamp = str(int(datetime.now(UTC).timestamp()))
    wrong_service_uuid = "b" * 32

    with TestClient(app) as client:
        response = client.get(
            "/api/skills",
            headers={
                HOST_SKILLS_NONCE_HEADER: nonce,
                HOST_SKILLS_TIMESTAMP_HEADER: timestamp,
                HOST_SKILLS_PROOF_HEADER: host_skills_request_proof(
                    token, nonce, wrong_service_uuid, timestamp
                ),
            },
        )

    assert response.status_code == 401
