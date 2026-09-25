from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from uuid import uuid4

import jwt
from fastapi.testclient import TestClient

from agent_harness.capability.base import CapabilityRegistry
from agent_harness.capability.wiring import CapabilityWiring
from agent_harness.config import Settings
from agent_harness.memory.v2.capability import MemoryV2Service
from agent_harness.memory.v2.index import InMemoryMemoryV2Index
from agent_harness.memory.v2.store import SqliteMemoryV2Store
from agent_harness.memory.v2.types import TrustedMemoryIdentity
from agent_harness.session import MEMORY_RECALLED, Session
from agent_harness.web.app import create_app
from tests.memory.v2._records import make_draft

_SECRET = "memory-v2-recall-api-signing-secret-at-least-32"


def _token(user_id: str, scopes: list[str] | None = None) -> dict[str, str]:
    encoded = jwt.encode({
        "tenant_id": "tenant-a", "user_id": user_id,
        "scopes": scopes or ["user"],
        "exp": int(datetime.now(UTC).timestamp()) + 600,
    }, _SECRET)
    return {"Authorization": f"Bearer {encoded}"}


def test_session_recall_api_returns_authorized_redacted_explanations(tmp_path) -> None:
    async def seed():
        store = SqliteMemoryV2Store(tmp_path / "memory-v2.db")
        await store.initialize()
        service = MemoryV2Service(store, InMemoryMemoryV2Index())
        own = await service.create(
            make_draft(content="PRIVATE OWN MEMORY CONTENT"),
            TrustedMemoryIdentity("tenant-a", "alice"),
        )
        foreign = await service.create(
            make_draft(content="PRIVATE OTHER USER CONTENT"),
            TrustedMemoryIdentity("tenant-a", "bob"),
        )
        return service, own, foreign

    service, own, foreign = asyncio.run(seed())
    settings = Settings(
        _env_file=None, workspace_dir=str(tmp_path), model_api_key="sk-test",
        jwt_secret=_SECRET, capabilities="{}",
    )
    app = create_app(settings, enable_cors=False)

    with TestClient(app) as client:
        state = client.app.state.agent
        state._registry = CapabilityRegistry()
        state._wiring = CapabilityWiring(memory_v2=service)
        session_id = str(uuid4())
        session = Session.start(state.store, session_id=session_id)
        session.append(MEMORY_RECALLED, {
            "ranking_version": "hybrid-v1",
            "memories": [
                {
                    "memory_id": own.id, "version": own.version,
                    "ranking": {
                        "ranking_version": "hybrid-v1", "score": 0.75,
                        "private_detail": "must not be echoed",
                    },
                    "evidence": ["must not be echoed"],
                },
                {"memory_id": foreign.id, "version": foreign.version, "ranking": {}},
                {"memory_id": own.id, "version": True, "ranking": {"score": 0.9}},
            ],
        }, run_id="run-a")

        response = client.get(
            f"/api/sessions/{session_id}/memory-recalls", headers=_token("alice"),
        )
        assert response.status_code == 200, response.text
        payload = response.json()
        assert len(payload) == 1 and len(payload[0]["memories"]) == 1
        explanation = payload[0]["memories"][0]
        assert explanation["memory_id"] == own.id
        assert explanation["ranking"] == {"ranking_version": "hybrid-v1", "score": 0.75}
        serialized = json.dumps(payload)
        assert foreign.id not in serialized
        assert "PRIVATE OWN MEMORY CONTENT" not in serialized
        assert "PRIVATE OTHER USER CONTENT" not in serialized
        assert "evidence" not in serialized and "private_detail" not in serialized

        denied = client.get(
            f"/api/sessions/{session_id}/memory-recalls",
            headers=_token("alice", ["session"]),
        )
        assert denied.status_code == 403

        state._wiring = CapabilityWiring()
        unavailable = client.get(
            f"/api/sessions/{session_id}/memory-recalls", headers=_token("alice"),
        )
        assert unavailable.status_code == 503
        assert unavailable.json()["detail"]["code"] == "memory_v2_unavailable"
