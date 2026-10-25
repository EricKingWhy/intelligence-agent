"""#1441 §3（F-13b）：`GET /api/memory-journey` 的出口形状（M10）。

真 app + 真装配（只关掉 tombstone purger 的后台任务），记忆经 `wiring.memory_v2.create`
直写——不经过模型，判的是"版本链 → 卡片"这条领域路径与响应模型。
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime

import jwt
import pytest
from fastapi.testclient import TestClient

from agent_harness.config import Settings
from agent_harness.identity import IdentityContext
from agent_harness.memory.v2.capability import MemoryV2Service
from agent_harness.memory.v2.types import TrustedMemoryIdentity
from agent_harness.web.app import create_app
from tests.memory.v2._records import make_draft

_SECRET = "journey-api-test-signing-secret-at-least-32"
_ALICE = IdentityContext("acme", "alice", ["user"])


def _auth(identity: IdentityContext) -> dict[str, str]:
    token = jwt.encode(
        {
            "tenant_id": identity.tenant_id,
            "user_id": identity.user_id,
            "scopes": list(identity.scopes),
            "exp": int(datetime.now(UTC).timestamp()) + 600,
        },
        _SECRET,
    )
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def app_client(tmp_path, monkeypatch):
    monkeypatch.setattr(MemoryV2Service, "start_tombstone_purger", lambda _self: None)
    settings = Settings(
        _env_file=None, workspace_dir=str(tmp_path), model_api_key="sk-test",
        jwt_secret=_SECRET, capabilities='{"memory": {"provider": "langmem"}}',
    )
    with TestClient(create_app(settings, enable_cors=False, allow_testserver=True)) as client:
        yield client


async def _seed(client: TestClient, content: str):
    _registry, wiring = await client.app.state.agent.get_wiring()
    assert wiring.memory_v2 is not None
    return await wiring.memory_v2.create(
        make_draft(content=content,
                   source_event_ids=[hashlib.sha256(content.encode()).hexdigest()]),
        TrustedMemoryIdentity(_ALICE.tenant_id, _ALICE.user_id),
    )


@pytest.mark.asyncio
async def test_journey_endpoint_returns_cards_nodes_and_empty_edges(app_client) -> None:
    first = await _seed(app_client, "初版：偏好 pnpm")
    _registry, wiring = await app_client.app.state.agent.get_wiring()
    assert wiring.memory_v2 is not None
    updated = await wiring.memory_v2.update(
        first.id, make_draft(content="改版：偏好 npm"), TrustedMemoryIdentity("acme", "alice"))

    response = app_client.get("/api/memory-journey", headers=_auth(_ALICE))

    assert response.status_code == 200
    payload = response.json()
    assert set(payload) == {"nodes", "edges", "memory", "stats"}
    assert payload["edges"] == []
    assert [card["version"] for card in payload["memory"]] == [2, 1]
    assert [card["state"] for card in payload["memory"]] == ["active", "superseded"]
    assert payload["memory"][0]["memory_id"] == updated.id
    assert payload["memory"][0]["source"] == "memory"
    assert set(payload["memory"][0]) == {
        "source", "timestamp", "title", "body", "fingerprint",
        "memory_id", "version", "state",
    }
    assert payload["stats"]["memory_nodes"] == 2
    assert payload["stats"]["learned_skills"] == 0
    assert payload["nodes"][0]["id"].startswith("memory:memory:0:")


@pytest.mark.asyncio
async def test_journey_endpoint_is_empty_before_any_memory(app_client) -> None:
    response = app_client.get("/api/memory-journey", headers=_auth(_ALICE))

    assert response.status_code == 200
    assert response.json() == {
        "nodes": [], "edges": [], "memory": [],
        "stats": {"memory_nodes": 0, "memory_skill_edges": 0,
                  "learned_skills": 0, "memories": 0},
    }
