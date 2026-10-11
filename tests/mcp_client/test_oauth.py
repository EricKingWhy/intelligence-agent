import pytest
from mcp.shared.auth import (
    OAuthClientInformationFull,
    OAuthMetadata,
    OAuthToken,
    ProtectedResourceMetadata,
)

from agent_harness.mcp.oauth import (
    MCPKeyringTokenStorage,
    OAuthCredentialError,
    logout_mcp_oauth,
)


class FakeKeyring:
    def __init__(self):
        self.values = {}

    def get_password(self, service, username):
        return self.values.get((service, username))

    def set_password(self, service, username, value):
        self.values[(service, username)] = value

    def delete_password(self, service, username):
        key = (service, username)
        if key not in self.values:
            raise KeyError(key)
        del self.values[key]


def _metadata(**overrides):
    values = {
        "issuer": "https://as.example.com",
        "authorization_endpoint": "https://as.example.com/authorize",
        "token_endpoint": "https://as.example.com/token",
    }
    values.update(overrides)
    return OAuthMetadata(**values)


@pytest.mark.asyncio
async def test_credentials_persist_across_storage_instances():
    keyring = FakeKeyring()
    first = MCPKeyringTokenStorage("remote", "https://mcp.example.com/mcp", keyring_api=keyring)
    await first.set_tokens(OAuthToken(access_token="access", refresh_token="refresh", scope="files:read"))
    await first.set_client_info(OAuthClientInformationFull(client_id="client"))
    await first.set_oauth_metadata(_metadata(revocation_endpoint="https://as.example.com/revoke"))
    await first.set_resource_metadata(ProtectedResourceMetadata(
        resource="https://mcp.example.com/mcp",
        authorization_servers=["https://as.example.com"],
    ))

    restarted = MCPKeyringTokenStorage("remote", "https://mcp.example.com/mcp", keyring_api=keyring)

    assert (await restarted.get_tokens()).refresh_token == "refresh"
    assert (await restarted.get_client_info()).client_id == "client"
    assert str((await restarted.get_oauth_metadata()).revocation_endpoint) == "https://as.example.com/revoke"
    assert str((await restarted.get_resource_metadata()).resource) == "https://mcp.example.com/mcp"


@pytest.mark.asyncio
async def test_oauth_client_configuration_isolated_from_previous_registration():
    keyring = FakeKeyring()
    old = MCPKeyringTokenStorage(
        "remote",
        "https://mcp.example.com/mcp",
        client_info=OAuthClientInformationFull(client_id="old-client"),
        oauth_identity="old-client",
        keyring_api=keyring,
    )
    current = MCPKeyringTokenStorage(
        "remote",
        "https://mcp.example.com/mcp",
        client_info=OAuthClientInformationFull(client_id="current-client"),
        oauth_identity="current-client",
        keyring_api=keyring,
    )
    await old.set_tokens(OAuthToken(access_token="old-access"))
    await old.set_client_info(OAuthClientInformationFull(client_id="old-client"))

    assert await current.get_tokens() is None
    assert (await current.get_client_info()).client_id == "current-client"


@pytest.mark.asyncio
async def test_oversized_credentials_fail_before_keyring_write():
    keyring = FakeKeyring()
    storage = MCPKeyringTokenStorage("remote", "https://mcp.example.com/mcp", keyring_api=keyring)

    with pytest.raises(OAuthCredentialError, match="exceed the secure credential store limit"):
        await storage.set_tokens(OAuthToken(access_token="x" * 3000))

    assert keyring.values == {}


@pytest.mark.asyncio
async def test_clear_is_idempotent_and_deletes_persisted_oauth_data():
    keyring = FakeKeyring()
    storage = MCPKeyringTokenStorage("remote", "https://mcp.example.com/mcp", keyring_api=keyring)
    await storage.set_tokens(OAuthToken(access_token="access"))

    await storage.clear()
    await storage.clear()

    assert await storage.get_tokens() is None
    assert keyring.values == {}


@pytest.mark.asyncio
async def test_clear_verifies_keyring_delete_failure_before_reporting_success():
    from keyring.errors import PasswordDeleteError

    keyring = FakeKeyring()
    storage = MCPKeyringTokenStorage("remote", "https://mcp.example.com/mcp", keyring_api=keyring)
    await storage.set_tokens(OAuthToken(access_token="access"))

    def fail_delete(*_):
        raise PasswordDeleteError("locked")

    keyring.delete_password = fail_delete

    with pytest.raises(OAuthCredentialError, match="could not clear MCP OAuth data"):
        await storage.clear()

    assert await storage.get_tokens() is not None


@pytest.mark.asyncio
async def test_logout_revokes_refresh_and_access_tokens_then_clears_local_state():
    import base64

    import httpx2

    keyring = FakeKeyring()
    storage = MCPKeyringTokenStorage("remote", "https://mcp.example.com/mcp", keyring_api=keyring)
    await storage.set_tokens(OAuthToken(access_token="access", refresh_token="refresh"))
    await storage.set_client_info(OAuthClientInformationFull(
        client_id="client id",
        client_secret="client:secret",
        token_endpoint_auth_method="client_secret_basic",
    ))
    await storage.set_oauth_metadata(_metadata(
        revocation_endpoint="http://127.0.0.1/revoke",
        revocation_endpoint_auth_methods_supported=["client_secret_basic"],
    ))
    requests = []

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return None

        async def post(self, url, *, data, headers):
            requests.append((url, data, headers))
            return httpx2.Response(200)

    result = await logout_mcp_oauth(
        storage,
        client_factory=lambda **_: Client(),
    )

    assert result == {"local_cleared": True, "remote_revoke": "succeeded"}
    assert [request[1]["token_type_hint"] for request in requests] == ["refresh_token", "access_token"]
    expected_basic = base64.b64encode(b"client+id:client%3Asecret").decode()
    assert all(request[2]["Authorization"] == f"Basic {expected_basic}" for request in requests)
    assert await storage.get_tokens() is None


@pytest.mark.asyncio
async def test_logout_reports_remote_failure_but_still_clears_local_state():
    import httpx2

    keyring = FakeKeyring()
    storage = MCPKeyringTokenStorage("remote", "https://mcp.example.com/mcp", keyring_api=keyring)
    await storage.set_tokens(OAuthToken(access_token="access", refresh_token="refresh"))
    await storage.set_client_info(OAuthClientInformationFull(client_id="client", token_endpoint_auth_method="none"))
    await storage.set_oauth_metadata(_metadata(revocation_endpoint="https://as.example.com/revoke"))
    revoked = []

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return None

        async def post(self, url, *, data, headers):
            revoked.append(data["token_type_hint"])
            return httpx2.Response(503 if data["token_type_hint"] == "refresh_token" else 200)

    result = await logout_mcp_oauth(storage, client_factory=lambda **_: Client())

    assert result == {
        "local_cleared": True,
        "remote_revoke": "failed",
        "detail": "revocation endpoint returned HTTP 503",
    }
    assert await storage.get_tokens() is None
    assert revoked == ["refresh_token", "access_token"]


@pytest.mark.asyncio
async def test_logout_without_revocation_metadata_only_clears_local_state():
    keyring = FakeKeyring()
    storage = MCPKeyringTokenStorage("remote", "https://mcp.example.com/mcp", keyring_api=keyring)
    await storage.set_tokens(OAuthToken(access_token="access"))
    await storage.set_client_info(OAuthClientInformationFull(client_id="client"))
    await storage.set_oauth_metadata(_metadata())

    result = await logout_mcp_oauth(storage)

    assert result == {"local_cleared": True, "remote_revoke": "not_supported"}
    assert await storage.get_tokens() is None


@pytest.mark.asyncio
async def test_logout_clears_local_state_when_stored_oauth_record_is_malformed():
    import json

    class MalformedKeyring(FakeKeyring):
        deleted = False

        def get_password(self, service, username):
            if not self.deleted:
                return json.dumps({"tokens": {"access_token": 7}})
            return None

        def delete_password(self, service, username):
            self.deleted = True

    keyring = MalformedKeyring()
    storage = MCPKeyringTokenStorage("remote", "https://mcp.example.com/mcp", keyring_api=keyring)

    result = await logout_mcp_oauth(storage)

    assert result == {
        "local_cleared": True,
        "remote_revoke": "failed",
        "detail": "stored OAuth credentials could not be read",
    }
    assert keyring.deleted


def test_oauth_client_provider_uses_the_locked_sdk_and_registered_redirect():
    from mcp.client.auth.oauth2 import OAuthClientProvider

    from agent_harness.mcp.config import parse_mcp_servers
    from agent_harness.mcp.oauth import make_oauth_client_provider

    config = parse_mcp_servers({"servers": [{
        "name": "remote",
        "transport": "http",
        "url": "https://mcp.example.com/mcp",
        "oauth": {"callbackPort": 8123, "scopes": "files:read"},
    }]})[0]
    storage = MCPKeyringTokenStorage("remote", config.url, keyring_api=FakeKeyring())

    provider = make_oauth_client_provider(config, storage)

    assert isinstance(provider, OAuthClientProvider)
    assert str(provider.context.client_metadata.redirect_uris[0]) == "http://localhost:8123/callback"
    assert provider.context.client_metadata.scope == "files:read"


@pytest.mark.asyncio
async def test_runtime_refuses_oauth_connection_without_saved_credentials(monkeypatch):
    from contextlib import asynccontextmanager

    from agent_harness.mcp.client import MCPServerConnection, MCPServerDownError
    from agent_harness.mcp.config import parse_mcp_servers

    config = parse_mcp_servers({"servers": [{
        "name": "remote",
        "transport": "http",
        "url": "https://mcp.example.com/mcp",
        "oauth": {},
    }]})[0]

    class EmptyStorage:
        async def get_tokens(self):
            return None

    transport_opened = False

    @asynccontextmanager
    async def forbidden_transport(*args, **kwargs):
        nonlocal transport_opened
        transport_opened = True
        raise AssertionError("unauthenticated MCP transport must not start")
        yield

    monkeypatch.setattr("agent_harness.mcp.client.make_oauth_token_storage", lambda _: EmptyStorage())
    monkeypatch.setattr("agent_harness.mcp.client.streamable_http_client", forbidden_transport)

    connection = MCPServerConnection(config)
    with pytest.raises(MCPServerDownError, match="OAuth login is required"):
        await connection.connect()

    assert not transport_opened


@pytest.mark.asyncio
async def test_sdk_oauth_challenge_completes_login_and_persists_tokens():
    import json
    from urllib.parse import parse_qs, urlsplit

    import httpx2
    from mcp.shared.auth import AuthorizationCodeResult

    from agent_harness.mcp.config import parse_mcp_servers
    from agent_harness.mcp.oauth import make_oauth_client_provider

    config = parse_mcp_servers({"servers": [{
        "name": "remote",
        "transport": "http",
        "url": "https://mcp.example.com/mcp",
        "oauth": {"scopes": "files:read"},
    }]})[0]
    keyring = FakeKeyring()
    storage = MCPKeyringTokenStorage(config.name, config.url, keyring_api=keyring)
    auth_state = {}
    requests = []

    async def redirect_handler(url):
        auth_state.update(parse_qs(urlsplit(url).query))

    async def callback_handler():
        return AuthorizationCodeResult(
            code="authorization-code",
            state=auth_state["state"][0],
            iss="https://as.example.com",
        )

    def oauth_http(request):
        requests.append(request)
        if request.url.path == "/mcp" and "authorization" not in request.headers:
            return httpx2.Response(
                401,
                headers={
                    "WWW-Authenticate": 'Bearer resource_metadata="https://mcp.example.com/.well-known/oauth-protected-resource"'
                },
            )
        if request.url.path == "/mcp":
            assert request.headers["authorization"] == "Bearer access-token"
            return httpx2.Response(200, json={"ok": True})
        if request.url.path == "/.well-known/oauth-protected-resource":
            return httpx2.Response(200, json={
                "resource": "https://mcp.example.com/mcp",
                "authorization_servers": ["https://as.example.com"],
                "scopes_supported": ["files:read"],
            })
        if request.url.path == "/.well-known/oauth-authorization-server":
            return httpx2.Response(200, json={
                "issuer": "https://as.example.com",
                "authorization_endpoint": "https://as.example.com/authorize",
                "token_endpoint": "https://as.example.com/token",
                "registration_endpoint": "https://as.example.com/register",
                "revocation_endpoint": "https://as.example.com/revoke",
                "response_types_supported": ["code"],
                "grant_types_supported": ["authorization_code", "refresh_token"],
                "code_challenge_methods_supported": ["S256"],
                "token_endpoint_auth_methods_supported": ["none"],
            })
        if request.url.path == "/register":
            body = json.loads(request.content)
            return httpx2.Response(201, json={
                "client_id": "client-id",
                "token_endpoint_auth_method": "none",
                "redirect_uris": body["redirect_uris"],
            })
        if request.url.path == "/token":
            return httpx2.Response(200, json={
                "access_token": "access-token",
                "refresh_token": "refresh-token",
                "token_type": "Bearer",
                "expires_in": 3600,
                "scope": "files:read",
            })
        raise AssertionError(f"unexpected OAuth request: {request.method} {request.url}")

    provider = make_oauth_client_provider(
        config,
        storage,
        redirect_handler=redirect_handler,
        callback_handler=callback_handler,
    )
    async with httpx2.AsyncClient(
        auth=provider,
        transport=httpx2.MockTransport(oauth_http),
    ) as client:
        response = await client.get(config.url)

    assert response.status_code == 200
    assert (await storage.get_tokens()).refresh_token == "refresh-token"
    assert (await storage.get_client_info()).client_id == "client-id"
    assert str((await storage.get_oauth_metadata()).revocation_endpoint) == "https://as.example.com/revoke"
    assert str((await storage.get_resource_metadata()).resource) == "https://mcp.example.com/mcp"
    assert auth_state["code_challenge_method"] == ["S256"]
    assert len(requests) >= 6


@pytest.mark.asyncio
async def test_restart_refreshes_with_persisted_authorization_server_metadata():
    import httpx2

    from agent_harness.mcp.config import parse_mcp_servers
    from agent_harness.mcp.oauth import make_oauth_client_provider

    config = parse_mcp_servers({"servers": [{
        "name": "remote",
        "transport": "http",
        "url": "https://mcp.example.com/mcp",
        "oauth": {},
    }]})[0]
    keyring = FakeKeyring()
    before_restart = MCPKeyringTokenStorage(config.name, config.url, keyring_api=keyring)
    await before_restart.set_tokens(OAuthToken(
        access_token="expired-access",
        refresh_token="refresh-token",
        expires_in=-1,
    ))
    await before_restart.set_client_info(OAuthClientInformationFull(
        client_id="client-id",
        token_endpoint_auth_method="none",
        issuer="https://as.example.com",
    ))
    await before_restart.set_oauth_metadata(_metadata(
        revocation_endpoint="https://as.example.com/revoke",
        token_endpoint_auth_methods_supported=["none"],
    ))
    await before_restart.set_resource_metadata(ProtectedResourceMetadata(
        resource="https://mcp.example.com/mcp",
        authorization_servers=["https://as.example.com"],
    ))
    after_restart = MCPKeyringTokenStorage(config.name, config.url, keyring_api=keyring)
    requests = []

    def refresh_http(request):
        requests.append(request)
        if request.url.path == "/token":
            assert request.content
            return httpx2.Response(200, json={
                "access_token": "new-access",
                "refresh_token": "new-refresh",
                "token_type": "Bearer",
                "expires_in": 3600,
            })
        if request.url.path == "/mcp":
            assert request.headers["authorization"] == "Bearer new-access"
            return httpx2.Response(200, json={"ok": True})
        raise AssertionError(f"unexpected refresh request: {request.method} {request.url}")

    provider = make_oauth_client_provider(config, after_restart)
    async with httpx2.AsyncClient(
        auth=provider,
        transport=httpx2.MockTransport(refresh_http),
    ) as client:
        response = await client.get(config.url)

    assert response.status_code == 200
    assert [request.url.path for request in requests] == ["/token", "/mcp"]
    assert (await after_restart.get_tokens()).access_token == "new-access"


@pytest.mark.asyncio
async def test_rejected_persisted_token_failure_points_to_login(monkeypatch):
    import httpx2

    from agent_harness.mcp.client import MCPServerConnection, MCPServerDownError
    from agent_harness.mcp.config import parse_mcp_servers

    config = parse_mcp_servers({"servers": [{
        "name": "remote",
        "transport": "http",
        "url": "https://mcp.example.com/mcp",
        "oauth": {},
    }]})[0]
    keyring = FakeKeyring()
    storage = MCPKeyringTokenStorage(config.name, config.url, keyring_api=keyring)
    await storage.set_tokens(OAuthToken(
        access_token="expired-access",
        refresh_token="revoked-refresh",
        expires_in=-1,
    ))
    await storage.set_client_info(OAuthClientInformationFull(
        client_id="client-id",
        token_endpoint_auth_method="none",
        issuer="https://as.example.com",
    ))
    await storage.set_oauth_metadata(_metadata(
        token_endpoint_auth_methods_supported=["none"],
    ))
    await storage.set_resource_metadata(ProtectedResourceMetadata(
        resource=config.url,
        authorization_servers=["https://as.example.com"],
    ))

    def rejected_http(request):
        if request.url.path == "/mcp":
            return httpx2.Response(401, headers={
                "WWW-Authenticate": 'Bearer resource_metadata="https://mcp.example.com/.well-known/oauth-protected-resource"'
            })
        if request.url.path == "/.well-known/oauth-protected-resource":
            return httpx2.Response(200, json={
                "resource": config.url,
                "authorization_servers": ["https://as.example.com"],
            })
        if request.url.path == "/.well-known/oauth-authorization-server":
            return httpx2.Response(200, json={
                "issuer": "https://as.example.com",
                "authorization_endpoint": "https://as.example.com/authorize",
                "token_endpoint": "https://as.example.com/token",
                "response_types_supported": ["code"],
                "code_challenge_methods_supported": ["S256"],
                "token_endpoint_auth_methods_supported": ["none"],
            })
        if request.url.path == "/token":
            return httpx2.Response(400, json={"error": "invalid_grant"})
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    monkeypatch.setattr(
        "agent_harness.mcp.client.make_oauth_token_storage",
        lambda _config: storage,
    )
    monkeypatch.setattr(
        "agent_harness.mcp.client.build_http_client",
        lambda _config, *, auth=None: httpx2.AsyncClient(
            auth=auth,
            transport=httpx2.MockTransport(rejected_http),
        ),
    )

    with pytest.raises(MCPServerDownError, match="plugins mcp login remote"):
        await MCPServerConnection(config).connect()


@pytest.mark.asyncio
async def test_unclassified_oauth_flow_error_does_not_point_to_login():
    from contextlib import asynccontextmanager

    from mcp.client.auth.exceptions import OAuthFlowError

    from agent_harness.mcp.client import MCPServerConnection, MCPServerDownError
    from agent_harness.mcp.config import parse_mcp_servers

    config = parse_mcp_servers({"servers": [{
        "name": "remote",
        "transport": "http",
        "url": "https://mcp.example.com/mcp",
        "oauth": {},
    }]})[0]

    class Session:
        async def initialize(self):
            return None

        async def call_tool(self, *_args, **_kwargs):
            raise ExceptionGroup(
                "unhandled errors in a TaskGroup",
                [ExceptionGroup("nested transport error", [
                    OAuthFlowError("authorization is required"),
                ])],
            )

    @asynccontextmanager
    async def session_factory():
        yield Session()

    connection = MCPServerConnection(
        config,
        session_factory=session_factory,
        oauth_redirect_handler=lambda _url: None,
    )
    await connection.connect()
    try:
        with pytest.raises(MCPServerDownError) as error:
            await connection.call_tool("write", {})
        assert "plugins mcp login remote" not in str(error.value)
    finally:
        await connection.aclose()


@pytest.mark.asyncio
async def test_protected_resource_mismatch_does_not_mark_reauthorization_required():
    import httpx2
    from mcp.client.auth.exceptions import OAuthFlowError

    from agent_harness.mcp.config import parse_mcp_servers
    from agent_harness.mcp.oauth import make_oauth_client_provider

    config = parse_mcp_servers({"servers": [{
        "name": "remote",
        "transport": "http",
        "url": "https://mcp.example.com/mcp",
        "oauth": {},
    }]})[0]
    storage = MCPKeyringTokenStorage(config.name, config.url, keyring_api=FakeKeyring())
    provider = make_oauth_client_provider(config, storage)

    def mismatch_http(request):
        if request.url.path == "/mcp":
            return httpx2.Response(401, headers={
                "WWW-Authenticate": 'Bearer resource_metadata="https://mcp.example.com/.well-known/oauth-protected-resource"'
            })
        if request.url.path == "/.well-known/oauth-protected-resource":
            return httpx2.Response(200, json={
                "resource": "https://other.example.com/mcp",
                "authorization_servers": ["https://as.example.com"],
            })
        raise AssertionError(f"unexpected OAuth request: {request.method} {request.url}")

    async with httpx2.AsyncClient(
        auth=provider,
        transport=httpx2.MockTransport(mismatch_http),
    ) as client:
        with pytest.raises(OAuthFlowError, match="Protected resource"):
            await client.get(config.url)

    assert not provider.reauthorization_required


@pytest.mark.asyncio
async def test_active_http_connection_rejected_token_points_to_login(monkeypatch):
    import json

    import httpx2

    from agent_harness.mcp.client import MCPServerConnection, MCPServerDownError
    from agent_harness.mcp.config import parse_mcp_servers

    config = parse_mcp_servers({"servers": [{
        "name": "remote",
        "transport": "http",
        "url": "https://mcp.example.com/mcp",
        "oauth": {},
    }]})[0]
    keyring = FakeKeyring()
    storage = MCPKeyringTokenStorage(config.name, config.url, keyring_api=keyring)
    await storage.set_tokens(OAuthToken(
        access_token="rejected-access",
        refresh_token="refresh-token",
        expires_in=3600,
    ))
    await storage.set_client_info(OAuthClientInformationFull(
        client_id="client-id",
        token_endpoint_auth_method="none",
        issuer="https://as.example.com",
    ))
    await storage.set_oauth_metadata(_metadata(
        token_endpoint_auth_methods_supported=["none"],
    ))
    await storage.set_resource_metadata(ProtectedResourceMetadata(
        resource=config.url,
        authorization_servers=["https://as.example.com"],
    ))
    methods = []

    def rejected_http(request):
        if request.url.path == "/mcp":
            assert request.headers["authorization"] == "Bearer rejected-access"
            message = json.loads(request.content)
            method = message["method"]
            methods.append(method)
            if method == "initialize":
                return httpx2.Response(200, json={
                    "jsonrpc": "2.0",
                    "id": message["id"],
                    "result": {
                        "protocolVersion": "2025-03-26",
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": "oauth-mcp", "version": "1.0"},
                    },
                })
            if method == "notifications/initialized":
                return httpx2.Response(202)
            if method == "tools/call":
                return httpx2.Response(401, headers={
                    "WWW-Authenticate": 'Bearer resource_metadata="https://mcp.example.com/.well-known/oauth-protected-resource"'
                })
            raise AssertionError(f"unexpected MCP method: {method}")
        if request.url.path == "/.well-known/oauth-protected-resource":
            return httpx2.Response(200, json={
                "resource": config.url,
                "authorization_servers": ["https://as.example.com"],
            })
        if request.url.path == "/.well-known/oauth-authorization-server":
            return httpx2.Response(200, json={
                "issuer": "https://as.example.com",
                "authorization_endpoint": "https://as.example.com/authorize",
                "token_endpoint": "https://as.example.com/token",
                "response_types_supported": ["code"],
                "code_challenge_methods_supported": ["S256"],
                "token_endpoint_auth_methods_supported": ["none"],
            })
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    monkeypatch.setattr(
        "agent_harness.mcp.client.make_oauth_token_storage",
        lambda _config: storage,
    )
    monkeypatch.setattr(
        "agent_harness.mcp.client.build_http_client",
        lambda _config, *, auth=None: httpx2.AsyncClient(
            auth=auth,
            transport=httpx2.MockTransport(rejected_http),
        ),
    )

    connection = MCPServerConnection(config)
    await connection.connect()
    try:
        with pytest.raises(MCPServerDownError, match="plugins mcp login remote"):
            await connection.call_tool("write", {})
    finally:
        await connection.aclose()

    assert methods == ["initialize", "notifications/initialized", "tools/call"]


@pytest.mark.asyncio
async def test_oauth_server_cannot_succeed_on_an_unauthenticated_request():
    import httpx2
    from mcp.client.auth.exceptions import OAuthFlowError

    from agent_harness.mcp.config import parse_mcp_servers
    from agent_harness.mcp.oauth import make_oauth_client_provider

    config = parse_mcp_servers({"servers": [{
        "name": "remote",
        "transport": "http",
        "url": "https://mcp.example.com/mcp",
        "oauth": {},
    }]})[0]
    storage = MCPKeyringTokenStorage(config.name, config.url, keyring_api=FakeKeyring())
    provider = make_oauth_client_provider(config, storage)

    async with httpx2.AsyncClient(
        auth=provider,
        transport=httpx2.MockTransport(lambda request: httpx2.Response(200, json={"ok": True})),
    ) as client:
        with pytest.raises(OAuthFlowError, match="accepted an unauthenticated request"):
            await client.get(config.url)


@pytest.mark.asyncio
async def test_authenticated_http_connection_reuses_existing_mcp_tool_path(monkeypatch, tmp_path):
    import json
    from urllib.parse import parse_qs, urlsplit

    import httpx2
    from langchain_core.messages import AIMessage
    from mcp.shared.auth import AuthorizationCodeResult

    from agent_harness.agent import AgentRuntime
    from agent_harness.mcp.adapter import MCPTool
    from agent_harness.mcp.client import MCPServerConnection
    from agent_harness.mcp.config import parse_mcp_servers
    from agent_harness.session import MODEL_COMPLETED, TOOL_CALL, TOOL_RESULT
    from agent_harness.storage import OperationState, SqliteOperationLedger
    from agent_harness.tooling import PermissionPolicy, ToolExecutor, ToolRegistry
    from tests.conftest import make_session
    from tests.scripted_model import ScriptedModel

    config = parse_mcp_servers({"servers": [{
        "name": "remote",
        "transport": "http",
        "url": "https://mcp.example.com/mcp",
        "oauth": {"scopes": "files:write"},
    }]})[0]
    keyring = FakeKeyring()
    auth_state = {}
    calls = []
    tool_calls = []

    async def redirect_handler(url):
        auth_state.update(parse_qs(urlsplit(url).query))

    async def callback_handler():
        return AuthorizationCodeResult(
            code="authorization-code",
            state=auth_state["state"][0],
            iss="https://as.example.com",
        )

    def response(payload, status=200, headers=None):
        return httpx2.Response(status, json=payload, headers=headers)

    def server_http(request):
        path = request.url.path
        if path == "/mcp":
            if not request.headers.get("authorization"):
                return response(None, 401, {
                    "WWW-Authenticate": 'Bearer resource_metadata="https://mcp.example.com/.well-known/oauth-protected-resource"'
                })
            assert request.headers["authorization"] == "Bearer access-token"
            message = json.loads(request.content)
            method = message["method"]
            calls.append(method)
            if method == "notifications/initialized":
                return httpx2.Response(202)
            if method == "initialize":
                result = {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "oauth-mcp", "version": "1.0"},
                }
            elif method == "tools/list":
                result = {"tools": [{
                    "name": "write",
                    "description": "write through the authenticated server",
                    "inputSchema": {
                        "type": "object",
                        "properties": {"text": {"type": "string"}},
                        "required": ["text"],
                    },
                    "annotations": {"readOnlyHint": False},
                }]}
            elif method == "tools/call":
                tool_calls.append(message["params"]["arguments"]["text"])
                result = {"content": [{"type": "text", "text": "written"}], "isError": False}
            else:
                raise AssertionError(f"unexpected MCP request: {method}")
            return response({"jsonrpc": "2.0", "id": message["id"], "result": result})
        if path == "/.well-known/oauth-protected-resource":
            return response({
                "resource": "https://mcp.example.com/mcp",
                "authorization_servers": ["https://as.example.com"],
                "scopes_supported": ["files:write"],
            })
        if path == "/.well-known/oauth-authorization-server":
            return response({
                "issuer": "https://as.example.com",
                "authorization_endpoint": "https://as.example.com/authorize",
                "token_endpoint": "https://as.example.com/token",
                "registration_endpoint": "https://as.example.com/register",
                "revocation_endpoint": "https://as.example.com/revoke",
                "response_types_supported": ["code"],
                "grant_types_supported": ["authorization_code", "refresh_token"],
                "code_challenge_methods_supported": ["S256"],
                "token_endpoint_auth_methods_supported": ["none"],
            })
        if path == "/register":
            body = json.loads(request.content)
            return response({
                "client_id": "client-id",
                "token_endpoint_auth_method": "none",
                "redirect_uris": body["redirect_uris"],
            }, status=201)
        if path == "/token":
            return response({
                "access_token": "access-token",
                "refresh_token": "refresh-token",
                "token_type": "Bearer",
                "expires_in": 3600,
                "scope": "files:write",
            })
        raise AssertionError(f"unexpected HTTP request: {request.method} {request.url}")

    monkeypatch.setattr(
        "agent_harness.mcp.client.make_oauth_token_storage",
        lambda server: MCPKeyringTokenStorage(server.name, server.url, keyring_api=keyring),
    )
    monkeypatch.setattr(
        "agent_harness.mcp.client.build_http_client",
        lambda server, *, auth=None: httpx2.AsyncClient(
            auth=auth,
            transport=httpx2.MockTransport(server_http),
        ),
    )

    first_connection = MCPServerConnection(
        config,
        oauth_redirect_handler=redirect_handler,
        oauth_callback_handler=callback_handler,
    )
    await first_connection.connect()
    remote_tools = await first_connection.list_tools()
    assert [tool.name for tool in remote_tools] == ["write"]
    tool_registry = ToolRegistry()
    tool_registry.register(MCPTool(first_connection, config, remote_tools[0]))
    ledger = SqliteOperationLedger(tmp_path / "oauth-tool-state.db")
    await ledger.initialize()
    executor = ToolExecutor(
        tool_registry,
        policy=PermissionPolicy.DANGER_FULL_ACCESS,
        operation_ledger=ledger,
    )
    runtime = AgentRuntime(
        ScriptedModel([
            AIMessage(content="", tool_calls=[{
                "id": "oauth-tool-call",
                "name": "mcp__remote__write",
                "args": {"text": "from runtime"},
                "type": "tool_call",
            }]),
            AIMessage(content="completed"),
        ]),
        tool_registry,
        executor,
    )
    session = make_session(tmp_path / "oauth-sessions")
    first_result = await runtime.run(session, "use the OAuth MCP tool")
    await first_connection.aclose()

    restarted_connection = MCPServerConnection(config)
    await restarted_connection.connect()
    second_result = await restarted_connection.call_tool("write", {"text": "after restart"})
    await restarted_connection.aclose()

    assert first_result.completed
    assert second_result.content[0].text == "written"
    assert tool_calls == ["from runtime", "after restart"]
    assert "tools/call" in calls
    operation = await ledger.get(session.session_id, "oauth-tool-call")
    assert operation is not None and operation.state == OperationState.SUCCEEDED
    event_types = [event.type for event in session.events]
    assert MODEL_COMPLETED in event_types
    assert event_types.index(TOOL_CALL) < event_types.index(TOOL_RESULT)


@pytest.mark.asyncio
async def test_user_denial_does_not_persist_a_token_or_retry_the_mcp_request():
    import httpx2
    from mcp.client.auth.exceptions import OAuthFlowError

    from agent_harness.mcp.config import parse_mcp_servers
    from agent_harness.mcp.oauth import make_oauth_client_provider

    config = parse_mcp_servers({"servers": [{
        "name": "remote",
        "transport": "http",
        "url": "https://mcp.example.com/mcp",
        "oauth": {},
    }]})[0]
    storage = MCPKeyringTokenStorage(config.name, config.url, keyring_api=FakeKeyring())
    requests = []

    async def redirect_handler(_url):
        return None

    async def callback_handler():
        raise OAuthFlowError("user denied")

    def denied_http(request):
        requests.append(request)
        if request.url.path == "/mcp":
            return httpx2.Response(401, headers={
                "WWW-Authenticate": 'Bearer resource_metadata="https://mcp.example.com/.well-known/oauth-protected-resource"'
            })
        if request.url.path == "/.well-known/oauth-protected-resource":
            return httpx2.Response(200, json={
                "resource": config.url,
                "authorization_servers": ["https://as.example.com"],
            })
        if request.url.path == "/.well-known/oauth-authorization-server":
            return httpx2.Response(200, json={
                "issuer": "https://as.example.com",
                "authorization_endpoint": "https://as.example.com/authorize",
                "token_endpoint": "https://as.example.com/token",
                "registration_endpoint": "https://as.example.com/register",
                "response_types_supported": ["code"],
                "code_challenge_methods_supported": ["S256"],
                "token_endpoint_auth_methods_supported": ["none"],
            })
        if request.url.path == "/register":
            import json

            registration = json.loads(request.content)
            return httpx2.Response(201, json={
                "client_id": "client-id",
                "token_endpoint_auth_method": "none",
                "redirect_uris": registration["redirect_uris"],
            })
        raise AssertionError(f"unexpected request after denial: {request.method} {request.url}")

    provider = make_oauth_client_provider(
        config,
        storage,
        redirect_handler=redirect_handler,
        callback_handler=callback_handler,
    )
    async with httpx2.AsyncClient(
        auth=provider,
        transport=httpx2.MockTransport(denied_http),
    ) as client:
        with pytest.raises(OAuthFlowError, match="user denied"):
            await client.get(config.url)

    assert sum(request.url.path == "/mcp" for request in requests) == 1
    assert await storage.get_tokens() is None


@pytest.mark.asyncio
async def test_insufficient_scope_uses_sdk_step_up_and_preserves_existing_scope():
    from urllib.parse import parse_qs, urlsplit

    import httpx2
    from mcp.shared.auth import AuthorizationCodeResult

    from agent_harness.mcp.config import parse_mcp_servers
    from agent_harness.mcp.oauth import make_oauth_client_provider

    config = parse_mcp_servers({"servers": [{
        "name": "remote",
        "transport": "http",
        "url": "https://mcp.example.com/mcp",
        "oauth": {"scopes": "files:read"},
    }]})[0]
    keyring = FakeKeyring()
    storage = MCPKeyringTokenStorage(config.name, config.url, keyring_api=keyring)
    await storage.set_tokens(OAuthToken(
        access_token="read-token",
        refresh_token="read-refresh",
        expires_in=3600,
        scope="files:read",
    ))
    await storage.set_client_info(OAuthClientInformationFull(
        client_id="client-id",
        token_endpoint_auth_method="none",
        issuer="https://as.example.com",
        redirect_uris=["http://localhost:8765/callback"],
    ))
    await storage.set_oauth_metadata(_metadata(
        revocation_endpoint="https://as.example.com/revoke",
        authorization_endpoint="https://as.example.com/authorize",
        token_endpoint="https://as.example.com/token",
    ))
    await storage.set_resource_metadata(ProtectedResourceMetadata(
        resource=config.url,
        authorization_servers=["https://as.example.com"],
    ))
    auth_state = {}

    async def redirect_handler(url):
        auth_state.update(parse_qs(urlsplit(url).query))

    async def callback_handler():
        return AuthorizationCodeResult(
            code="step-up-code",
            state=auth_state["state"][0],
            iss="https://as.example.com",
        )

    def step_up_http(request):
        if request.url.path == "/mcp":
            token = request.headers.get("authorization")
            if token == "Bearer read-token":
                return httpx2.Response(403, headers={
                    "WWW-Authenticate": 'Bearer error="insufficient_scope", scope="files:write"'
                })
            assert token == "Bearer write-token"
            return httpx2.Response(200, json={"ok": True})
        if request.url.path == "/token":
            return httpx2.Response(200, json={
                "access_token": "write-token",
                "refresh_token": "write-refresh",
                "token_type": "Bearer",
                "expires_in": 3600,
                "scope": "files:read files:write",
            })
        raise AssertionError(f"unexpected scope step-up request: {request.method} {request.url}")

    provider = make_oauth_client_provider(
        config,
        storage,
        redirect_handler=redirect_handler,
        callback_handler=callback_handler,
    )
    async with httpx2.AsyncClient(
        auth=provider,
        transport=httpx2.MockTransport(step_up_http),
    ) as client:
        response = await client.get(config.url)

    assert response.status_code == 200
    assert set(auth_state["scope"][0].split()) == {"files:read", "files:write"}
    assert set((await storage.get_tokens()).scope.split()) == {"files:read", "files:write"}
