"""请求身份在异步任务之间隔离。"""

import asyncio
from dataclasses import FrozenInstanceError

import jwt
import pytest
from fastapi.testclient import TestClient

from agent_harness.config import Settings
from agent_harness.identity import (
    IdentityContext,
    get_identity_context,
    identity_context_var,
    set_identity_context,
)
from agent_harness.web.app import create_app


def test_default_identity_when_contextvar_unset():
    assert get_identity_context() == IdentityContext("local", "local", ["user", "session"])


@pytest.mark.asyncio
async def test_contextvar_isolated_across_async_tasks():
    async def read_as(user):
        token = set_identity_context(IdentityContext("acme", user, ["user"]))
        try:
            await asyncio.sleep(0)
            async def child():
                return get_identity_context().user_id
            return await asyncio.create_task(child())
        finally:
            identity_context_var.reset(token)

    assert await asyncio.gather(read_as("alice"), read_as("bob")) == ["alice", "bob"]
    assert get_identity_context().user_id == "local"


def test_identity_is_immutable():
    identity = IdentityContext("acme", "alice", ["user"])
    with pytest.raises(FrozenInstanceError):
        identity.user_id = "bob"


def test_child_cannot_mutate_inherited_scope_permissions():
    scopes = ["user"]
    identity = IdentityContext("acme", "alice", scopes)
    scopes.append("session")
    assert list(identity.scopes) == ["user"]
    with pytest.raises((AttributeError, TypeError)):
        identity.scopes.append("session")


def test_auth_seam_sets_contextvar(tmp_path):
    secret = "test-signing-secret-at-least-32-characters"
    app = create_app(Settings(_env_file=None, workspace_dir=str(tmp_path), jwt_secret=secret))

    @app.get("/identity-probe")
    async def probe():
        return get_identity_context()

    encoded = jwt.encode({"tenant_id": "acme", "user_id": "alice", "scopes": ["user"]}, secret)
    with TestClient(app) as client:
        response = client.get("/identity-probe", headers={"Authorization": f"Bearer {encoded}"})
        assert response.json() == {"tenant_id": "acme", "user_id": "alice", "scopes": ["user"]}
        assert client.get("/identity-probe").json()["user_id"] == "local"
        assert client.get("/identity-probe", headers={"Authorization": "Bearer invalid"}).status_code == 401
        expired = jwt.encode({"tenant_id": "acme", "user_id": "alice", "exp": 1}, secret)
        assert client.get("/identity-probe", headers={"Authorization": f"Bearer {expired}"}).status_code == 401
        forged = jwt.encode({"tenant_id": "acme", "user_id": "alice"}, "other-secret-at-least-32-characters")
        assert client.get("/identity-probe", headers={"Authorization": f"Bearer {forged}"}).status_code == 401
    assert get_identity_context().user_id == "local"


def test_no_secret_does_not_trust_bearer_identity(tmp_path):
    app = create_app(Settings(_env_file=None, workspace_dir=str(tmp_path)))

    @app.get("/identity-probe")
    async def probe():
        return get_identity_context()

    with TestClient(app) as client:
        assert client.get("/identity-probe", headers={"Authorization": "Bearer untrusted"}).json()["user_id"] == "local"
